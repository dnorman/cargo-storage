import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import tomllib
import unittest
import uuid

ROOT = Path(__file__).resolve().parent.parent
BINARY = ROOT / ".build/cargo-storage"
spec = importlib.util.spec_from_file_location("installer", ROOT / "scripts/install.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class ConfigTests(unittest.TestCase):
    def test_preserves_existing_settings_and_comments(self):
        before = '# keep me\n[profile.dev]\ndebug = "line-tables-only"\n[build]\njobs = 4\n[alias]\nc = "check"\n'
        after = installer.add_managed_setting(before, Path("/a path/build"))
        parsed = tomllib.loads(after)
        self.assertEqual(parsed["build"], {"jobs": 4, "build-dir": "/a path/build/{workspace-path-hash}"})
        self.assertEqual(parsed["profile"]["dev"]["debug"], "line-tables-only")
        self.assertIn("# keep me", after)
        self.assertEqual(parsed["alias"]["c"], "check")

    def test_creates_missing_table_and_escapes_unicode_path(self):
        value = '/cache/é "quoted"/build'
        after = installer.add_managed_setting("", Path(value))
        self.assertEqual(tomllib.loads(after)["build"]["build-dir"], value + "/{workspace-path-hash}")

    def test_refuses_existing_or_ambiguous_build_settings(self):
        for text in ('[build]\nbuild-dir="mine"', 'build={jobs=2}',
                     '["build"]\njobs=2', '[build] # comment\njobs=2',
                     installer.BEGIN + '\n'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                installer.add_managed_setting(text, Path("/cache"))

    def test_rejects_escaping_or_template_subdirectory(self):
        for path in ("/absolute", "../outside", "foo/../bar", "foo//bar", "a:b", "a{b", "a}b", "a\nb"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                installer.validate_subdirectory(path)


class HelperTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="cargo-storage-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = self.root / "cargo/config.toml"
        self.config.parent.mkdir()
        self.original = '[profile.dev]\ndebug = "line-tables-only"\n'
        self.config.write_text(installer.add_managed_setting(self.original, self.root / "fallback"))
        self.config.chmod(0o640)
        self.settings = dict(volumeUUID=str(uuid.uuid4()), externalSubdirectory="CargoBuilds/test",
                             internalRoot=str(self.root / "fallback"), cargoConfig=str(self.config),
                             stateDirectory=str(self.root / "state"))
        self.settings_file = self.root / "settings.json"
        self.write_settings()

    def write_settings(self):
        self.settings_file.write_text(json.dumps(self.settings))

    def run_helper(self, command="sync"):
        return subprocess.run([str(BINARY), command, str(self.settings_file)],
                              capture_output=True, text=True, timeout=15)

    def test_fallback_and_repeat_do_not_rewrite_files(self):
        self.assertEqual(self.run_helper().returncode, 0)
        state = self.root / "state/state.json"
        saved = json.loads(state.read_text())
        self.assertEqual(saved["route"]["mode"], "internal")
        times = (self.config.stat().st_mtime_ns, state.stat().st_mtime_ns)
        self.assertEqual(self.run_helper().returncode, 0)
        self.assertEqual(times, (self.config.stat().st_mtime_ns, state.stat().st_mtime_ns))
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o640)
        self.assertEqual(tomllib.loads(self.config.read_text())["profile"]["dev"]["debug"], "line-tables-only")

    def test_refuses_damaged_markers_without_touching_config(self):
        initial = self.config.read_text()
        for changed in (initial.replace(installer.END, ""), initial + installer.BEGIN,
                        initial.replace("[build]", "[alias]"),
                        initial.replace(installer.END, "jobs = 7\n" + installer.END)):
            self.config.write_text(changed)
            self.assertNotEqual(self.run_helper().returncode, 0)
            self.assertEqual(self.config.read_text(), changed)

    def test_settings_validation_precedes_writes(self):
        self.settings["externalSubdirectory"] = "../escape"
        self.write_settings()
        self.assertNotEqual(self.run_helper().returncode, 0)
        self.assertFalse((self.root / "state").exists())
        self.assertFalse((self.root / "fallback").exists())

    def test_refuses_symlink_config_when_switching(self):
        target = self.root / "other.toml"
        target.write_bytes(self.config.read_bytes())
        self.config.unlink()
        self.config.symlink_to(target)
        before = target.read_bytes()
        self.assertNotEqual(self.run_helper().returncode, 0)
        self.assertEqual(target.read_bytes(), before)

    def test_watch_quiet_ticks_do_not_write(self):
        log = (self.root / "watch.log").open("w")
        process = subprocess.Popen([str(BINARY), "watch", str(self.settings_file)],
                                   stdout=log, stderr=log)
        try:
            state = self.root / "state/state.json"
            deadline = time.monotonic() + 10
            while not state.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(state.exists())
            before = (self.config.stat().st_mtime_ns, state.stat().st_mtime_ns)
            time.sleep(4.5)
            self.assertIsNone(process.poll())
            self.assertEqual(before, (self.config.stat().st_mtime_ns, state.stat().st_mtime_ns))
        finally:
            process.terminate()
            process.wait(timeout=10)
            log.close()

    def test_install_into_disposable_home_and_refuse_overwrite(self):
        home = self.root / "disposable-home"
        cargo_home = home / ".cargo"
        plan = installer.make_plan(home, cargo_home, str(uuid.uuid4()), "CargoBuilds/test", BINARY)
        installer.install(plan, start=False)
        config = tomllib.loads((cargo_home / "config.toml").read_text())
        self.assertEqual(config["build"]["build-dir"], str(cargo_home / "build/{workspace-path-hash}"))
        self.assertTrue(plan["command"].is_symlink())
        with self.assertRaises(ValueError):
            installer.make_plan(home, cargo_home, str(uuid.uuid4()), "CargoBuilds/test", BINARY)


if __name__ == "__main__":
    unittest.main()
