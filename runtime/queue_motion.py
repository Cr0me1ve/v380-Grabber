#!/usr/bin/python3
import json,os,time,uuid,urllib.request,io
from pathlib import Path
from PIL import Image
from scene_guard import stats
event_at=time.time()
p=Path('/var/lib/motioneye/.telegram-spool')
if not (p/'enabled').exists():raise SystemExit(0)
test=(p/'test-notification').exists();(p/'test-notification').unlink(missing_ok=True)
try:
 st=json.load(urllib.request.urlopen('http://v380-source:8081/health.json',timeout=2))
 if not st['healthy']:raise SystemExit(0)
 connection=urllib.request.urlopen('http://127.0.0.1:7999/1/detection/connection',timeout=2).read()
 if b'Connection OK' not in connection:raise SystemExit(0)
 raw=urllib.request.urlopen('http://v380-source:8081/snapshot.jpg',timeout=2).read(1024*1024)
 scene=stats(Image.open(io.BytesIO(raw)))
 if scene['black']:
  print('Motion notification suppressed: black scene',json.dumps(scene),flush=True);raise SystemExit(0)
 # The source may have changed while checking the detector and frame.
 end=json.load(urllib.request.urlopen('http://v380-source:8081/health.json',timeout=2))
 if not end['healthy'] or end['generation']!=st['generation']:raise SystemExit(0)
except Exception:raise SystemExit(0)
try:settings=json.loads((p/'settings.json').read_text())
except (OSError,ValueError):settings={}
u=uuid.uuid4().hex;job={'id':u,'created':event_at,'camera':1,'source_generation':st['generation'],'scene':scene,'requested_duration':int(settings.get('duration',30)),'pre_roll':float(settings.get('pre_roll',3))}
if test:job['test']=True
t=p/(u+'.tmp')
with open(t,'w') as out:
 os.chmod(t,0o600);json.dump(job,out);out.flush();os.fsync(out.fileno())
t.rename(p/(u+'.json'))
fd=os.open(p,os.O_RDONLY|os.O_DIRECTORY)
try:os.fsync(fd)
finally:os.close(fd)
