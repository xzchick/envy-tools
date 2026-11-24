"""envy-tools: find, install, update, open and remove ENVY apps.

With no command it opens the window. Every command works without it:

    envy-tools list [--check] [search words]
    envy-tools install vault veil | all [--require-signature]
    envy-tools update [names | all]
    envy-tools open vault
    envy-tools remove oracle
    envy-tools installed
    envy-tools where
"""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .catalog import ToolsError, find, resolve
from .installer import Installer


def _progress_printer():
    state = {"last": -1}

    def show(got, total):
        if not sys.stdout.isatty():
            return
        if total:
            pct = int(got * 100 / total)
            if pct != state["last"]:
                state["last"] = pct
                sys.stdout.write(f"\r  downloading… {pct:3d}%  ({got / 1e6:.1f} of {total / 1e6:.1f} MB)")
                sys.stdout.flush()
                if got >= total:
                    sys.stdout.write("\n")
        else:
            sys.stdout.write(f"\r  downloading… {got / 1e6:.1f} MB")
            sys.stdout.flush()
    return show


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="envy-tools", description="Find, install, update and remove ENVY apps.")
    p.add_argument("--version", action="version", version=f"envy-tools {__version__}")
    p.add_argument("--offline", action="store_true", help="use the built-in app list; no network")
    sub = p.add_subparsers(dest="command")
    sub.add_parser("gui", help="open the window (the default)")
    ls = sub.add_parser("list", help="every ENVY app, and which are installed")
    ls.add_argument("--check", action="store_true", help="also ask GitHub for each latest version")
    ls.add_argument("search", nargs="*", help="only apps matching these words")
    ins = sub.add_parser("install", help="install apps by name, or 'all'")
    ins.add_argument("names", nargs="+")
    ins.add_argument("--require-signature", action="store_true",
                     help="refuse to install unless cosign verifies the Sigstore signature")
    up = sub.add_parser("update", help="update installed apps to their latest release")
    up.add_argument("names", nargs="*", default=["all"])
    up.add_argument("--require-signature", action="store_true")
    rm = sub.add_parser("remove", aliases=["uninstall"], help="remove apps")
    rm.add_argument("names", nargs="+")
    op = sub.add_parser("open", help="start an installed app")
    op.add_argument("name")
    sub.add_parser("installed", help="list what ENVY Tools has installed")
    sub.add_parser("where", help="show where apps and launchers go")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command in (None, "gui"):
        from .gui import launch
        return launch()
    inst = Installer()
    online = not args.offline
    try:
        if args.command == "where":
            print(f"apps      {inst.paths.apps}")
            print(f"commands  {inst.paths.bin_dir or '(none: use the Start Menu)'}")
            print(f"menu      {inst.paths.menu_dir}")
            print(f"platform  {inst.os_name} {inst.arch}")
            return 0
        if args.command == "list":
            if args.offline and args.check:
                raise ToolsError("--check needs the network")
            quiet = Installer(paths=inst.paths, log=lambda _m: None)
            apps = quiet.apps(online=online)
            if args.search:
                apps = find(apps, " ".join(args.search))
                if not apps:
                    print("No ENVY app matches that.")
                    return 1
            for row in inst.rows(check_latest=args.check, apps=apps):
                if row.installed:
                    state = f"installed {row.installed.version}"
                    if row.update_available:
                        state += f" → {row.latest} available"
                elif row.elsewhere:
                    state = "on this computer (not via ENVY Tools)"
                else:
                    state = "—"
                if row.latest and not row.installed:
                    state += f"  latest {row.latest}"
                net = "  [uses network]" if row.app.network else ""
                print(f"  {row.app.slug:<18} {state:<34} {row.app.summary}{net}")
            return 0
        if args.command == "installed":
            recs = inst.installed()
            if not recs:
                print("Nothing installed yet. Try: envy-tools install vault")
            for slug, rec in recs.items():
                print(f"  {slug:<18} {rec.version:<8} {rec.exe}")
            return 0
        apps = inst.apps(online=online)
        if args.command == "install":
            for app in resolve(apps, args.names):
                inst.install(app, _progress_printer(), args.require_signature)
            return 0
        if args.command == "update":
            names = args.names if args.names != ["all"] else [s for s in inst.installed()]
            if not names:
                print("Nothing installed yet.")
                return 0
            changed = inst.update(resolve(apps, names), _progress_printer(), args.require_signature)
            print(f"Updated: {', '.join(changed)}" if changed else "Everything is up to date.")
            return 0
        if args.command in ("remove", "uninstall"):
            for app in resolve(apps, args.names):
                inst.remove(app)
            return 0
        if args.command == "open":
            inst.open(resolve(apps, [args.name])[0])
            return 0
    except ToolsError as exc:
        print(f"envy-tools: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopped. Nothing half-installed is left behind.", file=sys.stderr)
        return 130
    return 2
