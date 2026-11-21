"""Working lightweight TTS adapter using macOS say. Not a neural-model benchmark."""
import json
import os
from pathlib import Path
import subprocess
import sys
root=Path(sys.argv[1]);task=json.loads((root/'task.json').read_text());params=dict(task['parameters'],prompt=task['inputs']['prompt'],options=task['options'])
text=root/'speech.txt';text.write_text(params['prompt'])
options=params.get('options',{})
command=['/usr/bin/say','-f',str(text),'-o',str(root/'speech.aiff')]
if options.get('voice'):command+=['-v',options['voice']]
if options.get('rate'):command+=['-r',str(options['rate'])]
subprocess.run(command,check=True)
subprocess.run([os.environ.get('MEDIA_FFMPEG_BIN','/opt/homebrew/bin/ffmpeg'),'-v','error','-y','-i',str(root/'speech.aiff'),str(root/'output.wav')],check=True)
(root/'result.json').write_text(json.dumps({'schema_version':'gateway.result.v1','artifacts':[{'path':'output.wav','kind':'audio'}],'details':{'backend':'macos-say'}}))
