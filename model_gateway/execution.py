"""Runner contract shared by built-in and operator-provided script engines."""
import json
from pathlib import Path


def checked_output(job_dir):
    root=Path(job_dir).resolve()
    result=json.loads((root/'result.json').read_text())
    return checked_file(root,result['file'],json.loads((root/'input.json').read_text())['kind'])

def checked_file(job_dir,name,kind):
    root=Path(job_dir).resolve()
    file=Path(name)
    if file.is_absolute():raise ValueError('result.file must be relative')
    output=(root/file).resolve()
    if not output.is_relative_to(root) or not output.is_file() or output.stat().st_size==0:raise ValueError('Result must be a nonempty file inside the task directory')
    if output.suffix.lower() not in {'image':{'.png','.jpg','.jpeg','.webp'},'video':{'.mp4','.mov'},'audio':{'.wav','.mp3','.flac'}}[kind]:raise ValueError('Result extension does not match task kind')
    return output
