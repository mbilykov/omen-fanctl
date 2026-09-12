"""Installer, uninstaller, and configuration-deployment tests."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests import PROJECT_ROOT

INSTALL_SCRIPT = PROJECT_ROOT / "install.sh"
UNINSTALL_SCRIPT = PROJECT_ROOT / "uninstall.sh"
CONFIG_STEP = PROJECT_ROOT / "src" / "install" / "config.sh"


class ConfigurationInstallTests(unittest.TestCase):
    """Drive the sourced configuration step directly, without root."""

    def setUp(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory)
        self.root = Path(directory)
        self.config_dir = self.root / "etc"
        self.target = self.config_dir / "omen-fanctl.toml"
        self.packaged = self.root / "packaged.toml"
        self.packaged.write_text('preset = "packaged"\n', encoding="utf-8")

    def _call(self, snippet, path=None):
        environment = dict(os.environ)
        if path is not None:
            environment["PATH"] = path
        result = subprocess.run(
            [
                "bash",
                "-c",
                f'set -Eeuo pipefail\nsource "{CONFIG_STEP}"\n{snippet}\n',
            ],
            capture_output=True,
            text=True,
            env=environment,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def _install(self, replace="false", path=None):
        return self._call(
            f'install_configuration "{self.packaged}" "{self.target}" {replace}',
            path=path,
        )

    def _backups(self):
        return sorted(self.config_dir.glob("omen-fanctl.toml.bak.*"))

    def test_missing_configuration_is_installed(self):
        output = self._install()

        self.assertEqual(self.target.read_text(), 'preset = "packaged"\n')
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o644)
        self.assertEqual(self._backups(), [])
        self.assertIn("Installing configuration", output)

    def test_existing_configuration_is_kept_without_the_flag(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_text('preset = "mine"\n', encoding="utf-8")

        output = self._install()

        self.assertEqual(self.target.read_text(), 'preset = "mine"\n')
        self.assertEqual(self._backups(), [])
        self.assertIn("Preserving existing configuration", output)
        self.assertIn("--replace-config", output)

    def test_replacing_backs_up_the_previous_configuration(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_text('preset = "mine"\n', encoding="utf-8")

        output = self._install(replace="true")

        self.assertEqual(self.target.read_text(), 'preset = "packaged"\n')
        backups = self._backups()
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), 'preset = "mine"\n')
        self.assertRegex(backups[0].name, r"^omen-fanctl\.toml\.bak\.\d{14}$")
        self.assertIn("Saving replaced configuration", output)
        self.assertIn("systemctl restart", output)

    def test_replacing_an_identical_file_writes_no_backup(self):
        self.target.parent.mkdir(parents=True)
        shutil.copy(self.packaged, self.target)

        output = self._install(replace="true")

        self.assertEqual(self._backups(), [])
        self.assertIn("already matches the packaged defaults", output)

    def _freeze_clock(self, stamp="20260101000000"):
        """Pin date(1) so a collision is forced rather than hoped for."""
        stub_dir = self.root / "stub-bin"
        stub_dir.mkdir()
        stub = stub_dir / "date"
        stub.write_text(
            "#!/usr/bin/env bash\n"
            'if [[ "$1" == "+%Y%m%d%H%M%S" ]]; then\n'
            f'    printf "{stamp}\\n"\n'
            "else\n"
            '    exec /usr/bin/date "$@"\n'
            "fi\n",
            encoding="utf-8",
        )
        stub.chmod(0o755)
        self.stub_path = f"{stub_dir}:{os.environ['PATH']}"
        return stamp

    def test_backups_within_one_second_stay_distinct(self):
        stamp = self._freeze_clock()
        self.target.parent.mkdir(parents=True)
        contents = ('preset = "first"\n', 'preset = "second"\n', 'preset = "third"\n')
        for content in contents:
            self.target.write_text(content, encoding="utf-8")
            self._install(replace="true", path=self.stub_path)

        backups = self._backups()
        self.assertEqual(
            [path.name for path in backups],
            [
                f"omen-fanctl.toml.bak.{stamp}",
                f"omen-fanctl.toml.bak.{stamp}.1",
                f"omen-fanctl.toml.bak.{stamp}.2",
            ],
        )
        self.assertEqual([path.read_text() for path in backups], list(contents))

    def test_purging_removes_the_configuration_but_keeps_backups(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_text('preset = "mine"\n', encoding="utf-8")
        self._install(replace="true")

        output = self._call(f'purge_configuration "{self.config_dir}"')

        self.assertFalse(self.target.exists())
        self.assertEqual(len(self._backups()), 1)
        self.assertIn("Preserving 1 replaced-configuration backup", output)

    def test_purging_removes_an_empty_configuration_directory(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_text('preset = "mine"\n', encoding="utf-8")

        output = self._call(f'purge_configuration "{self.config_dir}"')

        self.assertFalse(self.config_dir.exists())
        self.assertNotIn("Preserving", output)

    def test_purging_a_missing_configuration_is_reported(self):
        self.config_dir.mkdir(parents=True)

        output = self._call(f'purge_configuration "{self.config_dir}"')

        self.assertIn("Not installed", output)


class InstallerArgumentTests(unittest.TestCase):
    """Argument handling runs before the root check, so it is testable here."""

    def _run(self, *arguments):
        return subprocess.run(
            [str(INSTALL_SCRIPT), *arguments],
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
        )

    def test_help_documents_every_option(self):
        result = self._run("--help")

        self.assertEqual(result.returncode, 0)
        for option in ("--start-now", "--enable-now", "--replace-config"):
            with self.subTest(option=option):
                self.assertIn(option, result.stdout)

    def test_options_combine(self):
        # Rejected for lack of root, which means parsing accepted the pair.
        result = self._run("--enable-now", "--replace-config")

        self.assertEqual(result.returncode, 1)
        self.assertIn("run this script with sudo", result.stderr)

    def test_start_now_and_enable_now_are_mutually_exclusive(self):
        result = self._run("--start-now", "--enable-now")

        self.assertEqual(result.returncode, 2)
        self.assertIn("mutually exclusive", result.stderr)

    def test_unknown_option_is_rejected(self):
        result = self._run("--replace-configs")

        self.assertEqual(result.returncode, 2)
        self.assertIn("Usage:", result.stderr)


class UninstallerArgumentTests(unittest.TestCase):
    """Uninstaller arguments are rejected before its root and hardware checks."""

    def test_extra_argument_is_rejected(self):
        result = subprocess.run(
            [str(UNINSTALL_SCRIPT), "--purge-config", "unexpected"],
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("unexpected extra arguments", result.stderr)


if __name__ == "__main__":
    unittest.main()
