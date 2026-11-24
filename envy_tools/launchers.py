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
    keywords = "ENVY;" + ("OSINT;recon;" if app.slug == "recon" else "VPN;WireGuard;" if app.slug == "tunnel"
                          else "encrypted;offline;")
    text = ("[Desktop Entry]\nType=Application\n"
            f"Name={app.name}\nComment={app.summary}\nExec={exec_line}\n"
            f"Terminal={'true' if app.kind == 'terminal' else 'false'}\n"
            f"Categories=Utility;Security;\nKeywords={keywords}\n")
    return _write(menu_dir / f"{app.bundle}.desktop", text)


def windows_shortcut(app: App, exe: Path, menu_dir: Path, run=subprocess.run) -> Path:
    menu_dir.mkdir(parents=True, exist_ok=True)
    lnk = menu_dir / f"{app.name}.lnk"
    # Values travel in environment variables, so no path needs PowerShell quoting.
    script = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:ENVY_LNK); "
              "$s.TargetPath = $env:ENVY_EXE; $s.WorkingDirectory = $env:ENVY_DIR; "
              "$s.Description = $env:ENVY_DESC; $s.Save()")
    env = dict(os.environ, ENVY_LNK=str(lnk), ENVY_EXE=str(exe), ENVY_DIR=str(exe.parent),
               ENVY_DESC=app.summary)
    proc = run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
               env=env, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0 or not lnk.exists():
        raise ToolsError(f"couldn't create the Start Menu shortcut: {(proc.stderr or '').strip()[-200:]}")
    return lnk


def create(app: App, exe: Path, paths: Paths, os_name: str, run=subprocess.run) -> list[str]:
    made: list[Path] = []
    if os_name == "windows":
        made.append(windows_shortcut(app, exe, paths.menu_dir, run))
    else:
        if paths.bin_dir is not None:
            made.append(_link(exe, paths.bin_dir / app.bundle))
        if os_name == "macos":
            made.append(macos_app(app, exe, paths.menu_dir))
        else:
            made.append(linux_desktop(app, exe, paths.menu_dir))
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
