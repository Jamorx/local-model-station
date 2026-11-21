#!/usr/bin/env python3
"""H3-Image P0 decode bridge: latent dump -> PNG.

Reads the raw F32 latent produced by `h3 --image --output FILE` (row-major
[24, 1, latent_h, latent_w]) plus its FILE.meta.json sidecar, denormalizes it
with the VAE config, decodes with the official diffusers
AutoencoderKLMiniMaxH3Image on MPS, reverts the ImageNet pixel normalization,
quantizes with truncation exactly like the released pipeline, and resizes the
generation canvas back to the requested output size.

Run with the isolated venv:
  ~/LocalModelLab/venvs/h3image/bin/python decode_vae.py \
      --latent out.latent --out out.png
"""

import argparse
import json
import os

import numpy as np
import torch
from PIL import Image
from diffusers import AutoencoderKLMiniMaxH3Image

DEFAULT_MODEL_ROOT = os.path.join(os.path.expanduser(os.environ.get('MEDIA_LAB_ROOT','~/LocalModelLab')), 'models/MiniMax-H3-Image')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--latent", required=True,
                        help="raw F32 latent file written by h3 --image")
    parser.add_argument("--out", required=True, help="output PNG path")
    parser.add_argument("--model-root", default=DEFAULT_MODEL_ROOT)
    parser.add_argument("--device", default="mps",
                        choices=["mps", "cpu"])
    args = parser.parse_args()

    meta_path = args.latent + ".meta.json"
    with open(meta_path, "r", encoding="utf-8") as handle:
        meta = json.load(handle)
    if meta.get("format") != "h3-image-latent-v1":
        raise SystemExit(f"unsupported latent format: {meta.get('format')}")
    channels = int(meta["channels"])
    latent_h = int(meta["latent_h"])
    latent_w = int(meta["latent_w"])

    latent = np.fromfile(args.latent, dtype="<f4")
    expected = channels * latent_h * latent_w
    if latent.size != expected:
        raise SystemExit(
            f"latent file holds {latent.size} floats, expected {expected}")

    device = torch.device(args.device)
    vae = AutoencoderKLMiniMaxH3Image.from_pretrained(
        args.model_root, subfolder="vae_diffusers",
        torch_dtype=torch.bfloat16,
        low_cpu_mem_usage=True).to(device).eval()

    latents = torch.from_numpy(latent).reshape(
        1, channels, 1, latent_h, latent_w).to(device)
    # The transformer works on 2D image latents; keep (1, C, H, W).
    latents = latents.squeeze(2)

    mean = torch.tensor(vae.config.latents_mean, device=device).view(1, -1, 1, 1)
    std = torch.tensor(vae.config.latents_std, device=device).view(1, -1, 1, 1)
    latents = latents * std + mean

    with torch.no_grad():
        image = vae.decode(latents.to(torch.bfloat16), return_dict=False)[0]

    # ImageNet constants from the released pipeline (modular_pipeline.py),
    # not stored in the VAE config.
    pixel_mean = torch.tensor((0.485, 0.456, 0.406), device=device).view(1, -1, 1, 1)
    pixel_std = torch.tensor((0.229, 0.224, 0.225), device=device).view(1, -1, 1, 1)
    image = (image.float() * pixel_std + pixel_mean).clamp(0, 1)
    # Match the released pipeline's 8-bit truncation before resizing.
    image = (image * 255).clamp(0, 255).to(torch.uint8).float() / 255

    out_w = int(meta.get("output_width") or image.shape[-1])
    out_h = int(meta.get("output_height") or image.shape[-2])
    array = (image[0].permute(1, 2, 0).cpu().numpy() * 255).round().astype(np.uint8)
    picture = Image.fromarray(array)
    if (out_w, out_h) != picture.size:
        picture = picture.resize((out_w, out_h), Image.LANCZOS)
    picture.save(args.out)
    print(f"wrote {args.out} {out_w}x{out_h} "
          f"(canvas {meta.get('canvas_width')}x{meta.get('canvas_height')}, "
          f"seed {meta.get('seed')})")


if __name__ == "__main__":
    main()
