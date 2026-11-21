"""Local Qwen3-TTS Base adapter; inference remains in the gateway process group."""
import json
from pathlib import Path
import sys


def generation_args(task, root):
    options = task.get('options', {})
    allowed = {'temperature', 'ref_text', 'max_tokens'}
    if set(options) - allowed:
        raise ValueError('Unsupported Qwen Base options')
    assets = task['inputs'].get('assets', [])
    if len(assets) > 1 or any(a['role'] != 'reference_audio' for a in assets):
        raise ValueError('Expected at most one reference_audio')
    args = dict(text=task['inputs']['prompt'], lang_code='chinese',
                temperature=options.get('temperature', 0.7),
                max_tokens=options.get('max_tokens', 512), verbose=False)
    if assets:
        path = (root / assets[0]['path']).resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file():
            raise ValueError('Invalid reference path')
        if not options.get('ref_text', '').strip():
            raise ValueError('Reference transcript required')
        args.update(ref_audio=str(path), ref_text=options['ref_text'])
    elif options.get('ref_text'):
        raise ValueError('ref_text requires reference_audio')
    return args


def main(root):
    import numpy as np
    import wave
    from mlx_audio.tts.utils import load_model
    task = json.loads((root / 'task.json').read_text())
    execution = json.loads((root / 'execution.json').read_text())
    args = generation_args(task, root)
    model_dir = Path(execution['model_dir'])
    if not model_dir.is_dir():
        raise ValueError('Local model directory missing; automatic download disabled')
    model = load_model(model_dir)
    chunks = [np.asarray(r.audio, dtype=np.float32).reshape(-1)
              for r in model.generate(**args)]
    if not chunks or not sum(c.size for c in chunks):
        raise ValueError('No speech generated')
    audio = np.concatenate(chunks)
    if not np.isfinite(audio).all() or np.max(np.abs(audio)) < 1e-5:
        raise ValueError('Invalid or silent speech')
    with wave.open(str(root / 'output.wav'), 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(int(model.sample_rate))
        wav.writeframes((np.clip(audio, -1, 1) * 32767).astype('<i2').tobytes())
    (root / 'result.json').write_text(json.dumps({
        'schema_version': 'gateway.result.v1',
        'artifacts': [{'path': 'output.wav', 'kind': 'audio'}],
        'details': {'backend': 'qwen3-tts-mlx', 'neural_tts': True,
                    'sample_rate': int(model.sample_rate),
                    'emotion_instructions_supported': False,
                    'reference_conditioned': bool(task['inputs'].get('assets'))}}))


if __name__ == '__main__':
    main(Path(sys.argv[1]).resolve())
