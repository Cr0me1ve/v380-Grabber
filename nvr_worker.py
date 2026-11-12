#!/usr/bin/python3
"""Durable motion notification queue; capture buffered events, then send through Bot API."""
import datetime,json,os,subprocess,time,urllib.request,urllib.error,uuid,threading,signal,shutil,math
from pathlib import Path
from zoneinfo import ZoneInfo
SPOOL=STATE=MESSAGES=STATUS_FILE=None
RUNTIME=None
C={}
def configure(runtime):
 global RUNTIME,SPOOL,STATE,MESSAGES,STATUS_FILE,C,MONITOR_CACHE,STOP
 RUNTIME=runtime;SPOOL=runtime.data/'recordings/.telegram-spool';STATE=runtime.data/'telegram-state'
 STATE.mkdir(parents=True,exist_ok=True,mode=0o700)
 MESSAGES=STATE/'messages';MESSAGES.mkdir(exist_ok=True,mode=0o700);STATUS_FILE=STATE/'monitor.json'
 class Context:
  def __getitem__(self,key):return runtime.api._token if key=='token' else (runtime.store.owner or {}).get('chat_id',0)
 C=Context();MONITOR_CACHE={};STOP=runtime.stop_event

def log(s):print(datetime.datetime.now(datetime.timezone.utc).isoformat(),s,flush=True)
def save(p,d):
 t=p.with_suffix('.tmp')
 with open(t,'w') as f:
  os.chmod(t,0o600);json.dump(d,f);f.flush();os.fsync(f.fileno())
 t.replace(p)
 fd=os.open(p.parent,os.O_RDONLY|os.O_DIRECTORY)
 try:os.fsync(fd)
 finally:os.close(fd)
def safe_json(p,default):
 try:
  d=json.loads(p.read_text());return d if isinstance(d,type(default)) else default
 except (OSError,ValueError):return default
def api(method,fields,video=None,photo=None):
 if video:result=RUNTIME.api.send_video(fields['chat_id'],video,fields.get('caption',''))
 elif photo:result=RUNTIME.api.send_photo(fields['chat_id'],photo.read_bytes(),fields.get('caption',''))
 else:result=RUNTIME.api._json_call(method,fields)
 if method.startswith('send') and (not isinstance(result,dict) or type(result.get('message_id')) is not int or result['message_id']<=0):raise RuntimeError('Telegram did not acknowledge delivery')
 return result

def record(job,video):
 completed=(SPOOL/(job['id']+'.complete')).exists() and video.exists()
 if not completed:
  if shutil.disk_usage(SPOOL).free<float(RUNTIME.settings()['reserve_gb'])*1024**3+20*1024**2:raise RuntimeError('Disk reserve reached')
  if sum(p.stat().st_size for p in SPOOL.glob('*.mp4'))>256*1024**2:raise RuntimeError('Notification queue disk limit')
 subprocess.run(['docker','exec','v380-grabber-motioneye-1','python3','/helpers/capture_clip.py',job['id'],job.get('source_generation',''),str(job['created']),str(job.get('requested_duration',30)),str(job.get('pre_roll',3))],capture_output=True,timeout=job.get('requested_duration',30)+45,check=True)
 inside='/var/lib/motioneye/.telegram-spool/'+job['id']+'.mp4'
 r=subprocess.run(['docker','exec','v380-grabber-motioneye-1','ffprobe','-v','error','-show_entries','format=duration','-of','default=nw=1:nk=1',inside],capture_output=True,text=True,timeout=10,check=True)
 duration=float(r.stdout.strip())
 if not job.get('requested_duration',30)-.2<=duration<=job.get('requested_duration',30)+.5:raise RuntimeError('Incomplete video duration')
 return duration
class InvalidFootage(RuntimeError):pass
class EmptyFootage(InvalidFootage):pass
def validate_media(job):
 inside='/var/lib/motioneye/.telegram-spool/'+job['id']+'.mp4'
 minimum=[str(max(3,int(job.get('duration',30)*5)-2))]
 r=subprocess.run(['docker','exec','v380-grabber-motioneye-1','python3','/helpers/clock_guard.py',inside]+minimum,capture_output=True,text=True,timeout=max(20,int(job.get('duration',30)*.5)))
 if r.returncode:raise InvalidFootage('Camera timestamp validation failed')
 r=subprocess.run(['docker','exec','v380-grabber-motioneye-1','python3','/helpers/scene_guard.py',inside]+minimum,capture_output=True,text=True,timeout=max(20,int(job.get('duration',30)*.5)))
 if r.returncode==3:raise EmptyFootage('Empty black camera scene')
 if r.returncode:raise RuntimeError('Scene validation failed')
def recover_archive(job):
 root=RUNTIME.data/'recordings'/RUNTIME.settings()['archive_subdir']
 moment=datetime.datetime.fromtimestamp(job['created'],ZoneInfo(RUNTIME.settings()['timezone']))
 folder=root/moment.strftime('%Y-%m-%d');candidates=[]
 for archive in folder.glob('*.mp4'):
  try:
   started=datetime.datetime.strptime(folder.name+' '+archive.stem,'%Y-%m-%d %H-%M-%S').replace(tzinfo=ZoneInfo(RUNTIME.settings()['timezone'])).timestamp()
   delta=job['created']-started
   if -5<=delta<=20 and time.time()-archive.stat().st_mtime>=3:candidates.append((abs(delta),archive))
  except (ValueError,OSError):pass
 for _,archive in sorted(candidates):
  relative=str(archive.relative_to(root));rejected=job.get('archive_rejected',{})
  if rejected.get('path')==relative and rejected.get('mtime')==archive.stat().st_mtime:continue
  inside='/var/lib/motioneye/'+RUNTIME.settings()['archive_subdir']+'/'+relative
  code="from pathlib import Path;import os,sys;opened=False;\nfor p in Path('/proc').glob('[0-9]*/fd/*'):\n try:\n  if os.readlink(p)==sys.argv[1]:opened=True;break\n except OSError:pass\nprint('OPEN' if opened else 'CLOSED')"
  r=subprocess.run(['docker','exec','v380-grabber-motioneye-1','python3','-c',code,inside],capture_output=True,text=True,timeout=6)
  if r.returncode or r.stdout.strip()!='CLOSED':continue
  r=subprocess.run(['docker','exec','v380-grabber-motioneye-1','python3','/helpers/recover_archive.py',job['id'],inside,str(job.get('requested_duration',30))],capture_output=True,text=True,timeout=max(45,job.get('requested_duration',30)+15))
  if r.returncode==3:raise EmptyFootage('Empty archived scene')
  if r.returncode==4:
   job['archive_rejected']={'path':relative,'mtime':archive.stat().st_mtime};continue
  if r.returncode:raise RuntimeError('Archive recovery helper failed')
  return json.loads(r.stdout)
 return None

def capture_error(e):
 detail=type(e).__name__
 if isinstance(e,subprocess.CalledProcessError) and e.stderr:
  tail=e.stderr.decode(errors='replace') if isinstance(e.stderr,bytes) else e.stderr
  detail+=': '+tail.strip().splitlines()[-1][:250]
 return detail.replace(C['token'],'[redacted]')

def process(p,stage):
 job=json.loads(p.read_text());now=time.time()
 if not isinstance(job,dict) or job.get('id')!=p.stem or len(p.stem)!=32 or any(c not in '0123456789abcdef' for c in p.stem) or not isinstance(job.get('created'),(int,float)) or not math.isfinite(job['created']):raise ValueError('Malformed job')
 video=SPOOL/(job['id']+'.mp4')
 if job.get('done'):return
 if stage=='capture' and job.get('captured'):return
 if stage=='send' and not job.get('captured'):return
 if not job.get('captured') and now-job['created']>172800:
  job.update(done=True,failed=True);save(p,job);return
 if now<job.get('next_try',0):return
 if not job.get('captured'):
  if job.get('archive_pending') or now-job['created']>90:
   job['archive_pending']=True;result=recover_archive(job)
   if result:
    job.update(duration=result['duration'],captured=True,recovered=bool(result.get('recovered_from_archive')),archive=result.get('recovered_from_archive'));save(p,job);log('Recovered archived motion clip '+job['id']);return
   if now-job['created']>600:
    job.update(done=True,failed=True,skipped='no validated recording available')
    enqueue_message('Could not save the camera event at '+datetime.datetime.fromtimestamp(job['created'],ZoneInfo(RUNTIME.settings()['timezone'])).strftime('%H:%M:%S')+'. No valid footage was found in the archive.')
   else:job['next_try']=now+5
   save(p,job);return
  if (SPOOL/(job['id']+'.complete')).exists() and video.exists():
   job['duration']=record(job,video);job['captured']=True;save(p,job);return
  last=STATE/'last-event.json';prev=safe_json(last,{});previous=safe_json(SPOOL/(prev.get('id','none')+'.json'),{})
  if job['created']<prev.get('created',0)+max(1,previous.get('requested_duration',30)-previous.get('pre_roll',3)) and prev.get('id')!=job['id'] and not previous.get('archive_pending'):
   job.update(done=True,skipped='overlapping event');save(p,job);return
  save(last,{'created':job['created'],'id':job['id']});log('Recording motion clip '+job['id'])
  try:job['duration']=record(job,video);job['captured']=True
  except Exception as e:
   job.update(archive_pending=True,capture_error=capture_error(e),next_try=time.time()+5);log('Live capture interrupted; waiting for event archive '+job['id']+' '+job['capture_error'])
  save(p,job);return
 validate_media(job)
 moment=datetime.datetime.fromtimestamp(job['created'],ZoneInfo(RUNTIME.settings()['timezone'])).strftime('%Y-%m-%d %H:%M:%S %Z')
 caption=('Notification test — ' if job.get('test') else 'Motion — ')+RUNTIME.settings()['camera_name']+'\n'+moment
 if job.get('recovered'):caption+='\nRecovered from archive: '+str(round(job['duration'],1))+' seconds, muted'
 else:caption+='\n'+str(round(job['duration'],1))+' seconds, muted'
 r=api('sendVideo',{'chat_id':C['chat_id'],'caption':caption,'duration':round(job['duration']),'width':640,'height':360,'supports_streaming':'true'},video)
 if not isinstance(r,dict) or type(r.get('message_id')) is not int or r['message_id']<=0:raise RuntimeError('Telegram did not acknowledge the video')
 job.update(done=True,sent_at=time.time(),message_id=r['message_id']);save(p,job);log('Delivered motion clip '+job['id'])
def clean():
 for p in SPOOL.glob('*.json'):
  try:
   d=json.loads(p.read_text())
   if d.get('id')!=p.stem or len(p.stem)!=32 or any(c not in '0123456789abcdef' for c in p.stem):continue
   if d.get('done') and time.time()-p.stat().st_mtime>86400:
    for f in SPOOL.glob(d['id']+'.*'):f.unlink(missing_ok=True)
  except (OSError,ValueError):pass
STOP=threading.Event()
def work(stage):
 while not STOP.is_set():
  for p in sorted(SPOOL.glob('*.json')):
   if p.stem=='settings':continue
   if STOP.is_set():break
   try:process(p,stage)
   except EmptyFootage:
    d=safe_json(p,{});d.update(done=True,skipped='empty black camera scene');save(p,d);log('Suppressed black motion clip '+p.stem)
   except InvalidFootage:
    d=safe_json(p,{});d.update(done=True,skipped='invalid camera footage');save(p,d);log('Rejected non-camera footage '+p.stem)
   except (ValueError,KeyError):
    p.rename(p.with_suffix('.corrupt'));log('Quarantined malformed motion job '+p.stem)
   except Exception as e:
    try:
     d=json.loads(p.read_text());d['attempts']=d.get('attempts',0)+1
     d['next_try']=time.time()+min(30*2**min(d['attempts'],6),1800)
     save(p,d)
    except Exception:pass
    log('Queue retry: '+type(e).__name__+' '+p.stem)
  STOP.wait(1)
"""Owner-only commands, durable status notifications and NVR health recovery."""
import socket
# Paths are initialized by configure().
MONITOR_CACHE={};CACHE_LOCK=threading.Lock()
RECOVERED={}
def enqueue_message(text):
 u=uuid.uuid4().hex;save(MESSAGES/(str(time.time_ns())+'-'+u+'.json'),{'text':text,'created':time.time()})
def inspect_container(name):
 r=subprocess.run(['docker','inspect',name],capture_output=True,text=True,timeout=5,check=True)
 d=json.loads(r.stdout)[0];return d['State']['Running'],d['NetworkSettings']['Networks'].get('v380-camera',{}).get('IPAddress')
def probe():
 result={'camera':False,'motion':False,'reason':'Starting','checked':time.time(),'source_service_failed':True}
 try:
  running,ip=inspect_container('v380-grabber-selector-1')
  if not running:result['reason']='Camera receiver stopped';return result
  if not ip:result['reason']='Camera receiver network unavailable';return result
  with urllib.request.urlopen('http://'+ip+':8081/health.json',timeout=3) as r:st=json.load(r)
  result['source_service_failed']=False;age=st['age'];result['age']=age;result['ip']=ip;result['source_state']=st
  if not st['healthy'] or age>12:
   result['reason']={'camera timestamp absent':'No verified camera frame','camera timestamp frozen':'Camera clock stopped advancing','client overlay or wrong layout':'Camera frame is obstructed','camera starting':'Camera is connecting'}.get(st['reason'],'Camera frames are not advancing');return result
  result['camera']=True
  running,_=inspect_container('v380-grabber-motioneye-1')
  if not running:result['reason']='MotionEye stopped';return result
  code="import urllib.request; a=urllib.request.urlopen('http://127.0.0.1:7999/1/detection/status',timeout=3).read(); b=urllib.request.urlopen('http://127.0.0.1:9081/',timeout=3).read(32); c=urllib.request.urlopen('http://127.0.0.1:7999/1/detection/connection',timeout=3).read(); assert b and b'Connection OK' in c; print('OK' if b'ACTIVE' in a else 'PAUSED')"
  r=subprocess.run(['docker','exec','v380-grabber-motioneye-1','python3','-c',code],capture_output=True,text=True,timeout=9)
  result['motion']=r.returncode==0 and any(v in r.stdout for v in ['OK','PAUSED']);result['recording']=result['motion'] and 'OK' in r.stdout
  result['reason']='All services are healthy' if result['motion'] else 'MotionEye stream unavailable'
 except Exception as e:result['reason']='Video connection unavailable'
 return result
def cached_probe():
 with CACHE_LOCK:d=MONITOR_CACHE.copy()
 return d if time.time()-d.get('checked',0)<8 else probe()
def recover(d,state):
 if (RUNTIME.data/'maintenance').exists():return
 component='v380-grabber-motioneye-1' if d.get('camera') else ('v380-grabber-selector-1' if d.get('source_service_failed') else 'v380-grabber-receiver-1')
 since=state.get('outage_since',time.time());delay=45 if d.get('camera') else 90
 if time.time()-since<delay or time.time()-RECOVERED.get(component,0)<300:return
 RECOVERED[component]=time.time();log('Host watchdog recovering '+component)
 daemon=subprocess.run(['systemctl','is-active','--quiet','docker.service']).returncode==0
 if not daemon:subprocess.run(['systemctl','start','docker.service'],capture_output=True,timeout=45)
 else:subprocess.run(['docker','restart',component],capture_output=True,timeout=45)
REPLICA_BAD={}
REPLICA_GENERATION={}
REPLICA_LAST=0
def recover_replicas(d):
 global REPLICA_LAST
 now=time.time()
 if now-REPLICA_LAST<20 or (RUNTIME.data/'maintenance').exists():return
 REPLICA_LAST=now
 states={x['name']:x for x in d.get('source_state',{}).get('backends',[])}
 for index in range(1,RUNTIME.settings()['receivers']+1):
  key='receiver'+str(index);name='v380-grabber-receiver-'+str(index)
  try:
   running,_=inspect_container(name)
   if states.get(key,{}).get('eligible'):
    REPLICA_BAD.pop(name,None);continue
   generation=states.get(key,{}).get('generation')
   if generation and REPLICA_GENERATION.get(name)!=generation:
    REPLICA_GENERATION[name]=generation;REPLICA_BAD[name]=now
   since=REPLICA_BAD.setdefault(name,now)
   # Allow an authenticated reconnect to finish before restarting its process.
   if running and now-since<90:continue
   if now-RECOVERED.get(name,0)<180:continue
   RECOVERED[name]=now;REPLICA_BAD[name]=now;log('Recovering redundant camera client '+key)
   subprocess.run(['docker','restart' if running else 'start',name],capture_output=True,timeout=30)
  except Exception:pass

DIAG_LAST=0
DIAG_KEY=None
NETWORK_DIAG_CODE=r'''
import json,os,socket,ipaddress,re
from pathlib import Path
out={'processes':[],'sockets':[],'interfaces':{}};owned=set()
for p in Path('/proc').glob('[0-9]*/comm'):
 try:
  if p.read_text().strip() not in ['python3','ffmpeg']:continue
  a=(p.parent/'stat').read_text().rsplit(') ',1)[1].split();out['processes'].append({'pid':int(p.parts[2]),'state':a[0],'cpu_ticks':int(a[11])+int(a[12]),'rss_bytes':int(a[21])*os.sysconf('SC_PAGE_SIZE')})
  for f in (p.parent/'fd').iterdir():
   try:
    m=re.fullmatch(r'socket:\[(\d+)\]',os.readlink(f))
    if m:owned.add(m.group(1))
   except OSError:pass
 except OSError:pass
def address(s):
 h,p=s.split(':');raw=bytes.fromhex(h)
 if len(raw)==4:ip=socket.inet_ntop(socket.AF_INET,raw[::-1])
 else:ip=socket.inet_ntop(socket.AF_INET6,b''.join(raw[i:i+4][::-1] for i in range(0,16,4)))
 return ip,int(p,16)
for name in ['tcp','tcp6','udp','udp6']:
 try:
  for line in Path('/proc/net/'+name).read_text().splitlines()[1:]:
   a=line.split();local,lp=address(a[1]);peer,rp=address(a[2]);inode=a[9]
   if inode not in owned and (not rp or ipaddress.ip_address(peer).is_private or ipaddress.ip_address(peer).is_loopback):continue
   tx,rx=[int(x,16) for x in a[4].split(':')]
   out['sockets'].append({'protocol':name,'local':local,'local_port':lp,'peer':peer,'peer_port':rp,'state':a[3],'tx_queue':tx,'rx_queue':rx,'retransmit_counter':int(a[6],16),'native_owned':inode in owned})
 except OSError:pass
for line in Path('/proc/net/dev').read_text().splitlines()[2:]:
 n,v=line.split(':',1);v=v.split();out['interfaces'][n.strip()]={'rx_bytes':int(v[0]),'tx_bytes':int(v[8])}
print(json.dumps(out))
'''
def connection_diagnostics(d):
 global DIAG_LAST,DIAG_KEY
 key=(d.get('camera'),d.get('motion'),d.get('reason'),str(d.get('source_state',{}).get('generation')))
 now=time.time()
 if key==DIAG_KEY and now-DIAG_LAST<30:return
 DIAG_LAST=now;DIAG_KEY=key
 snapshot={'at':now,'local_time':datetime.datetime.fromtimestamp(now,ZoneInfo(RUNTIME.settings()['timezone'])).isoformat(),'health':d,'load':os.getloadavg()}
 try:
  r=subprocess.run(['docker','exec','v380-grabber-receiver-1','python3','-c',NETWORK_DIAG_CODE],capture_output=True,text=True,timeout=5)
  snapshot['network']=json.loads(r.stdout) if r.returncode==0 else {'probe_error':'container unavailable'}
 except Exception as e:snapshot['network']={'probe_error':type(e).__name__}
 path=STATE/'connection-diagnostics.jsonl'
 try:
  if path.exists() and path.stat().st_size>5*1024**2:
   older=STATE/'connection-diagnostics.jsonl.1';oldest=STATE/'connection-diagnostics.jsonl.2'
   if older.exists():older.replace(oldest)
   path.replace(older)
  with open(path,'a') as f:f.write(json.dumps(snapshot,ensure_ascii=False)+'\n')
  path.chmod(0o600)
 except OSError:pass

def monitor_loop():
 global MONITOR_CACHE
 state=safe_json(STATUS_FILE,{'last_ok':0,'outage_since':0,'announced':False})
 stable=0
 while not STOP.is_set():
  if not RUNTIME.store.cameras or (RUNTIME.data/'maintenance').exists():STOP.wait(1);continue
  try:
   d=probe()
   connection_diagnostics(d)
   with CACHE_LOCK:MONITOR_CACHE=d
   healthy=d.get('camera') and d.get('motion');now=time.time()
   if healthy:
    recover_replicas(d)
    stable+=1
    if stable>=2:
     if state.get('announced') and RUNTIME.settings()['outage_alerts']:
      seconds=round(now-state['outage_since']);enqueue_message('✅ Camera video is available again. Motion recording is ready. Outage: '+str(seconds)+' seconds.')
     state.update(last_ok=now,outage_since=0,announced=False)
   else:
    stable=0
    if not state.get('outage_since'):state['outage_since']=now
    if now-state['outage_since']>=15 and not state.get('announced') and RUNTIME.settings()['outage_alerts']:
     enqueue_message('❌ Camera video was interrupted. '+d['reason']+'. Automatic recovery is running.')
     state['announced']=True
    recover(d,state)
   save(STATUS_FILE,state)
  except Exception as e:log('Monitor retry: '+type(e).__name__)
  STOP.wait(5)
def messages_loop():
 while not STOP.is_set():
  for p in sorted(MESSAGES.glob('*.json')):
   if STOP.is_set():break
   try:
    d=json.loads(p.read_text())
    if time.time()<d.get('next_try',0):continue
    api('sendMessage',{'chat_id':C['chat_id'],'text':d['text']});log('Status notification delivered: '+d['text']);p.unlink(missing_ok=True)
   except ValueError:
    p.rename(p.with_suffix('.corrupt'));log('Quarantined malformed status message')
   except Exception:
    try:d['next_try']=time.time()+30;save(p,d)
    except Exception:pass
  STOP.wait(2)
def ping(chat):
 d=probe();ok=d.get('camera') and d.get('motion')
 pending=sum(1 for p in SPOOL.glob('*.json') if len(p.stem)==32 and not safe_json(p,{'done':True}).get('done'))
 text=('✅ Camera is online; video frames are advancing.\nMotion recording is enabled.' if ok else '❌ Video is unavailable. '+d['reason']+'.\nAutomatic recovery is running.')
 if ok and not d.get('recording'):text=text.replace('Motion recording is enabled.','Motion recording is disabled.')
 if d.get('age') is not None:text+='\nLast camera frame: '+str(round(d['age'],1))+' seconds ago.'
 receivers=d.get('source_state',{}).get('backends',[]);live=sum(bool(x.get('eligible')) for x in receivers)
 text+='\nHealthy receivers: '+str(live)+'/'+str(RUNTIME.settings()['receivers'])+'.'
 text+='\nTelegram clips pending: '+str(pending)+'.'
 if ok:
  try:
   image=urllib.request.urlopen('http://'+d['ip']+':8081/snapshot.jpg',timeout=4).read()
   p=STATE/'ping.jpg';p.write_bytes(image);p.chmod(0o600)
   api('sendPhoto',{'chat_id':chat,'caption':text},photo=p);return
  except Exception:text='❌ Could not fetch a verified camera frame. Automatic recovery is running.'
 api('sendMessage',{'chat_id':chat,'text':text})
