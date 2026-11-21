import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('qwen_adapter', Path(__file__).resolve().parents[1] / 'examples/qwen3_tts_mlx.py')
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


class QwenAdapterTests(unittest.TestCase):
    def test_rejects_unsupported_performance_controls(self):
        for option in ('instructions', 'speed', 'voice'):
            with self.assertRaises(ValueError):
                adapter.generation_args({'inputs': {'prompt': 'hello'}, 'options': {option: 'x'}}, Path('.'))

    def test_reference_requires_matching_transcript_and_contained_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / 'voice.wav').write_bytes(b'fixture')
            task = {'inputs': {'prompt': 'hello', 'assets': [{'role': 'reference_audio', 'path': 'voice.wav'}]}, 'options': {}}
            with self.assertRaises(ValueError): adapter.generation_args(task, root)
            task['options']['ref_text'] = 'reference words'
            result = adapter.generation_args(task, root)
            self.assertEqual(result['ref_text'], 'reference words')
            task['inputs']['assets'][0]['path'] = '../outside.wav'
            with self.assertRaises(ValueError): adapter.generation_args(task, root)

    def test_plain_synthesis_has_no_fake_emotion_argument(self):
        result = adapter.generation_args({'inputs': {'prompt': 'hello'}, 'options': {'temperature': 0.8}}, Path('.'))
        self.assertNotIn('instruct', result)
        self.assertNotIn('speed', result)
        self.assertEqual(result['temperature'], 0.8)
