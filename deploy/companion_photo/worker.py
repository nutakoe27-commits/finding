"""Single GPU consumer. Credentials and chat history never enter diffusion prompts."""
import argparse
import io
import json
import logging
import math
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests

MODEL = "stable-diffusion-v1-5/stable-diffusion-v1-5"
REVISION = "451f4fe16113bff5a5d2269ed5ad43b0592e9a14"
SCENES = {
    "selfie": "handheld front-camera selfie, forearm at the frame edge, slightly high angle, sweater, uneven window light",
    "cafe": "handheld selfie at a cafe table, moss green blouse, off-centre framing, light from the cafe window",
    "boudoir": "mirror selfie, lace lingerie, phone in one hand, uneven bedside lamplight",
    "nude": "unclothed adult woman, self-timer phone snapshot, soft light from the bedroom window",
}
NEGATIVE = ("illustration, CGI, waxy skin, excessive retouching, double face, duplicate person, "
            "distorted anatomy, extra limbs, watermark, signature, lettering")


REALVIS = "SG161222/RealVisXL_V5.0"
REALVIS_REV = "ac93e0dda1f6d448cae19bbfab8c5e720a5e48bc"
ADAPTER = "h94/IP-Adapter"
ADAPTER_REV = "018e402774aeeddd60609b4ecdb7e298259dc729"
POSE_ADAPTER = "TencentARC/t2i-adapter-openpose-sdxl-1.0"
POSE_ADAPTER_REV = "f90998885600ed3627cedc305316edff47b8b03f"
FACE_WEIGHTS = "ip-adapter-plus-face_sdxl_vit-h.safetensors"


def render_prompts(scene, photo_prompt, negative_prompt, *, xl):
    composition = photo_prompt or SCENES[scene]
    if composition.startswith("detail photograph"):
        positive = composition + ", candid phone photo, natural texture"
    elif composition.startswith("full-body photograph"):
        positive = composition + ", one adult woman, brunette, candid phone photograph"
    else:
        positive = (composition + ", photograph of one adult woman, short wavy brunette bob, "
                    "hazel eyes, olive skin, natural skin texture")
    negative = NEGATIVE + (", bad hands, airbrushed, plastic skin" if xl else "")
    return positive, (negative_prompt + ", " + negative if negative_prompt else negative)


def reclining_frame(prompt):
    return prompt.startswith("full-body photograph") and bool(
        re.search(r"\b(?:lying|laying|reclining|reclined|supine|prone)\b", prompt, re.I))


SPREAD_LEGS = re.compile(r"\blegs?\b.{0,24}\b(?:apart|spread|open)\b|\b(?:spread|open)\b.{0,16}\blegs?\b", re.I)


def legs_apart_requested(prompt):
    if re.search(r"\blegs?\s+(?:(?:are|held|kept)\s+)?(?:not|together|crossed|closed)\b", prompt, re.I):
        return False
    return bool(SPREAD_LEGS.search(prompt))


def pose_layout(prompt):
    return reclining_frame(prompt) and legs_apart_requested(prompt)


def reclining_guide(width, height):
    # OpenPose COCO-18 joints and limb palette. Keep both ankles inside the
    # frame with room beyond them. The guide is only an input to the adapter.
    from PIL import Image, ImageDraw

    joints = [(0.17, .44), (.26, .45), (.27, .36), (.34, .26), (.38, .19),
              (.25, .54), (.32, .63), (.41, .66), (.49, .43), (.66, .29),
              (.84, .18), (.48, .56), (.66, .73), (.85, .84), (.165, .42),
              (.16, .46), (.19, .39), (.18, .48)]
    limbs = [(1,2),(1,5),(2,3),(3,4),(5,6),(6,7),(1,8),(8,9),(9,10),
             (1,11),(11,12),(12,13),(1,0),(0,14),(14,16),(0,15),(15,17)]
    colors = [(255,0,0),(255,85,0),(255,170,0),(255,255,0),(170,255,0),
              (85,255,0),(0,255,0),(0,255,85),(0,255,170),(0,255,255),
              (0,170,255),(0,85,255),(0,0,255),(85,0,255),(170,0,255),
              (255,0,255),(255,0,170),(255,0,85)]
    # Leave context around the figure: at inference SDXL may zoom beyond
    # the endpoints of the skeleton, especially at the feet.
    points = [(round((.5 + .78 * (x - .5)) * width), round((.5 + .78 * (y - .5)) * height))
              for x, y in joints]
    image = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(image)
    radius = max(2, round(min(width, height) / 140))
    for (a, b), color in zip(limbs, colors):
        draw.line((points[a], points[b]), fill=tuple(round(c * .6) for c in color), width=2 * radius)
    for (x, y), color in zip(points, colors):
        draw.ellipse((x-radius, y-radius, x+radius, y+radius), fill=color)
    # The pinned TencentARC checkpoint uses the channel-reversed OpenPose map.
    red, green, blue = image.split()
    return Image.merge("RGB", (blue, green, red))


def pose_geometry_matches(points, confidence, size, *, reclining, legs_apart):
    # COCO order: shoulders 5/6, hips 11/12, knees 13/14, ankles 15/16.
    # These are keypoint heatmap scores, not probabilities.
    width, height = size
    for i in (5, 6, 11, 12, 13, 14, 15, 16):
        x, y = points[i][:2]
        if confidence[i] < 2 or not (0.015 * width < x < 0.985 * width and 0.015 * height < y < 0.985 * height):
            return False
    dx, dy = ((points[5][a] + points[6][a] - points[11][a] - points[12][a]) / 2 for a in (0, 1))
    torso = math.hypot(dx, dy)
    if torso < 0.06 * min(size) or (reclining and abs(dx) < 0.65 * abs(dy)):
        return False
    if legs_apart:
        knee_span = math.dist(points[13][:2], points[14][:2])
        ankle_span = math.dist(points[15][:2], points[16][:2])
        return knee_span > 0.48 * torso and ankle_span > 0.7 * torso
    return True


class PoseCheck:
    def __init__(self):
        import torch
        from torchvision.models.detection import KeypointRCNN_ResNet50_FPN_Weights, keypointrcnn_resnet50_fpn

        torch.set_num_threads(min(4, torch.get_num_threads()))
        weights = KeypointRCNN_ResNet50_FPN_Weights.COCO_V1
        self.transform = weights.transforms()
        self.model = keypointrcnn_resnet50_fpn(weights=weights, min_size=512, max_size=800).cpu().eval()

    def accepts(self, jpeg, prompt):
        import torch
        from PIL import Image

        with Image.open(io.BytesIO(jpeg)) as image, torch.inference_mode():
            result = self.model([self.transform(image.convert("RGB"))])[0]
            people = (result["scores"] >= 0.85).nonzero().flatten()
            if len(people) != 1:
                return False
            index = people.item()
            return pose_geometry_matches(result["keypoints"][index].tolist(), result["keypoints_scores"][index].tolist(),
                                         image.size, reclining=reclining_frame(prompt), legs_apart=legs_apart_requested(prompt))


def select_gpu():
    import torch

    if torch.cuda.device_count() != 1 or torch.cuda.get_device_capability(0) != (7, 0):
        raise RuntimeError("Select exactly one V100 via CUDA_VISIBLE_DEVICES")
    if "sm_70" not in torch.cuda.get_arch_list():
        raise RuntimeError("The installed torch wheel does not support V100")
    if torch.cuda.mem_get_info()[0] < 8 * 1024**3:
        raise RuntimeError("Less than 8 GiB free before loading portraits")
    torch.cuda.set_per_process_memory_fraction(0.25, 0)


def cached_files(repo, revision, patterns):
    from huggingface_hub import snapshot_download
    from huggingface_hub.errors import LocalEntryNotFoundError

    try:
        directory = Path(snapshot_download(repo, revision=revision, local_files_only=True))
        if all((directory / name).is_file() for name in patterns):
            return str(directory)
    except LocalEntryNotFoundError:
        pass
    return snapshot_download(repo, revision=revision, allow_patterns=patterns)


def pose_adapter_files():
    return cached_files(POSE_ADAPTER, POSE_ADAPTER_REV, ["config.json", "diffusion_pytorch_model.safetensors"])


def xl_files():
    files = ["model_index.json", "scheduler/scheduler_config.json"]
    for folder, weights in (("unet", "diffusion_pytorch_model"), ("vae", "diffusion_pytorch_model"),
                            ("text_encoder", "model"), ("text_encoder_2", "model")):
        files.extend([f"{folder}/config.json", f"{folder}/{weights}.fp16.safetensors"])
    for folder in ("tokenizer", "tokenizer_2"):
        files.extend(f"{folder}/{name}" for name in
                     ("merges.txt", "vocab.json", "tokenizer_config.json", "special_tokens_map.json"))
    return (cached_files(REALVIS, REALVIS_REV, files),
            cached_files(ADAPTER, ADAPTER_REV, ["models/image_encoder/config.json", "models/image_encoder/model.safetensors",
                                         f"sdxl_models/{FACE_WEIGHTS}"]))


class RealVisPortraits:
    def __init__(self, *, guided=False):
        import torch
        from diffusers import DPMSolverMultistepScheduler, StableDiffusionXLPipeline
        from PIL import Image, ImageOps

        model_path, face_path = xl_files()
        pipeline = StableDiffusionXLPipeline
        extra = {"add_watermarker": False}
        if guided:
            from diffusers import StableDiffusionXLAdapterPipeline, T2IAdapter
            pipeline = StableDiffusionXLAdapterPipeline
            adapter = T2IAdapter.from_pretrained(pose_adapter_files(), torch_dtype=torch.float16, local_files_only=True)
            extra = {"adapter": adapter}
        self.guided = guided
        self.pipe = pipeline.from_pretrained(
            model_path, **extra, variant="fp16", torch_dtype=torch.float16, use_safetensors=True,
            local_files_only=True)
        self.pipe.load_ip_adapter(face_path, subfolder="sdxl_models", weight_name=FACE_WEIGHTS,
                                  image_encoder_folder="models/image_encoder", local_files_only=True)
        if guided:
            from diffusers import EulerAncestralDiscreteScheduler
            self.pipe.scheduler = EulerAncestralDiscreteScheduler.from_config(self.pipe.scheduler.config)
        else:
            self.pipe.scheduler = DPMSolverMultistepScheduler.from_config(
                self.pipe.scheduler.config, algorithm_type="sde-dpmsolver++", use_karras_sigmas=True)
        if guided:
            self.pipe.model_cpu_offload_seq = "text_encoder->text_encoder_2->image_encoder->adapter->unet->vae"
        self.pipe.enable_vae_tiling()
        self.pipe.enable_model_cpu_offload(gpu_id=0)
        self.pipe.set_progress_bar_config(disable=True)
        with Image.open(Path(__file__).with_name("mira.webp")) as avatar:
            # This bundled avatar is square; crop to the face so the adapter
            # does not also condition every scene on its sweater and shoulders.
            side = avatar.width
            face = avatar.crop((round(side * .25), round(side * .17),
                                round(side * .81), round(side * .73)))
            self.reference = ImageOps.fit(face.convert("RGB"), (224, 224), Image.Resampling.LANCZOS)

    def render(self, scene, seed=None, photo_prompt=None, negative_prompt=None):
        import torch

        prompt, negative = render_prompts(scene, photo_prompt, negative_prompt, xl=True)
        face_weight = 0.65
        if prompt.startswith("detail photograph"):
            face_weight = 0.0
        elif prompt.startswith("full-body photograph"):
            face_weight = 0.2
        self.pipe.set_ip_adapter_scale(face_weight)
        width, height = (960, 768) if reclining_frame(prompt) else (768, 960)
        guide = {}
        if self.guided:
            guide = dict(image=reclining_guide(width, height), adapter_conditioning_scale=1.0,
                         adapter_conditioning_factor=1.0)
        with torch.inference_mode():
            image = self.pipe(**guide, prompt=prompt, negative_prompt=negative,
                              ip_adapter_image=self.reference, width=width, height=height, num_inference_steps=32,
                              guidance_scale=7.5 if self.guided else 5.0, generator=torch.Generator("cpu").manual_seed(
                                  secrets.randbits(32) if seed is None else seed)).images[0]
        return encode_jpeg(image)


def encode_jpeg(image):
    image = image.convert("RGB")
    if all(high - low < 8 for low, high in image.getextrema()):
        raise RuntimeError("Model returned a nearly blank image")
    for quality in (92, 84):
        out = io.BytesIO()
        image.save(out, "JPEG", quality=quality, optimize=True)
        if out.tell() <= 800_000:  # Receiver upload limit.
            return out.getvalue()
    raise RuntimeError("Photo exceeds the upload limit")


class SD15Portraits:
    def __init__(self):
        import torch
        from PIL import Image, ImageOps
        from diffusers import DPMSolverMultistepScheduler, StableDiffusionImg2ImgPipeline, StableDiffusionPipeline

        with Image.open(Path(__file__).with_name("mira.webp")) as avatar:
            self.portrait = ImageOps.fit(avatar.convert("RGB"), (512, 640), Image.Resampling.LANCZOS)
        self.base = StableDiffusionPipeline.from_pretrained(
            MODEL, revision=REVISION, variant="fp16", torch_dtype=torch.float16, use_safetensors=True,
            safety_checker=None, requires_safety_checker=False,
        ).to("cuda")
        self.base.scheduler = DPMSolverMultistepScheduler.from_config(
            self.base.scheduler.config, algorithm_type="dpmsolver++", use_karras_sigmas=True)
        self.refine = StableDiffusionImg2ImgPipeline(**self.base.components, requires_safety_checker=False)
        for pipe in (self.base, self.refine):
            pipe.enable_attention_slicing()
            pipe.enable_vae_slicing()
            pipe.set_progress_bar_config(disable=True)

    def render(self, scene, seed=None, photo_prompt=None, negative_prompt=None):
        import torch
        from PIL import Image

        generator = torch.Generator("cuda").manual_seed(secrets.randbits(32) if seed is None else seed)
        prompt, negative = render_prompts(scene, photo_prompt, negative_prompt, xl=False)
        width, height = (640, 512) if reclining_frame(prompt) else (512, 640)
        params = dict(prompt=prompt, negative_prompt=negative,
                      guidance_scale=6.5, generator=generator)
        with torch.inference_mode():
            if scene == "selfie" and not prompt.startswith(("full-body photograph", "detail photograph")):
                first = self.refine(**params, image=self.portrait, strength=0.53, num_inference_steps=32).images[0]
            else:
                first = self.base(**params, width=width, height=height, num_inference_steps=28).images[0]
            enlarged = first.resize((width * 5 // 4, height * 5 // 4), Image.Resampling.LANCZOS)
            image = self.refine(**params, image=enlarged, strength=0.28, num_inference_steps=30).images[0]
        return encode_jpeg(image)


def render_job(job):
    # The process lifetime releases private RAM, CUDA allocations and offload
    # hooks after each photo, including on CUDA errors. Only the poller stays up.
    with tempfile.TemporaryDirectory(prefix="mira-render-") as directory:
        path = Path(directory) / "photo.jpg"
        payload = json.dumps({"scene": job["scene"], "photo_prompt": job.get("photo_prompt"),
                              "negative_prompt": job.get("negative_prompt")}).encode()
        subprocess.run([sys.executable, __file__, "--job-output", str(path)], input=payload,
                       check=True, timeout=285)
        return path.read_bytes()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--download-pose", action="store_true", help="cache CPU pose verification weights")
    parser.add_argument("--download-sd15", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--download-xl", action="store_true", help="cache pinned XL weights without touching CUDA")
    parser.add_argument("--preview", type=Path, help="render a local JPEG without connecting to the chat")
    parser.add_argument("--job-output", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--scene", choices=SCENES, default="selfie")
    parser.add_argument("--seed", type=int)
    args = parser.parse_args()
    if args.download_pose:
        from torchvision.models.detection import KeypointRCNN_ResNet50_FPN_Weights
        KeypointRCNN_ResNet50_FPN_Weights.COCO_V1.get_state_dict(progress=False, check_hash=True)
        if os.environ.get("MIRA_PHOTO_MODEL", "realvisxl") == "realvisxl":
            pose_adapter_files()
        return
    if args.download_sd15:
        from diffusers import StableDiffusionPipeline
        StableDiffusionPipeline.download(MODEL, revision=REVISION, variant="fp16",
                                         use_safetensors=True, safety_checker=None)
        return
    if args.download_xl:
        xl_files()
        return

    profile = os.environ.get("MIRA_PHOTO_MODEL", "realvisxl")
    if profile not in ("realvisxl", "sd15"):
        raise ValueError("MIRA_PHOTO_MODEL must be realvisxl or sd15")
    if args.preview or args.job_output:
        import torch

        job = json.load(sys.stdin) if args.job_output else {"scene": args.scene}
        select_gpu()
        start = time.monotonic()
        prompt = job.get("photo_prompt") or ""
        if profile == "realvisxl":
            generator = RealVisPortraits(guided=pose_layout(prompt))
        else:
            generator = SD15Portraits()
        loaded = time.monotonic()
        torch.cuda.reset_peak_memory_stats()
        checker = PoseCheck() if prompt.startswith("full-body photograph") else None
        seed = args.seed
        for attempt in range(2):
            render_start = time.monotonic()
            jpeg = generator.render(job["scene"], seed, prompt, job.get("negative_prompt"))
            if checker is None or checker.accepts(jpeg, prompt):
                break
            # Leave time to upload within the existing worker lease.
            duration = time.monotonic() - render_start
            if attempt or time.monotonic() - start + duration > 250:
                raise RuntimeError("Requested pose could not be confirmed")
            if seed is not None:
                seed += 1
        (args.job_output or args.preview).write_bytes(jpeg)
        logging.info("%s loaded in %.1fs, rendered in %.1fs, torch peak %d MiB", profile,
                     loaded - start, time.monotonic() - loaded, torch.cuda.max_memory_reserved() // 2**20)
        return
    # Finish large first-time downloads before accepting a leased job. The poller
    # will not import torch or map any model weights itself.
    download = "--download-xl" if profile == "realvisxl" else "--download-sd15"
    subprocess.run([sys.executable, __file__, download], check=True)
    pose_ready = subprocess.run([sys.executable, __file__, "--download-pose"]).returncode == 0
    if not pose_ready:
        logging.warning("Pose verification unavailable. Serving other photo jobs.")
    site = os.environ.get("MIRA_SITE", "https://prognozich.online").rstrip("/")
    if urlsplit(site).scheme != "https":
        raise ValueError("MIRA_SITE requires HTTPS")
    token = Path(__file__).with_name("photo-worker.key").read_text().strip()
    session = requests.Session()
    session.headers.update({"Authorization": "Bearer " + token, "X-Mira-Photo-Context": "1"})
    if pose_ready:
        session.headers["X-Mira-Photo-Pose"] = "1"
    logging.info("Photo worker ready (%s, pose=%s)", profile, "ready" if pose_ready else "unavailable")
    while True:
        try:
            resp = session.post(site + "/api/photo-worker/claim", timeout=15, allow_redirects=False)
            if resp.status_code == 204:
                time.sleep(4)
                continue
            resp.raise_for_status()
            job = resp.json()
            try:
                jpeg = render_job(job)
            except Exception:
                logging.exception("Generation failed for %s", profile)
                jpeg = b""
            url = site + "/api/photo-worker/" + job["id"] + "/" + job["claim"]
            for attempt in range(3):
                try:
                    done = session.post(url, data=jpeg, headers={"Content-Type": "image/jpeg"},
                                        timeout=25, allow_redirects=False)
                    if done.status_code == 410:
                        break  # User cleared history, or the lease expired.
                    done.raise_for_status()
                    break
                except requests.RequestException:
                    if attempt == 2:
                        raise
                    time.sleep(2)
        except (requests.RequestException, json.JSONDecodeError) as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            logging.error("Site unavailable: %s %s", type(exc).__name__, status or "")
            time.sleep(10)


if __name__ == "__main__":
    main()
