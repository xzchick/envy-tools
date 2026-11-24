import hashlib
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from envy_tools import catalog as cat
from envy_tools import launchers
from envy_tools.catalog import App, ToolsError
from envy_tools.installer import Installer, safe_extract
from envy_tools.net import _check_url, parse_sums

DEMO = App("demo", "ENVY Demo", "envy-demo", "envy-demo", "A demo app", "gui", False)
TERM = App("termy", "ENVY Termy", "envy-termy", "envy-termy", "A terminal app", "terminal", True)


def make_bundle(app, tag, os_name, arch="x86_64", extra=None):
    """A real release-shaped archive: <bundle>-<tag>-<os>-<arch>/<exe> (+ a data file)."""
    stem = f"{app.bundle}-{tag}-{os_name}-{arch}"
    exe = app.exe_name(os_name)
    script = b"#!/bin/sh\necho demo " + tag.encode() + b"\n"
    buf = io.BytesIO()
    if os_name == "windows":
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(f"{stem}/{exe}", script)
            zf.writestr(f"{stem}/_internal/data.txt", b"x")
            for name, data in (extra or {}).items():
                zf.writestr(name, data)
        return stem + ".zip", buf.getvalue()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data, mode in ((f"{stem}/{exe}", script, 0o755), (f"{stem}/_internal/data.txt", b"x", 0o644)):
            info = tarfile.TarInfo(name); info.size = len(data); info.mode = mode
            tf.addfile(info, io.BytesIO(data))
        for name, data in (extra or {}).items():
            info = tarfile.TarInfo(name); info.size = len(data); info.mode = 0o644
            tf.addfile(info, io.BytesIO(data))
    return stem + ".tar.gz", buf.getvalue()


class FakeHttp:
    """Serves releases from memory, like GitHub would."""

    def __init__(self):
        self.releases = {}      # repo -> (tag, {asset: bytes})
        self.calls = []
        self.catalog_text = None

    def publish(self, app, tag, assets, tamper=None):
        files = dict(assets)
        sums = "".join(f"{hashlib.sha256(d).hexdigest()}  {n}\n" for n, d in files.items())
        if tamper:
            files[tamper] = files[tamper] + b"tampered"
        files["SHA256SUMS.txt"] = sums.encode()
        files["SHA256SUMS.txt.sigstore.json"] = b"{}"
        self.releases[app.repo] = (tag, files)

    def latest_tag(self, repo):
        self.calls.append(("latest", repo))
        if repo not in self.releases:
            raise ToolsError(f"{repo} has no published release yet")
        return self.releases[repo][0]

    def _file(self, url):
        parts = url.split("/")
        repo, tag, name = parts[4], parts[-2], parts[-1]
        rel_tag, files = self.releases[repo]
        assert tag == rel_tag, (tag, rel_tag)
        return files[name]

    def get_text(self, url, limit=0):
        self.calls.append(("get", url))
        if url == cat.REMOTE_CATALOG:
            if self.catalog_text is None:
                raise ToolsError("GitHub answered 404")
            return self.catalog_text
        return self._file(url).decode()

    def download(self, url, dest, progress=None):
        self.calls.append(("download", url))
        data = self._file(url)
        Path(dest).write_bytes(data)
        if progress:
            progress(len(data), len(data))


def new_installer(tmp, os_name="linux", arch="x86_64", http=None, run=None, which=None):
    paths = launchers.Paths(Path(tmp) / "home", Path(tmp) / "bin", Path(tmp) / "menu")
    logs = []
    inst = Installer(paths=paths, http=http or FakeHttp(), log=logs.append, os_name=os_name, arch=arch,
                     run=run or (lambda *a, **k: None), which=which or (lambda _n: None))
    inst.logs = logs
    return inst


@unittest.skipIf(os.name == "nt", "symlinked launchers are POSIX; Windows is covered separately")
class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.http = FakeHttp()
        self.inst = new_installer(self.tmp, http=self.http)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_install_verifies_unpacks_and_links(self):
        self.http.publish(DEMO, "v1.0.0", [make_bundle(DEMO, "v1.0.0", "linux")])
        rec = self.inst.install(DEMO)
        self.assertEqual(rec.version, "v1.0.0")
        exe = Path(rec.exe)
        self.assertTrue(exe.is_file() and os.access(exe, os.X_OK))
        self.assertTrue((Path(self.tmp) / "bin" / "envy-demo").is_symlink())
        desktop = (Path(self.tmp) / "menu" / "envy-demo.desktop").read_text()
        self.assertIn("Terminal=false", desktop)
        self.assertIn(str(exe), desktop)
        self.assertIn("checksum OK", "\n".join(self.inst.logs))
        self.assertEqual(list(self.inst.installed()), ["demo"])
        # No temporary download folders are left behind.
        self.assertEqual([p.name for p in (Path(self.tmp) / "home" / "apps").iterdir()], ["demo"])

    def test_checksum_mismatch_installs_nothing(self):
        name, data = make_bundle(DEMO, "v1.0.0", "linux")
        self.http.publish(DEMO, "v1.0.0", [(name, data)], tamper=name)
        with self.assertRaisesRegex(ToolsError, "MISMATCH"):
            self.inst.install(DEMO)
        self.assertEqual(self.inst.installed(), {})
        self.assertFalse((Path(self.tmp) / "bin" / "envy-demo").exists())
        self.assertEqual(list((Path(self.tmp) / "home" / "apps").iterdir()), [])

    def test_missing_platform_build_is_explained(self):
        self.http.publish(DEMO, "v1.0.0", [make_bundle(DEMO, "v1.0.0", "macos", "arm64")])
        with self.assertRaisesRegex(ToolsError, "no build for linux x86_64"):
            self.inst.install(DEMO)

    def test_update_replaces_old_version_and_its_files(self):
        self.http.publish(DEMO, "v1.0.0", [make_bundle(DEMO, "v1.0.0", "linux")])
        old = self.inst.install(DEMO)
        self.http.publish(DEMO, "v1.1.0", [make_bundle(DEMO, "v1.1.0", "linux")])
        self.assertEqual(self.inst.update([DEMO]), ["demo"])
        new = self.inst.installed()["demo"]
        self.assertEqual(new.version, "v1.1.0")
        self.assertFalse(Path(old.folder).exists())
        self.assertEqual(os.readlink(Path(self.tmp) / "bin" / "envy-demo"), new.exe)
        self.assertEqual(self.inst.update([DEMO]), [])  # already current

    def test_reinstall_same_version_is_a_no_op(self):
        self.http.publish(DEMO, "v1.0.0", [make_bundle(DEMO, "v1.0.0", "linux")])
        self.inst.install(DEMO)
        downloads = [c for c in self.http.calls if c[0] == "download"]
        self.inst.install(DEMO)
        self.assertEqual(downloads, [c for c in self.http.calls if c[0] == "download"])

    def test_remove_deletes_app_and_launchers_only(self):
        self.http.publish(DEMO, "v1.0.0", [make_bundle(DEMO, "v1.0.0", "linux")])
        self.inst.install(DEMO)
        keep = Path(self.tmp) / "bin" / "someone-elses-tool"
        keep.write_text("x")
        self.inst.remove(DEMO)
        self.assertEqual(self.inst.installed(), {})
        self.assertFalse((Path(self.tmp) / "bin" / "envy-demo").exists())
        self.assertFalse((Path(self.tmp) / "menu" / "envy-demo.desktop").exists())
        self.assertTrue(keep.exists())
        with self.assertRaises(ToolsError):
            self.inst.remove(DEMO)

    def test_terminal_app_gets_terminal_entry_and_open_uses_a_terminal(self):
        self.http.publish(TERM, "v0.1.0", [make_bundle(TERM, "v0.1.0", "linux")])
        self.inst.which = lambda n: "/usr/bin/xterm" if n == "xterm" else None
        self.inst.install(TERM)
        self.assertIn("Terminal=true", (Path(self.tmp) / "menu" / "envy-termy.desktop").read_text())
        argv = self.inst.open_argv(TERM)
        self.assertEqual(argv[:2], ["/usr/bin/xterm", "-e"])
        started = []
        self.inst.open(TERM, popen=lambda argv, **kw: started.append((argv, kw)))
        self.assertTrue(started and started[0][1].get("start_new_session"))

    def test_macos_install_makes_an_app_bundle(self):
        inst = new_installer(self.tmp, os_name="macos", arch="arm64", http=self.http)
        self.http.publish(TERM, "v0.1.0", [make_bundle(TERM, "v0.1.0", "macos", "arm64")])
        rec = inst.install(TERM)
        app = Path(self.tmp) / "menu" / "ENVY Termy.app"
        script = (app / "Contents" / "MacOS" / "launch").read_text()
        self.assertIn("open -a Terminal", script)
        self.assertIn(rec.exe, script)
        import plistlib
        info = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
        self.assertEqual(info["CFBundleExecutable"], "launch")
        self.assertEqual(inst.open_argv(TERM), ["open", "-a", "Terminal", rec.exe])
        inst.remove(TERM)
        self.assertFalse(app.exists())

    def test_cosign_signature_checked_when_available_and_required_when_asked(self):
        self.http.publish(DEMO, "v1.0.0", [make_bundle(DEMO, "v1.0.0", "linux")])
        calls = []

        class P:
            returncode = 1
            stderr = "bad signature"

        self.inst.which = lambda n: "/usr/bin/cosign" if n == "cosign" else None
        self.inst.run = lambda argv, **kw: (calls.append(argv), P())[1]
        with self.assertRaisesRegex(ToolsError, "signature check FAILED"):
            self.inst.install(DEMO)
        self.assertIn("verify-blob", calls[0])
        self.assertTrue(any("envy-demo/.github/workflows/release.yml" in a for a in calls[0]))
        self.assertEqual(self.inst.installed(), {})
        self.inst.which = lambda _n: None
        with self.assertRaisesRegex(ToolsError, "needs cosign"):
            self.inst.install(DEMO, require_signature=True)

    def test_rows_show_installed_updates_and_found_elsewhere(self):
        self.http.publish(DEMO, "v1.0.0", [make_bundle(DEMO, "v1.0.0", "linux")])
        self.inst.install(DEMO)
        self.http.publish(DEMO, "v2.0.0", [make_bundle(DEMO, "v2.0.0", "linux")])
        self.http.publish(TERM, "v0.1.0", [])
        self.inst.which = lambda n: "/opt/envy-termy" if n == "envy-termy" else None
        rows = {r.app.slug: r for r in self.inst.rows(check_latest=True, apps=[DEMO, TERM])}
        self.assertTrue(rows["demo"].update_available)
        self.assertEqual(rows["termy"].elsewhere, "/opt/envy-termy")
        self.assertFalse(rows["termy"].update_available)


class WindowsInstallTests(unittest.TestCase):
    def test_zip_install_and_start_menu_shortcut(self):
        tmp = tempfile.mkdtemp()
        try:
            http = FakeHttp()
            calls = []

            def fake_run(argv, env=None, **kw):
                calls.append((argv, env))
                Path(env["ENVY_LNK"]).write_text("lnk")

                class P:
                    returncode = 0
                    stderr = ""
                return P()

            paths = launchers.Paths(Path(tmp) / "home", None, Path(tmp) / "menu")
            inst = Installer(paths=paths, http=http, log=lambda _m: None, os_name="windows", arch="arm64",
                             run=fake_run, which=lambda _n: None)
            http.publish(DEMO, "v1.0.0", [make_bundle(DEMO, "v1.0.0", "windows", "x86_64")])
            rec = inst.install(DEMO)   # Windows on Arm falls back to the x86_64 build
            self.assertTrue(rec.exe.endswith("envy-demo.exe"))
            argv, env = calls[0]
            self.assertEqual(argv[0], "powershell")
            self.assertEqual(env["ENVY_EXE"], rec.exe)
            self.assertTrue(env["ENVY_LNK"].endswith("ENVY Demo.lnk"))
            inst.remove(DEMO)
            self.assertFalse((Path(tmp) / "menu" / "ENVY Demo.lnk").exists())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class ExtractSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _tar(self, members):
        path = self.tmp / "x.tar.gz"
        with tarfile.open(path, "w:gz") as tf:
            for info, data in members:
                tf.addfile(info, io.BytesIO(data) if data is not None else None)
        return path

    def test_rejects_parent_paths(self):
        info = tarfile.TarInfo("../evil"); info.size = 1
        with self.assertRaisesRegex(ToolsError, "unsafe path"):
            safe_extract(self._tar([(info, b"x")]), self.tmp / "out")
        self.assertFalse((self.tmp / "evil").exists())

    def test_rejects_absolute_paths(self):
        info = tarfile.TarInfo("/etc/evil"); info.size = 1
        with self.assertRaisesRegex(ToolsError, "unsafe path"):
            safe_extract(self._tar([(info, b"x")]), self.tmp / "out")

    def test_rejects_links_that_escape(self):
        info = tarfile.TarInfo("app/link"); info.type = tarfile.SYMTYPE; info.linkname = "../../../etc/passwd"
        with self.assertRaisesRegex(ToolsError, "escapes"):
            safe_extract(self._tar([(info, None)]), self.tmp / "out")

    @unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
    def test_allows_links_inside_the_bundle(self):
        a = tarfile.TarInfo("app/Versions/A/lib"); a.size = 1
        link = tarfile.TarInfo("app/Versions/Current"); link.type = tarfile.SYMTYPE; link.linkname = "A"
        safe_extract(self._tar([(a, b"x"), (link, None)]), self.tmp / "out")
        self.assertTrue((self.tmp / "out" / "app" / "Versions" / "Current" / "lib").exists())

    def test_rejects_zip_traversal(self):
        path = self.tmp / "x.zip"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("../evil.exe", b"x")
        with self.assertRaisesRegex(ToolsError, "unsafe path"):
            safe_extract(path, self.tmp / "out")


class CatalogTests(unittest.TestCase):
    def test_builtin_catalog_lists_all_thirteen(self):
        apps = cat.builtin()
        self.assertEqual(len(apps), 13)
        self.assertIn("tunnel", {a.slug for a in apps})
        crypto = next(a for a in apps if a.slug == "crypto-workbench")
        self.assertEqual(crypto.bundle, "envy-crypto")

    def test_remote_catalog_adds_apps_but_cannot_point_elsewhere(self):
        good = json.dumps({"format": 1, "apps": [{"slug": "new", "name": "ENVY New", "repo": "envy-new",
                                                  "bundle": "envy-new", "summary": "A new app"}]})
        merged = cat.merged(cat.parse(good))
        self.assertEqual(len(merged), 14)
        for evil in ({"repo": "evil/envy-x"}, {"repo": "../../x"}, {"bundle": "rm -rf"},
                     {"kind": "daemon"}, {"slug": "Bad Slug"}):
            entry = {"slug": "x", "name": "X", "repo": "envy-x", "bundle": "envy-x", "summary": "s"}
            entry.update(evil)
            with self.assertRaises(ToolsError):
                cat.parse(json.dumps({"format": 1, "apps": [entry]}))
        with self.assertRaises(ToolsError):
            cat.parse("not json")

    def test_find_and_resolve(self):
        apps = cat.builtin()
        self.assertEqual([a.slug for a in cat.find(apps, "wireguard vpn")], ["tunnel"])
        self.assertIn("vault", {a.slug for a in cat.find(apps, "encryption")})
        self.assertEqual([a.slug for a in cat.resolve(apps, ["envy-vault", "Veil"])], ["vault", "veil"])
        self.assertEqual(len(cat.resolve(apps, ["all"])), 13)
        with self.assertRaisesRegex(ToolsError, "unknown app"):
            cat.resolve(apps, ["nope"])

    def test_short_names(self):
        apps = cat.builtin()
        slugs = lambda *names: [a.slug for a in cat.resolve(apps, list(names))]
        # a unique word or prefix picks the app
        self.assertEqual(slugs("integrity"), ["integrity-monitor"])
        self.assertEqual(slugs("crypto", "envy-crypto"), ["crypto-workbench"])  # bundle name too; no duplicates
        self.assertEqual(slugs("evidence", "hypo", "ops", "sky"), ["evidence-vault", "hypothesis", "ops-desk", "skywatch"])
        # spaces, underscores and case are forgiven
        self.assertEqual(slugs("Ops Desk", "evidence_vault", "ENVY-Integrity-Monitor"),
                         ["ops-desk", "evidence-vault", "integrity-monitor"])
        # an exact name always wins over a word match ("vault" is not Evidence Vault)
        self.assertEqual(slugs("vault"), ["vault"])
        self.assertEqual(slugs("ALL")[:1], ["vault"])
        # ambiguous names list the choices instead of guessing
        self.assertEqual(slugs("orb", "ora"), ["orbit", "oracle"])
        with self.assertRaisesRegex(ToolsError, r"could be vault, evidence-vault|could be evidence-vault, vault"):
            cat.resolve(apps, ["vau"])
        # too short to guess from
        with self.assertRaisesRegex(ToolsError, "unknown app"):
            cat.resolve(apps, ["or"])

    def test_offline_falls_back_to_builtin(self):
        tmp = tempfile.mkdtemp()
        try:
            inst = new_installer(tmp)
            self.assertEqual(len(inst.apps()), 13)
            self.assertIn("built-in app list", "\n".join(inst.logs))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_asset_picking(self):
        names = ["envy-demo-v1.0.0-linux-x86_64.tar.gz", "envy-demo-v1.0.0-macos-arm64.tar.gz",
                 "envy-demo-v1.0.0-windows-x86_64.zip", "SHA256SUMS.txt"]
        self.assertEqual(cat.pick_asset(DEMO, "v1.0.0", names, "macos", "arm64"), names[1])
        self.assertEqual(cat.pick_asset(DEMO, "v1.0.0", names, "windows", "arm64"), names[2])
        with self.assertRaisesRegex(ToolsError, "Intel Macs"):
            cat.pick_asset(DEMO, "v1.0.0", names, "macos", "x86_64")
        with self.assertRaisesRegex(ToolsError, "linux arm64"):
            cat.pick_asset(DEMO, "v1.0.0", names, "linux", "arm64")


class NetTests(unittest.TestCase):
    def test_only_github_https(self):
        _check_url("https://github.com/xzchick/envy-vault/releases/latest")
        _check_url("https://objects.githubusercontent.com/x")
        for bad in ("http://github.com/x", "https://evil.example/x", "https://github.com.evil.example/x",
                    "file:///etc/passwd"):
            with self.assertRaises(ToolsError):
                _check_url(bad)

    def test_parse_sums(self):
        text = "a" * 64 + "  envy-x.tar.gz\n" + "B" * 64 + " *envy-y.zip\njunk line\n"
        self.assertEqual(parse_sums(text), {"envy-x.tar.gz": "a" * 64, "envy-y.zip": "b" * 64})

    def test_tls_context_verifies(self):
        import ssl
        from envy_tools.net import _context
        ctx = _context()
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertGreater(ctx.cert_store_stats()["x509_ca"], 50)


class PathsTests(unittest.TestCase):
    def test_defaults_per_os(self):
        with mock.patch.dict(os.environ, {"ENVY_TOOLS_HOME": ""}):
            mac = launchers.Paths.default("macos")
            self.assertTrue(str(mac.menu_dir).endswith("Applications"))
            win = launchers.Paths.default("windows")
            self.assertIsNone(win.bin_dir)
            self.assertTrue(str(win.menu_dir).endswith(os.path.join("Programs", "ENVY")))
            lin = launchers.Paths.default("linux")
            self.assertTrue(str(lin.menu_dir).endswith("applications"))

    def test_env_override_keeps_everything_inside(self):
        with mock.patch.dict(os.environ, {"ENVY_TOOLS_HOME": "/tmp/et"}):
            p = launchers.Paths.default("linux")
            self.assertEqual(p.bin_dir, Path("/tmp/et/bin"))
            self.assertEqual(p.menu_dir, Path("/tmp/et/menu"))


if __name__ == "__main__":
    unittest.main()
