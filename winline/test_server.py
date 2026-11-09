"""Server-worker isolation, backup recovery and pre-match time boundaries."""

import copy
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from winline import worker
from winline.backup import backup_database, restore_backup
from winline.storage import connect, save_results, save_snapshot
from winline.team_context import build_context


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / "source.sqlite3"
        self.db = connect(self.path)
        self.capture = self.db.execute(
            "INSERT INTO captures(started_at,url) VALUES ('2025-12-31','test')"
        ).lastrowid
        self.db.commit()
        self.data = {
            "sports": [{"id": 1, "name": "Футбол"}],
            "countries": [],
            "championships": [],
            "events": [
                {
                    "id": 100,
                    "idSport": 1,
                    "isLive": 0,
                    "date": "2026-01-02T10:00:00Z",
                    "members": ["Команда А", "Команда Б"],
                }
            ],
            "lines": [
                {
                    "id": 1,
                    "idEvent": 100,
                    "isLive": 0,
                    "idTipMarket": 1,
                    "V": [1.9, 2.0],
                    "tipLine": {"R": ["1", "2"]},
                }
            ],
        }
        save_snapshot(self.db, self.capture, self.data, "2025-12-31T12:00:00+00:00")
        self.space = patch(
            "winline.backup.shutil.disk_usage",
            return_value=type("Space", (), {"free": 100 * 1024**3})(),
        )
        self.space.start()
        worker.STOP = False

    def tearDown(self):
        self.space.stop()
        self.db.close()
        self.tmp.cleanup()

    def test_backup_includes_committed_wal_and_restore_never_overwrites(self):
        self.db.execute("UPDATE sports SET name='Updated in WAL' WHERE id=1")
        self.db.commit()
        archive = backup_database(self.path, self.root / "backups")
        target = self.root / "restored.sqlite3"
        restore_backup(archive, target)
        with sqlite3.connect(target) as db:
            self.assertEqual(
                db.execute("select name from sports").fetchone()[0], "Updated in WAL"
            )
        with self.assertRaises(FileExistsError):
            restore_backup(archive, self.path)
        self.assertEqual(
            self.db.execute("select count(*) from events").fetchone()[0], 1
        )

    def test_invalid_backup_checksum_does_not_publish_target(self):
        archive = backup_database(self.path, self.root / "backups")
        archive.with_name(archive.name + ".sha256").write_text("0" * 64)
        target = self.root / "bad.sqlite3"
        with self.assertRaises(ValueError):
            restore_backup(archive, target)
        self.assertFalse(target.exists())

    def test_backup_rotation_leaves_unrelated_files_and_working_database(self):
        directory = self.root / "backups"
        for _ in range(3):
            backup_database(self.path, directory, keep=2)
        other = directory / "manual.sqlite3.gz"
        other.write_bytes(b"keep")
        self.assertEqual(len(list(directory.glob("winline-*.sqlite3.gz"))), 2)
        self.assertEqual(other.read_bytes(), b"keep")
        self.assertEqual(self.db.execute("pragma integrity_check").fetchone()[0], "ok")

    def test_low_disk_backup_does_not_delete_last_good_backup(self):
        directory = self.root / "backups"
        good = backup_database(self.path, directory)
        with (
            patch(
                "winline.backup.shutil.disk_usage",
                return_value=type("Space", (), {"free": 1})(),
            ),
            self.assertRaises(OSError),
        ):
            backup_database(self.path, directory)
        self.assertTrue(good.exists())

    def result(self, sport, score, observed, name="Команда А"):
        row = {
            "idRadar": 10,
            "date": "2025-12-30T10:00:00Z",
            "member1": name,
            "member2": "Команда Б",
            "championship": "Тест",
            "score": score,
        }
        with patch("winline.storage.now", return_value=observed):
            save_results(self.db, self.capture, sport, 0, [row])

    def test_context_uses_score_version_known_at_cutoff(self):
        self.result(1, "1:0", "2025-12-31T12:00:00+00:00")
        self.result(1, "3:0", "2026-01-01T10:00:00+00:00")
        context = build_context(100, self.path, "2026-01-01T09:00:00Z")
        self.assertEqual(context["teams"][0]["recent_results"][0]["score_text"], "1:0")
        self.assertEqual(len(context["head_to_head"]), 1)

    def test_context_excludes_later_observations_other_sports_and_future_prices(self):
        self.result(1, "1:0", "2026-01-01T10:00:00+00:00")
        self.result(2, "100:80", "2025-12-31T12:00:00+00:00")
        self.result(1, "2:0", "2025-12-31T12:00:00+00:00", name="Команда А резерв")
        data = copy.deepcopy(self.data)
        data["lines"][0]["V"] = [1.1, 8.0]
        save_snapshot(self.db, self.capture, data, "2026-01-01T10:00:00+00:00")
        context = build_context(100, self.path, "2026-01-01T09:00:00Z")
        self.assertEqual(context["teams"][0]["recent_results"], [])
        self.assertEqual(context["markets"][0]["values"], [1.9, 2.0])

    def test_context_rejects_post_kickoff_or_unknown_event_observation(self):
        with self.assertRaises(ValueError):
            build_context(100, self.path, "2026-01-02T10:00:00Z")
        with self.assertRaises(ValueError):
            build_context(100, self.path, "2025-12-30T09:00:00Z")

    def test_preflight_cannot_reuse_old_success_or_touch_working_db(self):
        state = self.root / "state"
        state.mkdir()
        working = state / "winline.sqlite3"
        working.write_bytes(b"existing data")
        with (
            patch.object(worker, "run_child", return_value=0),
            patch.object(
                worker,
                "last_run",
                return_value={"capture_id": 1, "status": "complete", "snapshots": 1},
            ),
            patch.object(sys, "argv", ["worker", "--state-dir", str(state), "--check"]),
        ):
            self.assertEqual(worker.main(), 1)
        self.assertEqual(working.read_bytes(), b"existing data")
        self.assertEqual(
            json.loads((state / "status.json").read_text())["state"], "check_failed"
        )

    def test_existing_worker_lock_prevents_second_writer(self):
        import fcntl

        state = self.root / "state"
        state.mkdir()
        with (state / "worker.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(worker, "run_child") as child:
                with (
                    patch.object(
                        sys, "argv", ["worker", "--state-dir", str(state), "--once"]
                    ),
                    self.assertRaises(SystemExit),
                ):
                    worker.main()
                child.assert_not_called()


if __name__ == "__main__":
    unittest.main()
