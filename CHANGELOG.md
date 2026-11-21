# Changelog

## 0.1.0 — unreleased

- H3-Image reference edits accept up to 16 reference images: legacy `edit_image_base64` stays single-slot, new `edit_images_base64` / v1 `edit_image` role carry the list; files land as `edit-image-XX.png` and bind to `<Picture 1..N>` by sorted filename. The h3-image CLI takes repeated `--edit-image/--edit-latent` pairs (ordered) and now fails clearly when `--ref-image` is passed in image mode.
- Add `vpipe-h3` video profile: MiniMax-H3 generation through vpipe (FL2VA first/last-frame VAE conditioning + text prompt; `pika_flex_fl2va` 6 steps / `turbo_fl2va_lite` 4 steps / no-LoRA 16 steps), max 10 s per job, canvas ≤1032192 px and ≤1536 edge.
- Add persistent queue draining, per-task pause/resume, queue ordering and per-item batch results.
- Gate process suspension on an explicit engine capability; paused tasks retain memory and their execution slot.
- Preserve all unfinished tasks in history listings and exclude manual pause time from timeout budgets.

- Introduce Gateway Task v1 requests, resolved task documents, results and progress schemas.
- Preserve compatibility callers through the same task contract; support declared media roles in script engines.
- Show full task inputs, media previews, timings, process-tree RSS / MLX peaks and execution identity.
- Add editable retry drafts with complete media references, hashes and derived-task lineage.

- Extract local model gateway from video-production-system, macOS only.
- Preserve native job lifecycle and Firebao image/video/text compatibility.
- Remove production project and candidate-review responsibilities.
- Add configurable model/engine profiles, immutable execution snapshots and script adapters.
- Add native audio jobs, a macOS system TTS example, CLI, probes and validation endpoint.
- Add output containment checks, standalone operations docs and macOS CI.

No public release has been published; inherited-code license review is pending.
