"""Install, update, open and remove ENVY apps from their signed GitHub releases.

Installing one app:

1. Ask GitHub for the app's latest release tag.
2. Download that release's ``SHA256SUMS.txt`` and pick the bundle built for this OS.
3. If ``cosign`` is installed, verify the Sigstore signature on ``SHA256SUMS.txt``
   (required with ``require_signature``).
4. Download the bundle and check its SHA-256. A mismatch stops everything.
5. Unpack into a temporary folder (refusing any path that would escape it), then
   move it into place, so a failed install never leaves half an app.
6. Add launchers, record everything in ``installed.json``, delete the old version.

No administrator rights are used: everything lives in the user's own folders.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import catalog as cat
from . import launchers
from .catalog import App, ToolsError
from .net import Http, parse_sums


@dataclass
class Installed:
    slug: str
    version: str
    folder: str
    exe: str
    launchers: list = field(default_factory=list)
    installed_at: float = 0.0


@dataclass
class Row:
    """What the list/GUI shows for one app."""
    app: App
    installed: Installed | None = None
    latest: str | None = None
    elsewhere: str | None = None     # found on PATH but not installed by ENVY Tools

    @property
    def update_available(self) -> bool:
        return bool(self.installed and self.latest and _vkey(self.latest) > _vkey(self.installed.version))


def _vkey(tag: str):
    parts = []
    for p in tag.lstrip("v").replace("-", ".").split("."):
        parts.append((0, int(p)) if p.isdigit() else (-1, p))
    return parts


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _inside(base: Path, target: Path) -> bool:
    base = base.resolve()
    try:
        target.resolve().relative_to(base)
        return True
    except ValueError:
        return False


def safe_extract(archive: Path, dest: Path) -> None:
    """Unpack a .tar.gz or .zip, refusing absolute paths, '..', and links that leave dest."""
    dest.mkdir(parents=True, exist_ok=True)
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                name = info.filename.replace("\\", "/")
                if name.startswith("/") or ".." in name.split("/") or ":" in name.split("/")[0]:
                    raise ToolsError(f"unsafe path in download: {info.filename}")
                if not _inside(dest, dest / name):
                    raise ToolsError(f"unsafe path in download: {info.filename}")
            zf.extractall(dest)
        return
    with tarfile.open(archive, "r:gz") as tf:
        members = tf.getmembers()
        for m in members:
            name = m.name
            if name.startswith("/") or ".." in name.split("/"):
                raise ToolsError(f"unsafe path in download: {name}")
            if m.isdev():
                raise ToolsError(f"unexpected device file in download: {name}")
            if m.issym() or m.islnk():
                link = m.linkname
                origin = (dest / name).parent if m.issym() else dest
                if link.startswith("/") or not _inside(dest, origin / link):
                    raise ToolsError(f"link escapes the app folder: {name} -> {link}")
        if hasattr(tarfile, "data_filter"):
            tf.extractall(dest, members=members, filter="data")
        else:  # pragma: no cover - Python < 3.11.4; members were checked above
            tf.extractall(dest, members=members)


class Installer:
    def __init__(self, paths: launchers.Paths | None = None, http=None, log=print,
                 os_name: str | None = None, arch: str | None = None,
                 run=subprocess.run, which=shutil.which):
        host_os, host_arch = cat.host()
        self.os_name = os_name or host_os
        self.arch = arch or host_arch
        self.paths = paths or launchers.Paths.default(self.os_name)
        self.log = log
        self.http = http or Http(log=lambda m: self.log(m))
        self.run = run
        self.which = which
        self._catalog: list[App] | None = None

    # ----- catalog -----
    def apps(self, refresh: bool = False, online: bool = True) -> list[App]:
        if self._catalog is None or refresh:
            remote = None
            if online:
                try:
                    remote = cat.parse(self.http.get_text(cat.REMOTE_CATALOG, limit=200_000))
                except ToolsError as exc:
                    self.log(f"(using the built-in app list: {exc})")
            self._catalog = cat.merged(remote)
        return self._catalog

    # ----- installed.json -----
    def installed(self) -> dict[str, Installed]:
        try:
            raw = json.loads(self.paths.manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        out = {}
        for slug, rec in (raw.get("apps") or {}).items():
            try:
                out[slug] = Installed(**rec)
            except TypeError:
                continue
        return out

    def _save(self, records: dict[str, Installed]) -> None:
        self.paths.home.mkdir(parents=True, exist_ok=True)
        data = {"format": 1, "apps": {k: vars(v) for k, v in sorted(records.items())}}
        tmp = self.paths.manifest.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.paths.manifest)

    # ----- status -----
    def rows(self, check_latest: bool = False, apps: list[App] | None = None, online: bool = True) -> list[Row]:
        records = self.installed()
        rows = []
        for app in apps or self.apps(online=online):
            row = Row(app, records.get(app.slug))
            if row.installed is None:
                found = self.which(app.bundle)
                if found:
                    row.elsewhere = found
            if check_latest:
                try:
                    row.latest = self.http.latest_tag(app.repo)
                except ToolsError as exc:
                    self.log(f"{app.slug}: {exc}")
            rows.append(row)
        return rows

    # ----- install -----
    def _verify_signature(self, app: App, sums: Path, sig: Path, require: bool) -> None:
        cosign = self.which("cosign")
        if not cosign:
            if require:
                raise ToolsError("--require-signature needs cosign installed (https://docs.sigstore.dev)")
            self.log("  signature: cosign not installed, so the Sigstore signature wasn't checked "
                     "(the SHA-256 check still runs)")
            return
        argv = [cosign, "verify-blob",
                "--certificate-identity-regexp",
                f"^https://github.com/{cat.OWNER}/{app.repo}/.github/workflows/release.yml@.*",
                "--certificate-oidc-issuer", "https://token.actions.githubusercontent.com",
                "--bundle", str(sig), str(sums)]
        self.log("  ↳ " + " ".join(argv))
        proc = self.run(argv, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            raise ToolsError(f"Sigstore signature check FAILED for {app.name}; nothing was installed. "
                             f"{(proc.stderr or '').strip()[-300:]}")
        self.log("  signature: verified (Sigstore, built by this repo's release workflow)")

    def install(self, app: App, progress=None, require_signature: bool = False, tag: str | None = None) -> Installed:
        tag = tag or self.http.latest_tag(app.repo)
        current = self.installed().get(app.slug)
        if current and current.version == tag and Path(current.exe).exists():
            self.log(f"{app.name} {tag} is already installed.")
            return current
        self.log(f"Installing {app.name} {tag} for {self.os_name} {self.arch}")
        base = f"https://github.com/{cat.OWNER}/{app.repo}/releases/download/{tag}"
        self.paths.apps.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.paths.apps, prefix=".download-") as tmpdir:
            tmp = Path(tmpdir)
            sums_text = self.http.get_text(f"{base}/SHA256SUMS.txt")
            sums = parse_sums(sums_text)
            asset = cat.pick_asset(app, tag, list(sums), self.os_name, self.arch)
            (tmp / "SHA256SUMS.txt").write_text(sums_text, encoding="utf-8")
            if self.which("cosign"):
                sig = tmp / "SHA256SUMS.txt.sigstore.json"
                sig.write_text(self.http.get_text(f"{base}/SHA256SUMS.txt.sigstore.json"), encoding="utf-8")
                self._verify_signature(app, tmp / "SHA256SUMS.txt", sig, require_signature)
            else:
                self._verify_signature(app, tmp / "SHA256SUMS.txt", tmp / "missing", require_signature)
            archive = tmp / asset
            self.http.download(f"{base}/{asset}", archive, progress)
            digest = sha256_file(archive)
            if digest != sums[asset]:
                raise ToolsError(f"checksum MISMATCH for {asset}: expected {sums[asset]}, got {digest}. "
                                 "The download was deleted and nothing was installed.")
            self.log(f"  checksum OK  {digest[:16]}…  {asset}")
            unpack = tmp / "unpacked"
            safe_extract(archive, unpack)
            stem = asset.removesuffix(".tar.gz").removesuffix(".zip")
            folder = unpack / stem
            exe_name = app.exe_name(self.os_name)
            if not (folder / exe_name).is_file():
                raise ToolsError(f"{asset} doesn't contain {stem}/{exe_name}; nothing was installed")
            dest = self.paths.apps / app.slug / stem
            if dest.exists():
                shutil.rmtree(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(folder), str(dest))
        exe = dest / exe_name
        if self.os_name != "windows":
            exe.chmod(exe.stat().st_mode | 0o111)
        if current:
            launchers.remove(current.launchers)
        made = launchers.create(app, exe, self.paths, self.os_name, self.run)
        record = Installed(app.slug, tag, str(dest), str(exe), made, time.time())
        records = self.installed()
        records[app.slug] = record
        self._save(records)
        for old in (self.paths.apps / app.slug).iterdir():
            if old != dest and not old.name.startswith("."):
                shutil.rmtree(old, ignore_errors=True)
        self.log(f"✓ {app.name} {tag} installed")
        return record

    def update(self, apps: list[App], progress=None, require_signature: bool = False) -> list[str]:
        records = self.installed()
        changed = []
        for app in apps:
            if app.slug not in records:
                continue
            tag = self.http.latest_tag(app.repo)
            if tag == records[app.slug].version and Path(records[app.slug].exe).exists():
                self.log(f"{app.name} is up to date ({tag}).")
                continue
            self.install(app, progress, require_signature, tag)
            changed.append(app.slug)
        return changed

    def remove(self, app: App) -> None:
        records = self.installed()
        rec = records.pop(app.slug, None)
        if rec is None:
            raise ToolsError(f"{app.name} wasn't installed by ENVY Tools")
        launchers.remove(rec.launchers)
        shutil.rmtree(self.paths.apps / app.slug, ignore_errors=True)
        self._save(records)
        self.log(f"Removed {app.name}.")

    # ----- open -----
    def open_argv(self, app: App) -> list[str]:
        rec = self.installed().get(app.slug)
        if rec is None or not Path(rec.exe).exists():
            raise ToolsError(f"{app.name} isn't installed")
        exe = rec.exe
        if app.kind == "terminal":
            if self.os_name == "macos":
                return ["open", "-a", "Terminal", exe]
            if self.os_name == "windows":
                return ["cmd", "/c", "start", "", exe]
            for term, flag in (("x-terminal-emulator", "-e"), ("gnome-terminal", "--"), ("konsole", "-e"),
                               ("xfce4-terminal", "-x"), ("xterm", "-e")):
                found = self.which(term)
                if found:
                    return [found, flag, exe]
            raise ToolsError(f"{app.name} runs in a terminal; open one and run {exe}")
        return [exe]

    def open(self, app: App, popen=subprocess.Popen) -> None:
        argv = self.open_argv(app)
        self.log("↳ " + " ".join(argv))
        kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if sys.platform.startswith("win"):
            kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | \
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        popen(argv, **kwargs)
