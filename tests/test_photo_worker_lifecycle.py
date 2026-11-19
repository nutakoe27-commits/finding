"""The photo queue must stay available without keeping the renderer resident."""
import importlib.util
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch


path = Path(__file__).resolve().parents[1] / "deploy/companion_photo/worker.py"
spec = importlib.util.spec_from_file_location("mira_photo_worker", path)
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


class RenderLifecycle(unittest.TestCase):
    def test_poll_deliver_then_idle_without_torch(self):
        with TemporaryDirectory() as folder:
            Path(folder, "photo-worker.key").write_text("local-test-key")
            session = Mock(headers={})
            job = Mock(status_code=200)
            job.json.return_value = {"id":"id", "claim":"claim", "scene":"portrait", "photo_prompt":"red scarf"}
            session.post.side_effect = [job, Mock(status_code=204), Mock(status_code=204)]
            with patch.object(worker, "__file__", str(Path(folder, "worker.py"))), \
                 patch.dict(sys.modules, {"torch": None}), patch.object(sys, "argv", ["worker.py"]), \
                 patch.object(worker.subprocess, "run"), patch.object(worker.requests, "Session", return_value=session), \
                 patch.object(worker, "render_job", return_value=b"jpeg") as render, \
                 patch.object(worker.time, "sleep", side_effect=StopIteration):
                with self.assertRaises(StopIteration):
                    worker.main()
            render.assert_called_once_with(job.json.return_value)
            self.assertEqual(session.post.call_args_list[1].kwargs["data"], b"jpeg")

    def test_child_receives_only_render_inputs_and_exits(self):
        with TemporaryDirectory() as folder:
            helper = Path(folder, "fake_renderer.py")
            helper.write_text("import json,sys\nfrom pathlib import Path\n"
                              "assert sys.argv[1] == '--job-output'\n"
                              "Path(sys.argv[2]).write_text(json.dumps(json.load(sys.stdin)))\n")
            with patch.object(worker, "__file__", str(helper)):
                result = worker.render_job({"scene":"portrait", "photo_prompt":"red scarf", "claim":"private"})
            self.assertEqual(json.loads(result), {"scene":"portrait", "photo_prompt":"red scarf"})
