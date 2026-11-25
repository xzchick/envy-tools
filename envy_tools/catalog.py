"""The list of ENVY apps, and which release bundle fits this computer.

A copy of the catalog ships inside ENVY Tools, so it works offline. When online it
also reads the latest catalog from this project's GitHub repository, so new ENVY
apps show up without updating ENVY Tools. Every entry is validated, and a remote
catalog can only ever point at repositories owned by ``OWNER``.
"""

from __future__ import annotations

import json
import platform as _platform
import re
import sys
from dataclasses import dataclass
from importlib import resources

OWNER = "xzchick"
REMOTE_CATALOG = f"https://raw.githubusercontent.com/{OWNER}/envy-tools/main/envy_tools/catalog.json"

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_REPO = re.compile(r"^envy-[a-z0-9-]{1,40}$")
_BUNDLE = re.compile(r"^envy-[a-z0-9-]{1,40}$")
# Apps of the suite whose repository predates the envy- prefix. Still owner-locked.
EXTRA_REPOS = frozenset({"hash-audit"})
# A path inside an app's bundle folder: plain names joined by "/", never "..".
_REL = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*(/[A-Za-z0-9_][A-Za-z0-9._-]*)*$")


class ToolsError(Exception):
    """A problem worth showing the user as-is."""


@dataclass(frozen=True)
class App:
    slug: str
    name: str
    repo: str
    bundle: str
    summary: str
    kind: str = "gui"          # gui | terminal
    network: bool = False
    exe: str = ""              # program inside the bundle folder; default: the bundle name
    start_posix: str = ""      # launcher script that must start the app (macOS/Linux)
    start_windows: str = ""    # …and on Windows (a .ps1)
    setup_posix: str = ""      # one-time setup script, offered after install
    setup_windows: str = ""
    cli: bool = False          # a one-shot command: menu entries open a shell showing its help
    needs: str = ""            # something the user installs separately, shown after install

    @property
    def repo_url(self) -> str:
        return f"https://github.com/{OWNER}/{self.repo}"

    def exe_name(self, os_name: str) -> str:
        return (self.exe or self.bundle) + (".exe" if os_name == "windows" else "")

    def start(self, os_name: str) -> str:
        return self.start_windows if os_name == "windows" else self.start_posix

    def setup(self, os_name: str) -> str:
        return self.setup_windows if os_name == "windows" else self.setup_posix


def _rel(raw: dict, key: str, slug: str) -> str:
    value = raw.get(key, "")
    if isinstance(value, dict):          # {"posix": ..., "windows": ...} is split by the caller
        raise ToolsError(f"bad {key} for {slug}")
    if value == "":
        return ""
    if not (isinstance(value, str) and len(value) <= 120 and _REL.match(value)
            and ".." not in value.split("/")):
        raise ToolsError(f"bad {key} path for {slug}: {value!r}")
    return value


def _per_os(raw: dict, key: str, slug: str) -> tuple[str, str]:
    value = raw.get(key)
    if value is None:
        return "", ""
    if not isinstance(value, dict) or set(value) - {"posix", "windows"}:
        raise ToolsError(f"bad {key} for {slug}")
    return _rel(value, "posix", slug), _rel(value, "windows", slug)


def _app(raw: dict) -> App:
    if not isinstance(raw, dict):
        raise ToolsError("catalog entry is not an object")
    slug, repo, bundle = raw.get("slug"), raw.get("repo"), raw.get("bundle")
    name, summary, kind = raw.get("name"), raw.get("summary", ""), raw.get("kind", "gui")
    if not (isinstance(slug, str) and _SLUG.match(slug)):
        raise ToolsError(f"bad app id in catalog: {slug!r}")
    if not (isinstance(repo, str) and (_REPO.match(repo) or repo in EXTRA_REPOS)):
        raise ToolsError(f"bad repository for {slug}: {repo!r}")
    if not (isinstance(bundle, str) and (_BUNDLE.match(bundle) or bundle in EXTRA_REPOS)):
        raise ToolsError(f"bad bundle name for {slug}: {bundle!r}")
    if not (isinstance(name, str) and 0 < len(name) <= 60 and name.isprintable()):
        raise ToolsError(f"bad name for {slug}")
    if not (isinstance(summary, str) and len(summary) <= 160 and summary.isprintable()):
        raise ToolsError(f"bad summary for {slug}")
    if kind not in ("gui", "terminal"):
        raise ToolsError(f"bad kind for {slug}: {kind!r}")
    needs = raw.get("needs", "")
    if not (isinstance(needs, str) and len(needs) <= 160 and needs.isprintable()):
        raise ToolsError(f"bad needs for {slug}")
    start_posix, start_windows = _per_os(raw, "start", slug)
    setup_posix, setup_windows = _per_os(raw, "setup", slug)
    return App(slug, name, repo, bundle, summary, kind, bool(raw.get("network", False)),
               exe=_rel(raw, "exe", slug), start_posix=start_posix, start_windows=start_windows,
               setup_posix=setup_posix, setup_windows=setup_windows,
               cli=bool(raw.get("cli", False)), needs=needs)


def parse(text: str) -> list[App]:
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ToolsError(f"catalog is not valid JSON: {exc}") from None
    if not isinstance(data, dict) or data.get("format") != 1 or not isinstance(data.get("apps"), list):
        raise ToolsError("unrecognised catalog format")
    # "apps_ext" holds apps that need ENVY Tools 0.1.2 or later (launcher scripts, a
    # program in a subfolder, older repository names). Earlier versions ignore the key,
    # so adding apps there never breaks them.
    ext = data.get("apps_ext", [])
    if not isinstance(ext, list):
        raise ToolsError("unrecognised catalog format")
    apps, seen = [], set()
    for raw in data["apps"] + ext:
        app = _app(raw)
        if app.slug in seen:
            raise ToolsError(f"duplicate app in catalog: {app.slug}")
        seen.add(app.slug)
        apps.append(app)
    return apps


def builtin() -> list[App]:
    return parse(resources.files("envy_tools").joinpath("catalog.json").read_text(encoding="utf-8"))


def merged(remote: list[App] | None) -> list[App]:
    """The built-in catalog, updated and extended by the remote one (if it loaded)."""
    apps = {a.slug: a for a in builtin()}
    for a in remote or []:
        apps[a.slug] = a
    return list(apps.values())


def find(apps: list[App], query: str) -> list[App]:
    """Apps whose id, name or summary contain every word of the query."""
    words = query.lower().split()
    return [a for a in apps if all(w in f"{a.slug} {a.name} {a.summary}".lower() for w in words)]


MIN_SHORT = 3  # shortest name that may match by prefix, so one letter never guesses


def _normalize(name: str) -> str:
    key = "-".join(name.lower().replace("_", " ").replace("-", " ").split())
    return key.removeprefix("envy-")


def resolve_one(apps: list[App], name: str) -> App:
    """Find one app by name. Exact names win ("vault" is Vault, not Evidence Vault);
    otherwise a unique prefix of the name or of one of its words ("integrity",
    "crypto", "ops", "evidence") picks the app. Ambiguous names list the choices."""
    key = _normalize(name)
    by = {a.slug: a for a in apps}
    if key in by:
        return by[key]
    if len(key) >= MIN_SHORT:
        hits = [a for a in apps
                if a.slug.startswith(key) or any(w.startswith(key) for w in a.slug.split("-"))]
        if len(hits) == 1:
            return hits[0]
        if hits:
            raise ToolsError(f"'{name}' could be {', '.join(a.slug for a in hits)}. Say which one.")
    hint = ", ".join(sorted(by))
    raise ToolsError(f"unknown app: {name}. Available: {hint}")


def resolve(apps: list[App], names: list[str]) -> list[App]:
    if [n.lower() for n in names] == ["all"]:
        return list(apps)
    out: list[App] = []
    for n in names:
        app = resolve_one(apps, n)
        if app not in out:
            out.append(app)
    return out


# ----- platform -----

def host() -> tuple[str, str]:
    """(os, arch) in the release asset naming: macos|windows|linux, arm64|x86_64."""
    if sys.platform == "darwin":
        os_name = "macos"
    elif sys.platform.startswith("win"):
        os_name = "windows"
    else:
        os_name = "linux"
    machine = _platform.machine().lower()
    arch = {"amd64": "x86_64", "x64": "x86_64", "x86_64": "x86_64",
            "arm64": "arm64", "aarch64": "arm64"}.get(machine, machine or "unknown")
    return os_name, arch


def bundle_candidates(os_name: str, arch: str) -> list[str]:
    """Architectures we may install, best first. Windows on Arm runs x86_64 builds."""
    if os_name == "windows" and arch == "arm64":
        return ["arm64", "x86_64"]
    return [arch]


def pick_asset(app: App, tag: str, available: list[str], os_name: str, arch: str) -> str:
    ext = ".zip" if os_name == "windows" else ".tar.gz"
    for a in bundle_candidates(os_name, arch):
        name = f"{app.bundle}-{tag}-{os_name}-{a}{ext}"
        if name in available:
            return name
    built = sorted(n for n in available if n.startswith(app.bundle + "-") and n.endswith((".zip", ".tar.gz")))
    have = ", ".join(n[len(f"{app.bundle}-{tag}-"):].split(".")[0] for n in built) or "none"
    if os_name == "macos" and arch == "x86_64":
        extra = " Intel Macs aren't built yet; you can install from source with pip."
    else:
        extra = " You can install it from source with pip instead."
    raise ToolsError(f"{app.name} {tag} has no build for {os_name} {arch} (builds: {have}).{extra}")
