"""WP7 acceptance: run records (PRD §12, §16.2)."""

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

_ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ / "event-broker"))

from app.services.run_records import RunRecords  # noqa: E402


def now_provider() -> datetime:
    return datetime(2026, 11, 20, 12, 0)


class RunRecordsTests(unittest.TestCase):
    def test_append_writes_one_json_line_per_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            records = RunRecords(str(Path(tmp) / "runs"), now=now_provider)
            records.append({"run_id": "a", "outcome": "SUCCEEDED"})
            records.append({"run_id": "b", "outcome": "FAILED"})
            path = Path(tmp) / "runs" / "2026-11-20.jsonl"
            self.assertTrue(path.exists())
            lines = [json_loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(lines), 2)
            self.assertEqual(lines[0]["run_id"], "a")
            self.assertEqual(lines[1]["outcome"], "FAILED")

    def test_append_creates_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            records = RunRecords(str(Path(tmp) / "deep" / "runs"), now=now_provider)
            records.append({"run_id": "a"})
            self.assertTrue((Path(tmp) / "deep" / "runs" / "2026-11-20.jsonl").exists())

    def test_prune_keeps_30_days_and_non_matching_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            days = {
                "2026-10-20": "old (31 days) -> pruned",
                "2026-10-21": "boundary (30 days) -> kept",
                "2026-11-19": "recent -> kept",
                "2026-11-20": "today -> kept",
            }
            for name in days:
                (root / f"{name}.jsonl").write_text(days[name], encoding="utf-8")
            (root / "not-a-date.jsonl").write_text("kept", encoding="utf-8")
            (root / "readme.txt").write_text("kept", encoding="utf-8")

            records = RunRecords(str(root), now=now_provider)
            records.prune()

            self.assertFalse((root / "2026-10-20.jsonl").exists())
            for keep in ["2026-10-21.jsonl", "2026-11-19.jsonl", "2026-11-20.jsonl",
                         "not-a-date.jsonl", "readme.txt"]:
                self.assertTrue((root / keep).exists(), keep)

    def test_append_then_prune_removes_old_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "2026-09-01.jsonl").write_text("old", encoding="utf-8")
            records = RunRecords(str(root), now=now_provider)
            records.append({"run_id": "fresh"})
            # the append prunes too (REC-3)
            self.assertFalse((root / "2026-09-01.jsonl").exists())
            self.assertTrue((root / "2026-11-20.jsonl").exists())


def json_loads(s):
    import json
    return json.loads(s)


if __name__ == "__main__":
    unittest.main()