"""Single GPU consumer. Credentials and chat history never enter diffusion prompts."""
import argparse
import io
import json
import logging
import os
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
PORTRAIT = ("Editorial photograph of Mira, one fictional 28-year-old woman, short wavy brunette bob, "
            "hazel green eyes, arched brows, warm olive complexion, natural skin texture, subtle smile, ")
SCENES = {
    "selfie": "candid close-up selfie, ivory knit sweater, window light, soft shadows, softly blurred terracotta wall",
    "cafe": "waist-up seated in a cozy cafe, moss green blouse, late afternoon window light, film photography",
    "boudoir": "intimate boudoir portrait, lace lingerie, warm bedside lamp, relaxed pose, natural proportions",
    "nude": "fine art nude portrait, bare breasts, relaxed adult woman alone, side lighting, studio backdrop",
}
NEGATIVE = ("illustration, CGI, waxy skin, excessive retouching, blur, double face, duplicate person, "
            "distorted anatomy, extra limbs, watermark, signature, lettering")


REALVIS = "SG161222/RealVisXL_V5.0"
REALVIS_REV = "ac93e0dda1f6d448cae19bbfab8c5e720a5e48bc"
ADAPTER = "h94/IP-Adapter"
ADAPTER_REV = "018e402774aeeddd60609b4ecdb7e298259dc729"
FACE_WEIGHTS = "ip-adapter-plus-face_sdxl_vit-h.safetensors"


def render_prompts(scene, photo_prompt, negative_prompt, *, xl):
    if photo_prompt:
        # CLIP truncates long prompts. Put the requested shot ahead of style defaults.
        positive = ("Photograph of one adult woman, " + photo_prompt +
                    ", short wavy brunette bob, hazel eyes, olive skin, natural skin texture")
    elif xl:
        positive = ("Candid photograph of a 28-year-old adult woman, short wavy brunette bob, "
                    "hazel-green eyes, olive complexion, " + SCENES[scene] +
                    ", lifelike skin, fine flyaway hairs, candid expression, subtle film grain")
    else:
        positive = PORTRAIT + SCENES[scene]
    negative = NEGATIVE + (", bad hands, airbrushed, plastic skin" if xl else "")
    return positive, (negative_prompt + ", " + negative if negative_prompt else negative)


def select_gpu():
    import torch

    if torch.cuda.device_count() != 1 or torch.cuda.get_device_capability(0) != (7, 0):
        raise RuntimeError("Select exactly one V100 via CUDA_VISIBLE_DEVICES")
    if "sm_70" not in torch.cuda.get_arch_list():
        raise RuntimeError("The installed torch wheel does not support V100")
    if torch.cuda.mem_get_info()[0] < 8 * 1024**3:
        raise RuntimeError("Less than 8 GiB free before loading portraits")
    torch.cuda.set_per_process_memory_fraction(0.25, 0)


def xl_files():
    from huggingface_hub import snapshot_download

    def fetch(repo, revision, patterns):
        # Try the pinned files in the cache first, including when the host is offline.
        from huggingface_hub.errors import LocalEntryNotFoundError
        try:
            directory = Path(snapshot_download(repo, revision=revision, local_files_only=True))
            if all((directory / name).is_file() for name in patterns):
                return str(directory)
        except LocalEntryNotFoundError:
            pass
        return snapshot_download(repo, revision=revision, allow_patterns=patterns)

    files = ["model_index.json", "scheduler/scheduler_config.json"]
    for folder, weights in (("unet", "diffusion_pytorch_model"), ("vae", "diffusion_pytorch_model"),
                            ("text_encoder", "model"), ("text_encoder_2", "model")):
        files.extend([f"{folder}/config.json", f"{folder}/{weights}.fp16.safetensors"])
    for folder in ("tokenizer", "tokenizer_2"):
        files.extend(f"{folder}/{name}" for name in
                     ("merges.txt", "vocab.json", "tokenizer_config.json", "special_tokens_map.json"))
    return (fetch(REALVIS, REALVIS_REV, files),
            fetch(ADAPTER, ADAPTER_REV, ["models/image_encoder/config.json", "models/image_encoder/model.safetensors",
                                         f"sdxl_models/{FACE_WEIGHTS}"]))


class RealVisPortraits:
    def __init__(self):
        import torch
        from diffusers import DPMSolverMultistepScheduler, StableDiffusionXLPipeline
        from PIL import Image, ImageOps

        model_path, face_path = xl_files()
        self.pipe = StableDiffusionXLPipeline.from_pretrained(
            model_path, variant="fp16", torch_dtype=torch.float16, use_safetensors=True,
            local_files_only=True, add_watermarker=False)
        self.pipe.load_ip_adapter(face_path, subfolder="sdxl_models", weight_name=FACE_WEIGHTS,
                                  image_encoder_folder="models/image_encoder", local_files_only=True)
        self.pipe.set_ip_adapter_scale(0.65)
        self.pipe.scheduler = DPMSolverMultistepScheduler.from_config(
            self.pipe.scheduler.config, algorithm_type="sde-dpmsolver++", use_karras_sigmas=True)
        self.pipe.enable_vae_tiling()
        self.pipe.enable_model_cpu_offload(gpu_id=0)
        self.pipe.set_progress_bar_config(disable=True)
        with Image.open(Path(__file__).with_name("mira.webp")) as avatar:
            self.reference = ImageOps.fit(avatar.convert("RGB"), (224, 224), Image.Resampling.LANCZOS)

    def render(self, scene, seed=None, photo_prompt=None, negative_prompt=None):
        import torch

        prompt, negative = render_prompts(scene, photo_prompt, negative_prompt, xl=True)
        with torch.inference_mode():
            image = self.pipe(prompt=prompt, negative_prompt=negative,
                              ip_adapter_image=self.reference, width=768, height=960, num_inference_steps=32,
                              guidance_scale=5.0, generator=torch.Generator("cpu").manual_seed(
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
        params = dict(prompt=prompt, negative_prompt=negative,
                      guidance_scale=6.5, generator=generator)
        with torch.inference_mode():
            if scene == "selfie":
                first = self.refine(**params, image=self.portrait, strength=0.53, num_inference_steps=32).images[0]
            else:
                first = self.base(**params, width=512, height=640, num_inference_steps=28).images[0]
            enlarged = first.resize((640, 800), Image.Resampling.LANCZOS)
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
    parser.add_argument("--download-sd15", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--download-xl", action="store_true", help="cache pinned XL weights without touching CUDA")
    parser.add_argument("--preview", type=Path, help="render a local JPEG without connecting to the chat")
    parser.add_argument("--job-output", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--scene", choices=SCENES, default="selfie")
    parser.add_argument("--seed", type=int)
    args = parser.parse_args()
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
        generator = RealVisPortraits() if profile == "realvisxl" else SD15Portraits()
        loaded = time.monotonic()
        torch.cuda.reset_peak_memory_stats()
        jpeg = generator.render(job["scene"], args.seed, job.get("photo_prompt"), job.get("negative_prompt"))
        (args.job_output or args.preview).write_bytes(jpeg)
        logging.info("%s loaded in %.1fs, rendered in %.1fs, torch peak %d MiB", profile,
                     loaded - start, time.monotonic() - loaded, torch.cuda.max_memory_reserved() // 2**20)
        return
    # Finish large first-time downloads before accepting a leased job. The poller
    # will not import torch or map any model weights itself.
    download = "--download-xl" if profile == "realvisxl" else "--download-sd15"
    subprocess.run([sys.executable, __file__, download], check=True)
    site = os.environ.get("MIRA_SITE", "https://prognozich.online").rstrip("/")
    if urlsplit(site).scheme != "https":
        raise ValueError("MIRA_SITE requires HTTPS")
    token = Path(__file__).with_name("photo-worker.key").read_text().strip()
    session = requests.Session()
    session.headers.update({"Authorization": "Bearer " + token, "X-Mira-Photo-Context": "1"})
    logging.info("Photo worker ready (%s)", profile)
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
