"""File IPC client: only screenshot/Qt GUI primitives; no eval or CAD construction."""
import argparse
import json
from pathlib import Path
import time
import uuid

def request(run,method,args=None,timeout=15):
    run=Path(run).resolve()
    identifier=str(time.time_ns())+'_'+uuid.uuid4().hex[:8]
    payload={'id':identifier,'method':method,'args':args or {},'submitted_at':time.time()}
    path=run/'requests'/(identifier+'.json')
    if not path.parent.is_dir():raise RuntimeError('Background bridge is not ready')
    tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(payload),encoding='utf-8');tmp.replace(path)
    response=run/'responses'/path.name
    deadline=time.monotonic()+timeout
    while not response.exists():
        if time.monotonic()>deadline:raise TimeoutError('Request may still be pending. Do not resend input blindly: '+identifier)
        time.sleep(.05)
    return json.loads(response.read_text(encoding='utf-8'))

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--run',type=Path,required=True)
    parser.add_argument('method')
    parser.add_argument('--args',default='{}')
    parser.add_argument('--timeout',type=float,default=15)
    parser.add_argument('--brief',action='store_true')
    args=parser.parse_args()
    result=request(args.run,args.method,json.loads(args.args),args.timeout)
    if args.brief:
        result={k:v for k,v in result.items() if k not in ('tree','events')}
    print(json.dumps(result,ensure_ascii=False,indent=2))
