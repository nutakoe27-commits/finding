"""Maintenance checks for the dedicated user service and the shared Strata launch file."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import strata_gpu


class RetirePhotos(unittest.TestCase):
    def test_config_backup_uninstall_and_repeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            conf = home / ".local/state/strata/launch/englishbot-huihui-live.json"
            conf.parent.mkdir(parents=True)
            flags = ["--resident-budget-gib", "34", "--vram-reserve-later-mib", "16384",
                     "--max-context", "32768", "--batch", "6", "--kv", "int8"]
            conf.write_text(json.dumps({name: flags for name in strata_gpu.ARRAYS}))
            before = conf.read_bytes()
            folder = home / "mira-photo"
            (folder / ".venv").mkdir(parents=True)
            for name in ("worker.py", ".venv/pyvenv.cfg", "existing.jpg"):
                (folder / name).touch()
            with patch.object(Path, "home", return_value=home), patch.object(sys, "argv", ["test", "--retire-photos"]), \
                    patch.object(strata_gpu.subprocess, "run") as run:
                run.return_value.stdout = "loaded"
                strata_gpu.main()
                strata_gpu.main()
                run.assert_any_call(["systemctl", "--user", "disable", "--now", "mira-photo.service"], check=True)
            backups = list(conf.parent.glob("*.bak"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), before)
            self.assertFalse((folder / "worker.py").exists())
            self.assertFalse((folder / ".venv").exists())
            self.assertTrue((folder / "existing.jpg").exists())
            expected = ["2048" if x == "16384" else x for x in flags]
            for args in json.loads(conf.read_bytes()).values():
                self.assertEqual(args, expected)

    def test_trial_options_keep_separate_gpu_and_ram_budgets(self):
        config = {"args": ["--resident-budget-gib", "34", "--vram-reserve-later-mib", "2048"],
                  "parallel": 6, "user_args": ["--max-context", "32768", "--batch", "6"]}
        strata_gpu.configure(config, context=131072, slots=1)
        self.assertEqual(config["parallel"], 1)
        self.assertEqual(config["args"], ["--resident-budget-gib", "34", "--vram-reserve-later-mib",
                                          "2048", "--max-context", "131072", "--batch", "1"])
        self.assertEqual(config["user_args"], ["--max-context", "131072", "--batch", "1"])
