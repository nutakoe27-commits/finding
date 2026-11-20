"""The photo queue must stay available without keeping the renderer resident."""
import importlib.util
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, call, patch


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
                result = worker.render_job({"scene":"portrait", "photo_prompt":"red scarf", "negative_prompt":"glasses", "claim":"private"})
            self.assertEqual(json.loads(result), {"scene":"portrait", "photo_prompt":"red scarf", "negative_prompt":"glasses"})

    def test_custom_composition_and_exclusions_take_priority_for_both_renderers(self):
        for xl in (True, False):
            positive, negative = worker.render_prompts("portrait", "wide shot, blue scarf", "glasses", xl=xl)
            self.assertLess(positive.index("wide shot"), positive.index("brunette"))
            self.assertNotIn("glasses", positive)
            self.assertTrue(negative.startswith("glasses, "))
            legacy, _ = worker.render_prompts("cafe", None, None, xl=xl)
            self.assertIn(worker.SCENES["cafe"], legacy)

    def test_pose_frame_reduces_face_conditioning_only_for_its_render(self):
        from contextlib import nullcontext

        renderer = object.__new__(worker.RealVisPortraits)
        renderer.guided = False
        renderer.pipe = Mock()
        renderer.pipe.return_value.images = [object()]
        renderer.reference = object()
        torch = Mock()
        torch.inference_mode.return_value = nullcontext()
        with patch.dict(sys.modules, {"torch": torch}), patch.object(worker, "encode_jpeg"):
            renderer.render("portrait", 123, "full-body photograph, lying on a bed", "cropped legs")
            renderer.render("portrait", 123, "cafe, blue shirt", "")
        self.assertEqual(renderer.pipe.set_ip_adapter_scale.call_args_list, [call(0.2), call(0.65)])
        first = renderer.pipe.call_args_list[0].kwargs
        self.assertEqual((first["width"], first["height"]), (960, 768))
        self.assertIn("lying on a bed", first["prompt"])
        self.assertNotIn("hazel eyes", first["prompt"])
        self.assertIn("cropped legs", first["negative_prompt"])

    def test_pose_guide_conditions_only_matching_full_body_requests(self):
        self.assertFalse(worker.pose_layout("detail photograph of legs apart"))
        self.assertFalse(worker.pose_layout("full-body photograph, standing with legs apart"))
        self.assertFalse(worker.pose_layout("full-body photograph, lying on a bed, legs not spread"))
        self.assertTrue(worker.pose_layout("full-body photograph, lying on a bed, legs apart"))
        image = worker.reclining_guide(960, 768)
        self.assertEqual(image.mode, "RGB")
        left, top, right, bottom = image.getbbox()
        self.assertTrue(0 < left < 250 and 700 < right < 960)
        self.assertTrue(0 < top < 220 and 550 < bottom < 768)

    def test_pose_geometry_rejects_upright_hidden_and_joined_legs(self):
        points = [(70, 50)] * 17
        for joint, xy in zip((5, 6, 11, 12, 13, 14, 15, 16),
                             ((25, 45), (25, 55), (50, 45), (50, 55),
                              (65, 35), (65, 65), (85, 25), (85, 75))):
            points[joint] = xy
        scores = [8] * 17
        def accepts():
            return worker.pose_geometry_matches(points, scores, (100, 100), reclining=True, legs_apart=True)

        self.assertTrue(accepts())
        scores[15] = 0.5
        self.assertFalse(accepts())
        scores[15] = 8
        points[15] = points[16]
        self.assertFalse(accepts())
        points[15] = (85, 25)
        points[5], points[6] = (47, 15), (53, 15)
        self.assertFalse(accepts())

    def test_failed_pose_retries_before_writing_and_then_fails_closed(self):
        import io

        job = {"scene": "portrait", "photo_prompt": "full-body photograph, lying on bed"}
        for outcomes in ((False, True), (False, False)):
            with self.subTest(outcomes=outcomes), TemporaryDirectory() as folder:
                target = Path(folder) / "photo.jpg"
                with patch.object(sys, "argv", ["worker.py", "--job-output", str(target), "--seed", "49"]), \
                     patch.object(sys, "stdin", io.StringIO(json.dumps(job))), \
                     patch.dict(sys.modules, {"torch": Mock()}), patch.object(worker, "select_gpu"), \
                     patch.dict(worker.os.environ, {"MIRA_PHOTO_MODEL": "realvisxl"}), \
                     patch.object(worker, "RealVisPortraits") as generator, patch.object(worker, "PoseCheck") as checker:
                    sys.modules["torch"].cuda.max_memory_reserved.return_value = 0
                    generator.return_value.render.side_effect = [b"first", b"second"]
                    checker.return_value.accepts.side_effect = outcomes
                    if outcomes[1]:
                        worker.main()
                        self.assertEqual(target.read_bytes(), b"second")
                    else:
                        with self.assertRaisesRegex(RuntimeError, "pose"):
                            worker.main()
                        self.assertFalse(target.exists())
                    self.assertEqual([c.args[1] for c in generator.return_value.render.call_args_list], [49, 50])

    def test_detail_frame_does_not_request_hair_or_eye_attributes(self):
        for xl in (True, False):
            positive, _ = worker.render_prompts("portrait", "detail photograph of a bracelet", None, xl=xl)
            self.assertIn("bracelet", positive)
            self.assertNotIn("brunette", positive)
            self.assertNotIn("hazel eyes", positive)
