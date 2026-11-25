"""Make an installed app easy to start, the normal way for each OS.

* **macOS** — a small app in ``~/Applications`` (shows in Launchpad and Spotlight)
  plus a command in ``~/.local/bin``.
* **Windows** — a Start Menu shortcut under *ENVY*.
* **Linux** — a menu entry (``.desktop``) plus a command in ``~/.local/bin``.

Every file made here is recorded, so removing an app removes exactly these.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .catalog import App, ToolsError


@dataclass
class Paths:
    home: Path                # ENVY Tools' own data: apps/, installed.json
    bin_dir: Path | None      # command-line launchers (None on Windows)
    menu_dir: Path            # ~/Applications, Start Menu\Programs\ENVY, or ~/.local/share/applications

    @property
    def apps(self) -> Path:
        return self.home / "apps"

    @property
    def manifest(self) -> Path:
        return self.home / "installed.json"

    @classmethod
    def default(cls, os_name: str) -> "Paths":
        override = os.environ.get("ENVY_TOOLS_HOME")
        if override:
            base = Path(override).expanduser()
            return cls(base, None if os_name == "windows" else base / "bin", base / "menu")
        user = Path.home()
        if os_name == "macos":
            return cls(user / "Library" / "Application Support" / "ENVY Tools",
                       user / ".local" / "bin", user / "Applications")
        if os_name == "windows":
            local = Path(os.environ.get("LOCALAPPDATA") or user / "AppData" / "Local")
            roaming = Path(os.environ.get("APPDATA") or user / "AppData" / "Roaming")
            return cls(local / "ENVY Tools", None,
                       roaming / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "ENVY")
        data = Path(os.environ.get("XDG_DATA_HOME") or user / ".local" / "share")
        return cls(data / "envy-tools", user / ".local" / "bin", data / "applications")


def _write(path: Path, text: str, mode: int = 0o644) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.chmod(path, mode)
    return path


def _link(target: Path, link: Path) -> Path:
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(target)
    return link


def _sh_quote(text: str) -> str:
    return "'" + str(text).replace("'", "'\"'\"'") + "'"


def launch_target(app: App, folder: Path, os_name: str) -> Path:
    """What starting the app means: its own launcher script if it has one, else its program."""
    start = app.start(os_name)
    return folder / (start if start else app.exe_name(os_name))


def _wrapper(script: Path, dest: Path) -> Path:
    """A command that runs a launcher script in place. A bare symlink would break
    scripts that find their files relative to their own location."""
    if dest.is_symlink() or dest.exists():
        dest.unlink()
    return _write(dest, "#!/bin/sh\nexec " + _sh_quote(script) + ' "$@"\n', 0o755)


def cli_helper(app: App, exe: Path) -> Path:
    """For one-shot commands: a script that shows the command's help, then leaves an
    interactive shell where it's on PATH, so a menu click doesn't flash and vanish."""
    helper = exe.parent / ".envy-tools-open.sh"
    tip = f"{app.name}: type  {exe.name} --help  to see every option."
    return _write(helper, "#!/bin/sh\n"
                  f"export PATH={_sh_quote(exe.parent)}:\"$PATH\"\n"
                  'cd "$HOME"\n'
                  f"{_sh_quote(exe)} --help\n"
                  f"echo; echo {_sh_quote(tip)}\n"
                  'exec "${SHELL:-/bin/sh}" -i\n', 0o755)


def windows_command(app: App, target: Path) -> tuple[str, str]:
    """(program, arguments) for a Start Menu shortcut that starts `target` properly."""
    if target.suffix.lower() == ".ps1":
        return "powershell.exe", f'-NoProfile -ExecutionPolicy Bypass -NoExit -File "{target}"'
    if app.cli:
        return os.environ.get("ComSpec") or "cmd.exe", f'/k ""{target}" --help"'
    return str(target), ""


def macos_app(app: App, exe: Path, menu_dir: Path) -> Path:
    bundle = menu_dir / f"{app.name}.app"
    if bundle.exists():
        shutil.rmtree(bundle)
    if app.kind == "terminal":
        body = f'exec open -a Terminal {_sh_quote(exe)}\n'
    else:
        body = f'exec {_sh_quote(exe)} "$@"\n'
    _write(bundle / "Contents" / "MacOS" / "launch", "#!/bin/sh\n" + body, 0o755)
    info = {"CFBundleName": app.name, "CFBundleDisplayName": app.name,
            "CFBundleIdentifier": f"io.github.xzchick.envy-tools.{app.slug}",
            "CFBundleExecutable": "launch", "CFBundlePackageType": "APPL",
            "CFBundleShortVersionString": "1.0"}
    path = bundle / "Contents" / "Info.plist"
    with open(path, "wb") as fh:
        plistlib.dump(info, fh)
    return bundle


def linux_desktop(app: App, exe: Path, menu_dir: Path) -> Path:
    exec_line = '"' + str(exe).replace("\\", "\\\\").replace('"', '\\"') + '"'
    keywords = "ENVY;" + {"recon": "OSINT;recon;", "tunnel": "VPN;WireGuard;", "terminal": "AI;LLM;Ollama;research;",
                          "hash-audit": "hash;password;recovery;hashcat;john;"}.get(app.slug, "encrypted;offline;")
    text = ("[Desktop Entry]\nType=Application\n"
            f"Name={app.name}\nComment={app.summary}\nExec={exec_line}\n"
            f"Terminal={'true' if app.kind == 'terminal' else 'false'}\n"
            f"Categories=Utility;Security;\nKeywords={keywords}\n")
    return _write(menu_dir / f"{app.bundle}.desktop", text)


def windows_shortcut(app: App, exe: Path, menu_dir: Path, run=subprocess.run) -> Path:
    menu_dir.mkdir(parents=True, exist_ok=True)
    lnk = menu_dir / f"{app.name}.lnk"
    program, args = windows_command(app, exe)
    # Values travel in environment variables, so no path needs PowerShell quoting.
    script = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:ENVY_LNK); "
              "$s.TargetPath = $env:ENVY_EXE; $s.Arguments = $env:ENVY_ARGS; $s.WorkingDirectory = $env:ENVY_DIR; "
              "$s.Description = $env:ENVY_DESC; $s.Save()")
    env = dict(os.environ, ENVY_LNK=str(lnk), ENVY_EXE=program, ENVY_ARGS=args, ENVY_DIR=str(exe.parent),
               ENVY_DESC=app.summary)
    proc = run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
               env=env, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0 or not lnk.exists():
        raise ToolsError(f"couldn't create the Start Menu shortcut: {(proc.stderr or '').strip()[-200:]}")
    return lnk


def create(app: App, folder: Path, paths: Paths, os_name: str, run=subprocess.run) -> list[str]:
    """Launchers for the app installed in `folder`. Returns every file made."""
    made: list[Path] = []
    exe = folder / app.exe_name(os_name)
    target = launch_target(app, folder, os_name)
    if os_name == "windows":
        made.append(windows_shortcut(app, target, paths.menu_dir, run))
        return [str(p) for p in made]
    if paths.bin_dir is not None:
        command = paths.bin_dir / app.bundle
        made.append(_wrapper(target, command) if app.start(os_name) else _link(exe, command))
        if app.setup(os_name):
            made.append(_wrapper(folder / app.setup(os_name), paths.bin_dir / f"{app.bundle}-setup"))
    menu_target = cli_helper(app, exe) if app.cli else target
    if os_name == "macos":
        made.append(macos_app(app, menu_target, paths.menu_dir))
    else:
        made.append(linux_desktop(app, menu_target, paths.menu_dir))
    return [str(p) for p in made]


def remove(paths: list[str]) -> None:
    for p in map(Path, paths):
        try:
            if p.is_symlink() or p.is_file():
                p.unlink()
            elif p.is_dir():
                shutil.rmtree(p)
        except OSError:
            pass
