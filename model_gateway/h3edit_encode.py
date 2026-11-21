#!/usr/bin/env python3
"""H3-Image 编辑参考图编码器（官方 edit 配方复刻）：
resize→bound_short_edge(1024,2048,32)→ImageNet 归一→VAE 后验采样→fp16 舍入回
→裁到整 patch→(lat-mean)/std→F32 [24,lh,lw] 小端 + ".size" 边车 "lh lw"。"""
import sys, os, pathlib, numpy as np, torch
from PIL import Image
from diffusers import AutoencoderKLMiniMaxH3Image

MODEL = str(pathlib.Path(os.environ.get('MEDIA_LAB_ROOT',str(pathlib.Path.home()/'LocalModelLab')))/'models/MiniMax-H3-Image')
PM = torch.tensor([0.485, 0.456, 0.406]).view(1, -1, 1, 1)
PS = torch.tensor([0.229, 0.224, 0.225]).view(1, -1, 1, 1)

def bound(w, h, lo, hi, m=32):
    se = min(w, h)
    if lo <= se <= hi:
        return w, h
    s = (lo if se < lo else hi) / se
    return max(m, round(w * s / m) * m), max(m, round(h * s / m) * m)

src, out_prefix = sys.argv[1], sys.argv[2]
seed = int(sys.argv[3]) if len(sys.argv) > 3 else 42
im = Image.open(src).convert('RGB')
w, h = im.size
rw, rh = bound(w, h, 1024, 2048, 32)
im2 = im.resize((rw, rh), Image.LANCZOS)
x = torch.from_numpy(np.array(im2)).permute(2, 0, 1)[None].float()
x = x[..., :x.shape[-2] // 16 * 16, :x.shape[-1] // 16 * 16]
x = (x / 255.0 - PM) / PS

dev = 'mps' if torch.backends.mps.is_available() else 'cpu'
vae = AutoencoderKLMiniMaxH3Image.from_pretrained(
    MODEL, subfolder='vae_diffusers', torch_dtype=torch.float32).to(dev).eval()
cfg = vae.config
mean = torch.tensor(cfg.latents_mean).view(1, -1, 1, 1).to(dev)
std = torch.tensor(cfg.latents_std).view(1, -1, 1, 1).to(dev)
g = torch.Generator(device=dev); g.manual_seed(seed)
with torch.no_grad():
    post = vae.encode(x.to(dev)).latent_dist
    lat = post.sample(generator=g).to(torch.float16).float()
    lat = lat[..., :lat.shape[-2] // 2 * 2, :lat.shape[-1] // 2 * 2]
    lat = (lat - mean) / std
arr = lat[0].detach().cpu().numpy().astype('<f4')
assert arr.shape[0] == 24, arr.shape
arr.tofile(out_prefix + '.f32')
open(out_prefix + '.f32.size', 'w').write(f"{arr.shape[1]} {arr.shape[2]}\n")
print('ref latent', arr.shape, '->', out_prefix + '.f32 / .size')
