"""Run the freshly built standalone ENVY Tools through a real install, on this OS.

1. ``--version`` and ``--offline list`` (the built-in app list ships in the bundle).
2. In a throwaway folder (``ENVY_TOOLS_HOME``), install ENVY Veil for real from its
   GitHub release: resolve the latest tag, download, verify the SHA-256, unpack,
   and make the launcher (a Start Menu shortcut on Windows, an app on macOS, a
   menu entry on Linux).
3. Run the installed Veil's ``--version``, then remove it and check nothing is left.

Needs the network (GitHub only).
"""

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = "envy-tools"


def main():
    binary = ROOT / "dist" / "standalone" / APP / (APP + (".exe" if os.name == "nt" else ""))
    if not binary.exists():
        raise SystemExit(f"standalone binary not found: {binary}")
    with tempfile.TemporaryDirectory() as home:
        env = dict(os.environ, ENVY_TOOLS_HOME=home, PYTHONIOENCODING="utf-8")

        def run(*args, timeout=600):
            proc = subprocess.run([str(binary), *args], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=timeout, env=env)
            out = proc.stdout + proc.stderr
            print(out.strip())
            if proc.returncode != 0:
                raise SystemExit(f"{' '.join(args)} failed ({proc.returncode})")
            return out

        version = run("--version")
        if APP not in version or not re.search(r"\d+\.\d+\.\d+", version):
            raise SystemExit(f"unexpected --version output: {version!r}")
        listing = run("--offline", "list")
        assert all(s in listing for s in ("vault", "recon", "tunnel")), listing

        out = run("install", "veil")
        assert "checksum OK" in out, out
        manifest = json.loads((Path(home) / "installed.json").read_text(encoding="utf-8"))
        rec = manifest["apps"]["veil"]
        exe = Path(rec["exe"])
        assert exe.is_file(), exe
        for launcher in rec["launchers"]:
            assert Path(launcher).exists(), launcher
        proc = subprocess.run([str(exe), "--version"], capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0 and "envy-veil" in proc.stdout, (proc.stdout, proc.stderr)
        print("installed Veil runs:", proc.stdout.strip())

        run("remove", "veil")
        assert not exe.exists(), "Veil still on disk after remove"
        for launcher in rec["launchers"]:
            assert not Path(launcher).exists(), f"launcher left behind: {launcher}"
        print("standalone smoke test passed: real install, run and remove of ENVY Veil")


if __name__ == "__main__":
    main()
