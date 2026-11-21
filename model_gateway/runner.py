"""One isolated process per task; no shell interpolation of user input."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from model_gateway.execution import checked_output
from model_gateway import task_v1 as protocol


def main():
    directory=Path(sys.argv[1]).resolve()
    snapshot_file=directory/'execution.json'
    if snapshot_file.exists():snapshot=json.loads(snapshot_file.read_text())
    else:raise RuntimeError('Missing execution snapshot; migrate legacy queued tasks before enabling execution')
    if not (directory/'task.json').exists():
        row=json.loads((directory/'job.json').read_text()) if (directory/'job.json').exists() else {'id':directory.name,'parameters':json.loads((directory/'input.json').read_text()),'execution':snapshot}
        protocol.persist(directory,row)
    protocol.TaskDocument(**json.loads((directory/'task.json').read_text()))
    engine=snapshot['engine'];env=dict(os.environ,**snapshot.get('runtime_env',{}),MEDIA_LAB_ROOT=snapshot['lab_root'])
    if engine['type']=='builtin':
        command=[sys.executable,str(Path(__file__).with_name('builtin_runner.py')),str(directory)]
    elif engine['type']=='script':
        substitutions={'job_dir':str(directory),'python':sys.executable,'config_dir':snapshot['config_dir']}
        command=[arg.format(**substitutions) for arg in engine['argv']]
        env.update(engine.get('env',{}))
    else:raise ValueError('Unsupported engine')
    env['GATEWAY_TASK_FILE']=str(directory/'task.json')
    env['GATEWAY_EXECUTION_FILE']=str(directory/'execution.json')
    started=time.monotonic()
    subprocess.run(command,env=env,check=True)
    if engine.get('contract')=='gateway.task.v1' and (directory/'stage.json').exists():
        protocol.Progress(**json.loads((directory/'stage.json').read_text()))
    path=directory/'result.json';result=json.loads(path.read_text())
    if engine.get('contract')=='gateway.task.v1' or result.get('schema_version')=='gateway.result.v1':
        parsed=protocol.EngineResult(**result)
        for artifact in parsed.artifacts:
            from model_gateway.execution import checked_file
            checked_file(directory,artifact.path,artifact.kind)
        task=json.loads((directory/'task.json').read_text())
        if parsed.artifacts[0].kind!=task['kind']:raise ValueError('Primary artifact kind does not match task')
        result=dict(parsed.model_dump(),file=parsed.artifacts[0].path)
    else:
        checked_output(directory)
        result.update(schema_version='gateway.result.v1',artifacts=[{'path':result['file'],'kind':json.loads((directory/'task.json').read_text())['kind']}],metrics={k:result[k] for k in ('total_seconds','load_seconds','generation_seconds','mlx_peak_bytes') if k in result})
    result.update(profile_id=snapshot['profile_id'],engine_id=snapshot['profile']['engine'],model_id=snapshot['profile']['model_id'],registry_revision=snapshot['registry_revision'])
    result.setdefault('total_seconds',time.monotonic()-started)
    result.setdefault('parameters',json.loads((directory/'input.json').read_text()))
    temporary=directory/'result.tmp';temporary.write_text(json.dumps(result,ensure_ascii=False,indent=2));temporary.replace(path)

if __name__=='__main__':main()
