"""Safety and lifecycle checks for the computer AssetRipper adapter."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile
import venv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from assetripper import AssetRipperAdapter, AssetRipperError, _host_rid, _run, _unpack


class AssetRipperAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.adapter = AssetRipperAdapter(self.root / "private")
        self.input = self.root / "resource with spaces;literal$(name).bundle"
        self.input.write_bytes(b"UnityFS\x00fixture")

    def tearDown(self):
        self.temporary.cleanup()

    def test_status_does_not_disclose_local_paths(self):
        self.adapter.configure({"executable": sys.executable})
        status = self.adapter.status()
        self.assertNotIn(str(self.root), json.dumps(status))
        self.assertNotIn(str(Path.home()), json.dumps(status))

    def test_version_text_in_readme_does_not_select_tuanjie(self):
        readme=self.root / "readme.txt"
        readme.write_text("This folder also supports Tuanjie 2022.3.61t9.")
        self.assertEqual("unity",self.adapter._detect_variant([self.root],None))
        bundle=self.root / "actual.bundle"
        bundle.write_bytes(b"UnityFS\0"+bytes([0,0,0,7])+b"2022.3.61t9\0")
        self.assertEqual("tuanjie",self.adapter._detect_variant([self.root],None))

    def test_platform_runtime_selection(self):
        for system, machine, expected in [("Windows", "AMD64", "win-x64"),
                                          ("Darwin", "arm64", "osx-arm64"),
                                          ("Darwin", "x86_64", "osx-x64")]:
            with patch("assetripper.platform.system", return_value=system), patch("assetripper.platform.machine", return_value=machine):
                self.assertEqual(_host_rid(), expected)

    def test_configured_venv_keeps_its_installed_dependency_environment(self):
        environment = self.root / "isolated-python"
        venv.EnvBuilder(with_pip=False, symlinks=os.name != "nt").create(environment)
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        site = Path(subprocess.check_output([str(python), "-c", "import sysconfig;print(sysconfig.get_path('purelib'))"], text=True).strip())
        # Minimal local dependency used only by the configuration import probe.
        # No installer or network is involved; the module exists only in the
        # venv so resolving the launcher to base Python reproduces the real bug.
        helpers = site / "UnityPy/helpers"
        helpers.mkdir(parents=True)
        (helpers.parent / "__init__.py").write_text("")
        (helpers / "__init__.py").write_text("")
        (helpers / "Tpk.py").write_text("get_typetree_node = None\n")
        status = self.adapter.configure({"python": str(python)})
        self.assertTrue(status["tuanjie_ready"])
        self.assertEqual(self.adapter._config()["python"], str(python.absolute()))

    def test_output_never_overwrites_original_or_existing_files(self):
        original = self.input.read_bytes()
        for output in (self.input, self.root):
            with self.assertRaises(AssetRipperError):
                self.adapter.export([self.input], output)
        occupied = self.root / "occupied"
        occupied.mkdir()
        saved = occupied / "keep.txt"
        saved.write_text("keep")
        with self.assertRaises(AssetRipperError) as raised:
            self.adapter.export([self.input], occupied)
        self.assertEqual(raised.exception.code, "output_not_empty")
        self.assertEqual(saved.read_text(), "keep")
        self.assertEqual(self.input.read_bytes(), original)

    def test_archive_rejects_traversal_and_symlinks(self):
        archive = self.root / "source.zip"
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr("../../escaped", "bad")
        with self.assertRaises(AssetRipperError):
            _unpack(archive, self.root / "unpacked")
        self.assertFalse((self.root.parent / "escaped").exists())

    def test_export_keeps_paths_as_arguments_and_requires_real_manifest(self):
        self.adapter.configure({"executable": sys.executable})
        seen = []
        def fake_run(argv, **kwargs):
            seen.append(argv)
            output = Path(argv[argv.index("--output") + 1])
            output.mkdir()
            # A process exiting zero without actual AssetRipper evidence must
            # not produce a completed result.
            return 0, []
        with patch("assetripper._run", side_effect=fake_run):
            with self.assertRaises(AssetRipperError) as raised:
                self.adapter.export([self.input], self.root / "result")
        self.assertEqual(raised.exception.code, "export_failed")
        self.assertIn(str(self.input.resolve()), seen[0])
        self.assertFalse((self.root / "result").exists())

    def test_cancel_terminates_real_process_and_does_not_leak_logs(self):
        script = self.root / "slow.py"
        marker = self.root / "pid.txt"
        script.write_text("import os,sys,time\nfrom pathlib import Path\n"
                          "Path(sys.argv[1]).write_text(str(os.getpid()))\n"
                          "print('private input name',flush=True)\n"
                          "print('private stderr name',file=sys.stderr,flush=True)\n"
                          "time.sleep(60)\n")
        event = threading.Event()
        progress = []
        timer = threading.Timer(0.3, event.set)
        timer.start()
        started = time.monotonic()
        with self.assertRaises(AssetRipperError) as raised:
            _run([sys.executable, str(script), str(marker)], cwd=self.root,
                 log=self.root / "private.log", cancel_event=event,
                 progress_callback=progress.append)
        timer.join()
        self.assertEqual(raised.exception.code, "cancelled")
        self.assertLess(time.monotonic() - started, 7)
        self.assertNotIn("private input name", json.dumps(progress))
        self.assertNotIn("private stderr name", json.dumps(progress))
        if sys.platform != "win32":
            import os
            with self.assertRaises(ProcessLookupError):
                os.kill(int(marker.read_text()), 0)

    def test_both_streams_are_drained_and_failure_is_not_hidden(self):
        script = "import sys\nfor _ in range(3000):\n print('x'*100)\n print('y'*100,file=sys.stderr)\nsys.exit(7)"
        code, tail = _run([sys.executable, "-c", script], cwd=self.root,
                          log=self.root / "streams.log", timeout=10)
        self.assertEqual(code, 7)
        self.assertLessEqual(len(tail), 30)
        self.assertGreater((self.root / "streams.log").stat().st_size, 600000)


if __name__ == "__main__":
    unittest.main()
