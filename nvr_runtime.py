"""One canonical Compose NVR, controlled and supervised by the Telegram bot."""
from __future__ import annotations
import json,os,subprocess,threading,time,urllib.request,urllib.parse
from pathlib import Path
import nvr_config as cfg

class NvrRuntime:
 def __init__(self,api,store):
  self.api=api;self.store=store;self.data=store.data_dir.resolve();self.stop_event=threading.Event();self.lock=threading.RLock();self.threads=[];self.last_ensure=0;self.device_id=(store.load_camera_config(1) or {}).get("device_id")
  if self.data!=cfg.DATA:raise RuntimeError('DATA_DIR must match the deployment data directory')
  cfg.initialize()
  import nvr_worker
  self.worker=nvr_worker;nvr_worker.configure(self)
  self.manager=ReceiverManager(self);self.recordings=RecordingManager(self)
 def settings(self):return cfg.load_settings()
 def compose(self,*args,timeout=120):
  env=os.environ.copy();env['V380_DATA_DIR']=str(self.data);env['COMPOSE_PROJECT_NAME']=cfg.PROJECT
  return subprocess.run(['docker','compose','-f',str(cfg.ROOT/'compose.yaml'),*args],cwd=cfg.ROOT,env=env,capture_output=True,text=True,timeout=timeout,check=True)
 def container(self,name,*command,timeout=10):
  return subprocess.run(['docker','exec',name,*command],capture_output=True,text=True,timeout=timeout,check=True)
 def selector(self):
  running,ip=self.worker.inspect_container(cfg.SELECTOR)
  if not running or not ip:raise RuntimeError('Selector unavailable')
  return json.load(urllib.request.urlopen('http://'+ip+':8081/health.json',timeout=3))
 def control(self,path):
  code='import urllib.request,sys;print(urllib.request.urlopen("http://127.0.0.1:7999/1/"+sys.argv[1],timeout=3).read().decode())'
  return self.container(cfg.MOTIONEYE,'python3','-c',code,path).stdout
 def apply_settings(self):
  settings=self.settings();cfg.write_runtime_settings(settings)
  path=self.data/'motioneye/camera-1.conf'
  updates={'pre_capture':str(int(settings['pre_roll'])*15),'threshold':str({'low':9216,'medium':4608,'high':2304}[settings['sensitivity']]),'# @motion_detection':'true' if settings['enabled'] else 'false','# @preserve_movies':str(settings['retention_days'])}
  text=path.read_text();lines=text.splitlines()
  for key,value in updates.items():
   lines=[line for line in lines if not (line==key or line.startswith(key+' '))];lines.append(key+' '+value)
  cfg.atomic(path,('\n'.join(lines)+'\n').encode())
  if os.geteuid()==0:os.chown(path,1000,1000)
  try:
   self.control('config/set?'+urllib.parse.urlencode({'threshold':updates['threshold']}))
   self.control('config/set?'+urllib.parse.urlencode({'pre_capture':updates['pre_capture']}))
   self.control('detection/start' if settings['enabled'] else 'detection/pause')
  except Exception:pass  # A starting MotionEye reads the persisted configuration.
 def set_setting(self,name,value):
  with self.lock:
   value=cfg.validate_setting(name,value if isinstance(value,bool) else str(value))
   proposed=dict(self.settings(),**{name:value})
   if proposed['pre_roll']>=proposed['duration']:raise ValueError('Pre-roll must be shorter than the video duration.')
   cfg.save_settings({name:value});self.apply_settings()
   if name=='receivers' and self.store.cameras:
    self.compose('up','-d','--no-recreate','--scale','receiver='+str(value),'receiver')
  return True
 def start(self):
  funcs=[lambda:self.worker.work('capture'),lambda:self.worker.work('send'),self.worker.monitor_loop,self.worker.messages_loop,self.housekeeping]
  self.threads=[threading.Thread(target=f,daemon=True) for f in funcs]
  for thread in self.threads:thread.start()
 def housekeeping(self):
  from prune_media import prune
  due=0
  while not self.stop_event.wait(5):
   self.worker.clean()
   if time.monotonic()>due and not (self.data/'maintenance').exists():
    try:prune(self.data/'recordings',self.settings())
    except Exception as error:self.worker.log('Retention retry: '+type(error).__name__)
    due=time.monotonic()+300
   if not all(t.is_alive() for t in self.threads if t is not threading.current_thread()):os._exit(1)
 def shutdown(self):
  self.stop_event.set()
  for thread in self.threads:thread.join(timeout=2)
 def web(self,chat):
  settings=self.settings();text='Web viewer: '+settings['web_url']+'\n'
  try:
   secret=json.loads((self.data/'web.json').read_text());text+='Username: '+secret['username']+'\nPassword: '+secret['password']
  except (OSError,ValueError):text+='Your existing web login was preserved.'
  self.api.send_message(chat,text)
 def web_password(self,password):
  if not 12<=len(password)<=128:raise ValueError('Use a web password containing 12 to 128 characters')
  from argon2 import PasswordHasher
  path=self.data/'motioneye/motion.conf';lines=path.read_text().splitlines();lines=[x for x in lines if not x.startswith('# @admin_password ')]
  lines.append('# @admin_password '+PasswordHasher().hash(password));cfg.atomic(path,('\n'.join(lines)+'\n').encode())
  if os.geteuid()==0:os.chown(path,1000,1000)
  cfg.atomic(self.data/'web.json',json.dumps({'username':'admin','password':password}).encode())
  subprocess.run(['docker','restart',cfg.MOTIONEYE],check=True,capture_output=True,timeout=45)

class ReceiverManager:
 def __init__(self,runtime):self.runtime=runtime
 def start(self,slot):
  with self.runtime.lock:return self._start(slot)
 def _start(self,slot):
  if slot!=1:raise ValueError('This NVR supports one camera with up to four parallel receivers')
  runtime=self.runtime;cfg.initialize();path=runtime.store.camera_config_path(slot)
  if os.geteuid()==0:os.chown(path,1000,1000)
  for name in ['v380-camera',os.environ.get('PROXY_NETWORK','v380-proxy')]:
   result=subprocess.run(['docker','network','inspect',name],capture_output=True)
   if result.returncode:subprocess.run(['docker','network','create',name],check=True,capture_output=True)
  runtime.apply_settings();runtime.compose('up','-d','--scale','receiver='+str(runtime.settings()['receivers']))
  return True
 def stop(self,slot):
  with self.runtime.lock:self.runtime.compose('stop',timeout=90)
 def restart(self,slot):
  with self.runtime.lock:return self._restart(slot)
 def _restart(self,slot):
  if slot!=1:return False
  new_device=(self.runtime.store.load_camera_config(1) or {}).get('device_id')
  if new_device!=self.runtime.device_id:
   self.runtime.compose('stop','receiver',timeout=90)
   self.runtime.device_id=new_device
   return self._start(1)
  if os.geteuid()==0:os.chown(self.runtime.store.camera_config_path(slot),1000,1000)
  for index in range(1,self.runtime.settings()['receivers']+1):
   name=cfg.PROJECT+'-receiver-'+str(index)
   subprocess.run(['docker','restart',name],capture_output=True,check=True,timeout=30)
   # Wait for the selector to consume the replacement before touching a warm peer.
   deadline=time.monotonic()+90
   while time.monotonic()<deadline:
    try:
     state=self.runtime.selector()
     if any(x['name']=='receiver'+str(index) and x['eligible'] for x in state.get('backends',[])):break
    except Exception:pass
    time.sleep(.5)
  return True
 def health(self,slot):
  try:
   state=self.runtime.selector();return dict(state,process='running')
  except Exception:return {'healthy':False,'process':'unavailable'}
 def ping(self,chat):self.runtime.worker.ping(chat)
 def start_all(self):
  if self.runtime.store.cameras:self.start(1)
 def ensure_all(self,stop_event):
  while not stop_event.wait(20):
   if not self.runtime.store.cameras or (self.runtime.data/'maintenance').exists():continue
   try:
    if subprocess.run(['systemctl','is-active','--quiet','docker.service']).returncode:
     subprocess.run(['systemctl','start','docker.service'],capture_output=True,timeout=45)
    names=[cfg.SELECTOR,cfg.MOTIONEYE]+[cfg.PROJECT+'-receiver-'+str(i) for i in range(1,self.runtime.settings()['receivers']+1)]
    failed=False
    for name in names:
     try:failed=failed or not self.runtime.worker.inspect_container(name)[0]
     except Exception:failed=True
    if failed:self.start(1)
   except Exception as error:self.runtime.worker.log('Stack recovery retry: '+type(error).__name__)
 def shutdown(self):pass  # Docker keeps camera recording alive across a bot restart.

class RecordingManager:
 def __init__(self,runtime):self.runtime=runtime
 def settings(self,slot):return self.runtime.settings()
 def ensure_slot(self,slot):self.runtime.apply_settings()
 def start_all(self):self.runtime.apply_settings()
 def stop_slot(self,slot):self.runtime.set_setting('enabled',False)
 def stop_all(self):pass
 def set_setting(self,slot,name,value):return self.runtime.set_setting(name,value)
 def status(self,slot):
  state=self.runtime.worker.cached_probe();pending=sum(1 for p in self.runtime.worker.SPOOL.glob('*.json') if len(p.stem)==32 and not self.runtime.worker.safe_json(p,{'done':True}).get('done'))
  return {'enabled':self.runtime.settings()['enabled'],'recording':state.get('recording',False),'source_healthy':state.get('camera',False),'queued':pending}
