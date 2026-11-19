"""Single GPU consumer. Credentials and chat history never enter diffusion prompts."""
import io
import json
import logging
import os
import secrets
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


class Portraits:
    def __init__(self):
        import torch
        from PIL import Image, ImageOps
        from diffusers import DPMSolverMultistepScheduler, StableDiffusionImg2ImgPipeline, StableDiffusionPipeline

        if torch.cuda.device_count() != 1 or torch.cuda.get_device_capability(0) != (7, 0):
            raise RuntimeError("Select exactly one V100 via CUDA_VISIBLE_DEVICES")
        if "sm_70" not in torch.cuda.get_arch_list():
            raise RuntimeError("The installed torch wheel does not support V100")
        if torch.cuda.mem_get_info()[0] < 8 * 1024**3:
            raise RuntimeError("Less than 8 GiB free before loading portraits")
        torch.cuda.set_per_process_memory_fraction(0.25, 0)
        with Image.open(Path(__file__).with_name("mira.webp")) as avatar:
            self.portrait = ImageOps.fit(avatar.convert("RGB"), (512, 640), Image.Resampling.LANCZOS)
        # All four compositions are authored locally and depict a single adult character.
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

    def render(self, scene):
        import torch
        from PIL import Image

        params = dict(prompt=PORTRAIT + SCENES[scene], negative_prompt=NEGATIVE,
                      guidance_scale=6.5, generator=torch.Generator("cuda").manual_seed(secrets.randbits(32)))
        start = time.monotonic()
        torch.cuda.reset_peak_memory_stats()
        with torch.inference_mode():
            if scene == "selfie":
                first = self.refine(**params, image=self.portrait, strength=0.53, num_inference_steps=32).images[0]
            else:
                first = self.base(**params, width=512, height=640, num_inference_steps=28).images[0]
            enlarged = first.resize((640, 800), Image.Resampling.LANCZOS)
            image = self.refine(**params, image=enlarged, strength=0.28, num_inference_steps=30).images[0]
        out = io.BytesIO()
        image.convert("RGB").save(out, "JPEG", quality=92, optimize=True)
        logging.info("640x800 in %.1fs, torch peak %s MiB", time.monotonic() - start,
                     torch.cuda.max_memory_reserved() // 2**20)
        return out.getvalue()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    site = os.environ.get("MIRA_SITE", "https://prognozich.online").rstrip("/")
    if urlsplit(site).scheme != "https":
        raise ValueError("MIRA_SITE requires HTTPS")
    token = Path(__file__).with_name("photo-worker.key").read_text().strip()
    session = requests.Session()
    session.headers["Authorization"] = "Bearer " + token
    generator = Portraits()
    logging.info("Photo worker ready")
    while True:
        try:
            resp = session.post(site + "/api/photo-worker/claim", timeout=15, allow_redirects=False)
            if resp.status_code == 204:
                time.sleep(4)
                continue
            resp.raise_for_status()
            job = resp.json()
            try:
                jpeg = generator.render(job["scene"])
            except Exception as exc:
                logging.error("Generation failed: %s", type(exc).__name__)
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
