#!/usr/bin/env python3
"""MOSS-Image edit CLI: reference-prefix image editing on Apple Silicon (MPS).

Contract ported from chinoll/ComfyUI-Moss-Image-Edit (ref-prefix-only-t0):
- source image (+ optional extra references) VAE-encoded, token-embedded and
  PREPENDED to the DiT sequence with t=0 modulation (zero_vec)
- asymmetric attention: prefix tokens see only themselves; target+text see all
- target latents start from pure noise; only the target span is decoded
- PEFT adapter (format moss-edit-lora) merged into the DiT weights up-front

Runtime reuses the official OpenMOSS MPS path (mps_runtime.py: staged
text-encoder/DiT swapping, float64-on-CPU timestep patch, GIL-safe sync).
"""
import argparse, json, pathlib, sys, time

import torch
import torch.nn.functional as F
from PIL import Image

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "moss_edit_deps"))


def log(msg):
    print(f"[moss-edit] {msg}", flush=True)


def merge_adapter(transformer, adapter_dir, strength=1.0):
    """Merge moss-edit-lora deltas into DiT weights (W += B@A * alpha/rank)."""
    from safetensors.torch import load_file
    config = json.loads((pathlib.Path(adapter_dir) / "adapter_config.json").read_text())
    if config.get("format") != "moss-edit-lora" or config.get("format_version") != 1:
        raise ValueError(f"unsupported adapter format: {config.get('format')}")
    if config.get("metadata", {}).get("edit_contract") != "ref-prefix-only-t0__target-text-original-moss-v1":
        raise ValueError("unsupported edit contract")
    state = load_file(str(pathlib.Path(adapter_dir) / "adapter_model.safetensors"))
    merged = 0
    for target, spec in config["modules"].items():
        a = state[target + ".lora_A"].float()   # [rank, in]
        b = state[target + ".lora_B"].float()   # [out, rank]
        scale = float(spec.get("alpha", spec["rank"])) / float(spec["rank"])
        parts = target.split(".")
        module = transformer
        for part in parts:
            module = module[int(part)] if part.isdigit() else getattr(module, part)
        weight = module.weight
        delta = (b @ a).to(device=weight.device, dtype=weight.dtype)
        if delta.shape != weight.shape:
            raise ValueError(f"{target}: adapter delta {tuple(delta.shape)} != weight {tuple(weight.shape)}")
        with torch.no_grad():
            weight.add_(delta, alpha=scale * strength)
        merged += 1
    log(f"adapter merged into {merged} modules (strength={strength})")


class ReferenceEditor:
    """Wraps the DiT with ref-prefix conditioning (mirrors native_edit.py)."""

    def __init__(self, module, transformer, ref_latents, ref_max_edge):
        self.module = module
        self.model = transformer
        self.ref_pe_cache = None
        self.prefix_length = 0
        self.refs = []
        for index, ref in enumerate(ref_latents):
            self.refs.append(transformer.video_embedder(
                ref.to(transformer_dtype(transformer)).flatten(2).transpose(1, 2)))
            h, w = ref.shape[-2:]
            ids = torch.zeros(h * w, 4, device=ref.device, dtype=torch.float32)
            ids[:, 0] = index + 1
            ids[:, 2] = (torch.arange(h, device=ref.device, dtype=torch.float32) - (h - 1) / 2).repeat_interleave(w)
            ids[:, 3] = (torch.arange(w, device=ref.device, dtype=torch.float32) - (w - 1) / 2).repeat(h)
            phase = ids.unsqueeze(-1) * transformer.rope.bands
            pe = torch.cat((phase.sin(), phase.cos()), -2).flatten(-2).unsqueeze(0)
            self.prefix_length += h * w
            self.ref_pe_cache = pe if self.ref_pe_cache is None else torch.cat((self.ref_pe_cache, pe), 1)

    def forward(self, hidden_states, timestep, encoder_hidden_states, encoder_attention_mask=None,
                return_dict=True, skip_layers=None):
        torch_mod = self.module.torch
        model = self.model
        batch, _, height, width = hidden_states.shape
        device = hidden_states.device
        dtype = model.video_embedder.weight.dtype
        text = encoder_hidden_states
        mask = encoder_attention_mask if encoder_attention_mask is not None else torch.ones(
            text.shape[:2], dtype=torch.bool, device=device)
        context = text[0][mask[0]]  # single image per job, no padding path
        if context.shape[0] == 0:
            raise ValueError("empty text context")

        # timestep sinusoid in float64 on CPU (MPS has no float64), per mps_runtime patch
        half = model.config.freq_dim // 2
        freq = 10000 ** (-torch.arange(half, dtype=torch.float64) / half)
        times = timestep.reshape(-1).expand(batch).cpu().double()[:, None]
        embedding = torch.cat(((times * freq).cos(), (times * freq).sin()), -1).float().to(device)
        vec = model.time_embedding(embedding.to(model.time_embedding[0].weight.dtype))
        zero = model.time_embedding(torch.zeros_like(embedding).to(vec.dtype))
        mod_body = model.shared_block_modulation(vec).view(batch, 6, -1).unbind(1)
        mod_ref = model.shared_block_modulation(zero).view(batch, 6, -1).unbind(1)
        head = model.video_head_modulation(vec).view(batch, 2, -1)

        text_length = context.shape[0]
        rope_all = torch.cat((self.ref_pe_cache,
                              model.rope(height, width, text_length, device)), 1).to(dtype)
        prefix = self.prefix_length

        text_tokens = model.text_embedder(context[None].to(dtype), rope_all[:, prefix + height * width:])
        body = torch.cat((model.video_embedder(hidden_states.flatten(2).transpose(1, 2).to(dtype)),
                          text_tokens), 1)
        x = torch.cat((*self.refs, body), 1)
        body_length = body.shape[1]
        mods = tuple(torch.cat((r.expand(batch, prefix, -1), t.expand(batch, body_length, -1)), 1)
                     for r, t in zip(mod_ref, mod_body))

        for index, layer in enumerate(model.layers):
            if skip_layers and index in skip_layers:
                continue
            x = self._block_edit(layer, x, mods, rope_all)

        target = x[:, prefix:prefix + height * width]
        out = model.final_norm_video(target.to(dtype))
        out = out * (1 + head[:, 1, None]) + head[:, 0, None]
        out = model.final_linear_video(out.to(dtype))
        sample = out.transpose(1, 2).reshape(batch, -1, height, width)
        return sample

    @staticmethod
    def _block_edit(block, x, mods, rope):
        shift_a, scale_a, gate_a, shift_f, scale_f, gate_f = mods
        y = block.norm1(x) * (1 + scale_a) + shift_a
        dtype = block.proj["q"].weight.dtype
        y = y.to(dtype)
        q = block.q_norm(block.proj["q"](y).unflatten(-1, (block.heads, -1))).flatten(2)
        k = block.k_norm(block.proj["k"](y).unflatten(-1, (block.heads, -1))).flatten(2)
        x = x + block.proj["o"](ReferenceEditor._attention(q, k, block.proj["v"](y), rope, block.heads)) * gate_a
        y = block.norm2(x) * (1 + scale_f) + shift_f
        hidden, gate = block.proj["ffn_up_gate"](y.to(dtype)).chunk(2, -1)
        return x + block.proj["ffn_down"](ReferenceEditor._situ(hidden, gate)) * gate_f

    _attention = None       # installed by install_attention_patch
    _situ = None            # module-level _situ from pipeline module


def transformer_dtype(transformer):
    return transformer.video_embedder.weight.dtype


def install_attention_patch(module):
    """Two-segment SDPA: prefix sees prefix only; body sees everything."""
    original = module._attention
    state = {"prefix": 0}

    def _sdpa(q, k, v, qrope, krope, heads):
        q_shape = (*q.shape[:2], heads, q.shape[-1] // heads)
        k_shape = (*k.shape[:2], heads, k.shape[-1] // heads)
        q = module._rotate(q.reshape(q_shape), qrope).transpose(1, 2)
        k = module._rotate(k.reshape(k_shape), krope).transpose(1, 2)
        v = v.reshape(k_shape).transpose(1, 2)
        return F.scaled_dot_product_attention(q.to(v.dtype), k.to(v.dtype), v).transpose(1, 2).flatten(2)

    def attention(q, k, v, rope, heads):
        prefix = state["prefix"]
        if not prefix:
            return original(q, k, v, rope, heads)
        pre = _sdpa(q[:, :prefix], k[:, :prefix], v[:, :prefix], rope[:, :prefix], rope[:, :prefix], heads)
        body = _sdpa(q[:, prefix:], k, v, rope[:, prefix:], rope, heads)
        return torch.cat((pre, body), dim=1)

    def set_prefix_length(value):
        state["prefix"] = value

    module._attention = attention
    ReferenceEditor._attention = attention
    ReferenceEditor._situ = module._situ
    return set_prefix_length


def encode_reference(module, pipe, pil_image, max_edge):
    """Resize (align 16, long edge <= max_edge) then FLUX.2 VAE encode + pack + bn-normalize."""
    width, height = pil_image.size
    scale = min(1.0, max_edge / max(width, height))
    new_w = max(16, round(width * scale / 16) * 16)
    new_h = max(16, round(height * scale / 16) * 16)
    if (new_w, new_h) != (width, height):
        pil_image = pil_image.resize((new_w, new_h), Image.LANCZOS)
    import numpy as np
    array = np.asarray(pil_image, dtype="float32") / 127.5 - 1.0
    tensor = torch.from_numpy(array).permute(2, 0, 1)[None].to(pipe.vae.device, dtype=pipe.vae.dtype)
    with torch.no_grad():
        z = pipe.vae.encode(tensor).latent_dist.sample()
    batch, channels, big_h, big_w = z.shape
    small_h, small_w = big_h // 2, big_w // 2
    z = z.reshape(batch, channels, small_h, 2, small_w, 2).permute(0, 1, 3, 5, 2, 4)
    z = z.reshape(batch, channels * 4, small_h, small_w)
    bn = pipe.vae.bn
    eps = pipe.vae.config.batch_norm_eps
    z = (z - bn.running_mean.view(1, -1, 1, 1)) / (bn.running_var + eps).sqrt().view(1, -1, 1, 1)
    return z


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--adapter", required=True)
    parser.add_argument("--input", required=True, help="source image to edit")
    parser.add_argument("--reference", action="append", default=[], help="extra reference image(s), repeatable")
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--negative", default="")
    parser.add_argument("--output", required=True)
    parser.add_argument("--width", type=int, default=1024)
    parser.add_argument("--height", type=int, default=1024)
    parser.add_argument("--steps", type=int, default=50)
    parser.add_argument("--guidance", type=float, default=3.0)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--ref-max-edge", type=int, default=512)
    parser.add_argument("--strength", type=float, default=1.0)
    args = parser.parse_args()
    if args.width % 16 or args.height % 16:
        raise SystemExit("宽高必须16对齐")

    import mps_runtime
    module = mps_runtime._import_pipeline_module(pathlib.Path(args.model_dir))
    set_prefix_length = install_attention_patch(module)

    log("loading pipeline (staged MPS: VAE + DiT first, text encoder swaps in)")
    pipe = mps_runtime.load_pipeline(args.model_dir, local_files_only=True)
    merge_adapter(pipe.transformer, args.adapter, args.strength)

    log("encoding references")
    ref_images = [Image.open(args.input).convert("RGB")] + [Image.open(p).convert("RGB") for p in args.reference]
    ref_latents = [encode_reference(module, pipe, image, args.ref_max_edge) for image in ref_images]
    editor = ReferenceEditor(module, pipe.transformer, ref_latents, args.ref_max_edge)
    set_prefix_length(editor.prefix_length)
    editor_module_forward = editor.forward
    pipe.transformer.forward = lambda *a, **k: editor_module_forward(*a, **k)

    log("prompt encode")
    pos_embeds, pos_mask = pipe.encode_prompt(args.prompt)
    do_cfg = args.guidance != 1.0
    if do_cfg:
        neg_embeds, neg_mask = pipe.encode_prompt(args.negative if args.negative else "")

    torch.manual_seed(args.seed)
    device = torch.device("mps")
    # 分阶段加载：encode_prompt 已把 pipe.transformer 释放为 None；去噪循环用的是
    # editor 持有的原 DiT 模块（adapter 已合并），latent 通道数从它取，不重新加载 DiT。
    shape = (1, editor.model.config.latent_channels, args.height // 16, args.width // 16)
    latents = torch.randn(shape, device=device, dtype=torch.float32)
    pipe.scheduler.set_timesteps(args.steps, device=device)
    start = time.monotonic()
    with torch.inference_mode():
        for step_index, timestep in enumerate(pipe.scheduler.timesteps):
            prediction = editor_module_forward(latents, timestep, pos_embeds, pos_mask).float()
            if do_cfg:
                uncond = editor_module_forward(latents, timestep, neg_embeds, neg_mask,
                                               skip_layers=(22,)).float()
                prediction = uncond + args.guidance * (prediction - uncond)
            latents = pipe.scheduler.step(prediction, timestep, latents, return_dict=False)[0]
            if (step_index + 1) % 5 == 0 or step_index == 0:
                log(f"step {step_index + 1}/{len(pipe.scheduler.timesteps)} "
                    f"({time.monotonic() - start:.0f}s elapsed)")

    log("decoding")
    z = latents.to(dtype=pipe.vae.dtype)
    bn = pipe.vae.bn
    eps = pipe.vae.config.batch_norm_eps
    z = z * (bn.running_var + eps).sqrt().to(z).view(1, -1, 1, 1) + bn.running_mean.to(z).view(1, -1, 1, 1)
    b, c, h, w = z.shape
    z = z.reshape(b, c // 4, 2, 2, h, w).permute(0, 1, 4, 2, 5, 3).reshape(b, c // 4, 2 * h, 2 * w)
    image = pipe.vae.decode(z, return_dict=False)[0].clamp(-1, 1)
    array = ((image[0].permute(1, 2, 0).float().cpu().detach().numpy() + 1) * 127.5).clip(0, 255).astype("uint8")
    Image.fromarray(array).save(args.output)
    log(f"saved {args.output} total={time.monotonic() - start:.0f}s")


if __name__ == "__main__":
    main()
