import io
import os
import shutil
import tempfile
import time
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from test_core import DEMO, FakeHttp, make_bundle, new_installer  # noqa: E402

from envy_tools import cli  # noqa: E402
from envy_tools.catalog import host  # noqa: E402


def _wait(app, window, seconds=20):
    end = time.time() + seconds
    while window.worker is not None and time.time() < end:
        app.processEvents()
        time.sleep(0.02)
    app.processEvents()


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from envy_tools.gui import _build
        cls.QApplication, cls.Window = _build()
        cls.app = cls.QApplication.instance() or cls.QApplication([])

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os_name, arch = host()
        self.os_name, self.arch = os_name, arch
        self.http = FakeHttp()
        self.http.catalog_text = ('{"format": 1, "apps": [{"slug": "demo", "name": "ENVY Demo", '
                                  '"repo": "envy-demo", "bundle": "envy-demo", "summary": "A demo app"}]}')
        self.http.publish(DEMO, "v1.0.0", [make_bundle(DEMO, "v1.0.0", os_name, arch)])
        if os_name == "windows":
            def fake_run(argv, env=None, **kw):
                Path(env["ENVY_LNK"]).write_text("lnk")

                class P:
                    returncode = 0
                    stderr = ""
                return P()
            self.inst = new_installer(self.tmp, os_name, arch, self.http, run=fake_run)
            self.inst.paths.bin_dir = None
        else:
            self.inst = new_installer(self.tmp, os_name, arch, self.http)
        self.w = self.Window(self.inst, check_on_start=False)

    def tearDown(self):
        self.w.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def select(self, slug):
        for r in range(self.w.table.rowCount()):
            if self.w.table.item(r, 0).data(256) == slug:  # Qt.UserRole
                self.w.table.selectRow(r)
                self.app.processEvents()
                return
        self.fail(f"{slug} not listed")

    def test_lists_builtin_apps_before_any_network(self):
        self.assertEqual(self.w.table.rowCount(), 15)
        self.assertEqual(self.http.calls, [])

    def test_check_adds_remote_apps_and_versions(self):
        self.w.check(); _wait(self.app, self.w)
        self.assertEqual(self.w.table.rowCount(), 16)
        self.select("demo")
        self.assertIn("Available v1.0.0", self.w.table.item(self.w.table.currentRow(), 1).text())
        self.assertTrue(self.w.b_install.isEnabled())
        self.assertFalse(self.w.b_open.isEnabled())

    def test_install_then_remove_from_the_window(self):
        self.w.check(); _wait(self.app, self.w)
        self.select("demo")
        self.w.install(); _wait(self.app, self.w)
        self.assertIn("installed", self.w.status.text())
        self.select("demo")
        self.assertTrue(self.w.b_open.isEnabled())
        self.assertFalse(self.w.b_install.isEnabled())
        self.assertIn("Installed v1.0.0", self.w.table.item(self.w.table.currentRow(), 1).text())
        started = []
        with mock.patch.object(self.inst, "open", lambda app: started.append(app.slug)):
            self.w.open_app()
        self.assertEqual(started, ["demo"])
        self.w._confirm = lambda *a: True
        self.w.remove()
        self.assertEqual(self.inst.installed(), {})

    def test_update_flow(self):
        self.w.check(); _wait(self.app, self.w)
        self.select("demo"); self.w.install(); _wait(self.app, self.w)
        self.http.publish(DEMO, "v1.1.0", [make_bundle(DEMO, "v1.1.0", self.os_name, self.arch)])
        self.w.check(); _wait(self.app, self.w)
        self.assertEqual(self.w.b_all.text(), "Update all (1)")
        self.select("demo")
        self.assertTrue(self.w.b_update.isEnabled())
        self.w.update_all(); _wait(self.app, self.w)
        self.assertEqual(self.inst.installed()["demo"].version, "v1.1.0")

    def test_search_filters(self):
        self.w.search.setText("wireguard")
        self.assertEqual(self.w.table.rowCount(), 1)
        self.assertEqual(self.w.table.item(0, 0).text(), "ENVY Tunnel")
        self.w.search.setText("")
        self.assertEqual(self.w.table.rowCount(), 15)

    def test_errors_show_in_status(self):
        self.select("vault")  # not published in the fake GitHub
        self.w.install(); _wait(self.app, self.w)
        self.assertIn("no published release", self.w.status.text())


class CliTests(unittest.TestCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_version_and_offline_list(self):
        with self.assertRaises(SystemExit):
            self.run_cli("--version")
        tmp = tempfile.mkdtemp()
        try:
            with mock.patch.dict(os.environ, {"ENVY_TOOLS_HOME": tmp}):
                code, out, _ = self.run_cli("--offline", "list")
                self.assertEqual(code, 0)
                self.assertIn("tunnel", out)
                self.assertIn("[uses network]", out)
                code, out, _ = self.run_cli("--offline", "list", "steganography")
                self.assertEqual(code, 1)
                code, out, _ = self.run_cli("--offline", "list", "carrier")
                self.assertIn("veil", out)
                code, out, _ = self.run_cli("where")
                self.assertIn(tmp, out)
                code, _, err = self.run_cli("--offline", "install", "nope")
                self.assertEqual(code, 1)
                self.assertIn("unknown app", err)
                code, out, _ = self.run_cli("installed")
                self.assertIn("Nothing installed", out)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
