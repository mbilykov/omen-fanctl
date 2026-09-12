"""CSV telemetry persistence and recovery tests."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omen_fanctl.controller import (
    CsvLog,
)


class CsvLogTests(unittest.TestCase):
    def test_restart_appends_without_duplicate_header(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "omen-fanctl.csv"

            first = CsvLog(path)
            first.write({})
            first.close()
            second = CsvLog(path)
            second.write({})
            second.close()

            lines = path.read_text(encoding="utf-8").splitlines()

        self.assertEqual(lines.count(",".join(CsvLog.FIELDS)), 1)
        self.assertEqual(len(lines), 3)

    def test_archives_existing_csv_with_incompatible_header(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "omen-fanctl.csv"
            previous = path.with_name(f"{path.name}.previous")
            old_fields = tuple(
                field
                for field in CsvLog.FIELDS
                if field
                not in {
                    "amd_gpu_temperature_stale",
                    "nvidia_metrics_stale",
                }
            )
            old_contents = f"{','.join(old_fields)}\nlegacy-row\n"
            path.write_text(old_contents, encoding="utf-8")

            with self.assertLogs("omen-fanctl", level="WARNING") as captured:
                log = CsvLog(path)
            log.write({})
            log.close()

            current_lines = path.read_text(encoding="utf-8").splitlines()
            archived_contents = previous.read_text(encoding="utf-8")

        self.assertEqual(archived_contents, old_contents)
        self.assertEqual(current_lines[0], ",".join(CsvLog.FIELDS))
        self.assertEqual(len(current_lines), 2)
        self.assertTrue(
            any(
                "archived CSV with incompatible schema" in line
                for line in captured.output
            )
        )

    def test_rewrites_header_after_external_copytruncate(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "omen-fanctl.csv"
            log = CsvLog(path)
            log.write({})

            path.write_text("", encoding="utf-8")
            log.write({})
            log.close()
            lines = path.read_text(encoding="utf-8").splitlines()

        self.assertEqual(lines[0], ",".join(CsvLog.FIELDS))
        self.assertEqual(len(lines), 2)

    def test_io_failure_is_deduplicated_and_recovers(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "omen-fanctl.csv"
            log = CsvLog(path)
            with patch(
                "omen_fanctl.controller.os.fstat",
                side_effect=OSError("disk unavailable"),
            ):
                with self.assertLogs("omen-fanctl", level="WARNING") as captured:
                    log.write({})
                    log.write({})

            self.assertEqual(
                sum("CSV telemetry unavailable" in line for line in captured.output),
                1,
            )
            with self.assertLogs("omen-fanctl", level="INFO") as captured:
                log.write({})
            log.close()

        self.assertTrue(
            any("CSV telemetry recovered" in line for line in captured.output)
        )


if __name__ == "__main__":
    unittest.main()
