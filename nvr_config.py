"""Private runtime configuration for the single canonical NVR deployment."""
from __future__ import annotations
import json,os,re,secrets
from pathlib import Path
ROOT=Path(__file__).resolve().parent
DATA=Path(os.environ.get('DATA_DIR',str(ROOT/'data'))).resolve()
PROJECT='v380-grabber'
PRIMARY=PROJECT+'-receiver-1'
SELECTOR=PROJECT+'-selector-1'
MOTIONEYE=PROJECT+'-motioneye-1'
DEFAULTS={'duration':30,'receivers':2,'pre_roll':3,'retention_days':7,'storage_gb':3,'reserve_gb':2,
          'notifications':True,'outage_alerts':True,'archive_subdir':'camera','timezone':'UTC',
          'camera_name':'Camera','web_url':'http://127.0.0.1:8765','sensitivity':'medium','enabled':True}

def atomic(path,data,mode=0o600):
 path=Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
 tmp=path.parent/('.'+path.name+'.'+secrets.token_hex(6)+'.tmp')
 with os.fdopen(os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,mode),'wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
 os.replace(tmp,path)
 fd=os.open(path.parent,os.O_RDONLY)
 try:os.fsync(fd)
 finally:os.close(fd)

def load_settings():
 try:raw=json.loads((DATA/'runtime.json').read_text())
 except (OSError,ValueError):raw={}
 return dict(DEFAULTS,**raw)

def save_settings(values):
 settings=dict(load_settings(),**values)
 atomic(DATA/'runtime.json',json.dumps(settings).encode())
 return settings

def initialize():
 DATA.mkdir(parents=True,exist_ok=True,mode=0o700)
 settings=load_settings()
 if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',settings['archive_subdir']):raise ValueError('Invalid archive directory')
 for folder in ['motioneye','recordings','logs','telegram-state','cameras']:
  p=DATA/folder;p.mkdir(parents=True,exist_ok=True,mode=0o700)
 for folder in ['motioneye','recordings','logs']:
  if os.geteuid()==0:os.chown(DATA/folder,1000,1000)
 spool=DATA/'recordings'/'.telegram-spool';spool.mkdir(exist_ok=True,mode=0o700)
 if os.geteuid()==0:os.chown(spool,1000,1000)
 template=DATA/'motioneye/clock-template.json'
 if not template.exists():atomic(template,(ROOT/'assets/clock.template').read_bytes())
 if os.geteuid()==0:os.chown(template,1000,1000)
 # These values exist only in ignored private state, never in the repository/image.
 if not (DATA/'motioneye/motion.conf').exists():
  from argon2 import PasswordHasher
  password=secrets.token_urlsafe(20);normal=secrets.token_urlsafe(20)
  values=dict(settings,admin_password=PasswordHasher().hash(password),normal_password=PasswordHasher().hash(normal),relay_secret=secrets.token_hex(24),client_secret=secrets.token_hex(24))
  for name,source in [('motioneye.conf','motioneye.conf.template'),('motion.conf','motion.conf.template'),('camera-1.conf','camera.conf.template')]:
   text=(ROOT/'assets'/source).read_text()
   for key,value in values.items():text=text.replace('{{'+key+'}}',str(value))
   target=DATA/'motioneye'/name;atomic(target,text.encode())
   if os.geteuid()==0:os.chown(target,1000,1000)
  atomic(DATA/'web.json',json.dumps({'username':'admin','password':password}).encode())
 # Upgrade the previous on-disk event hook without changing web credentials or media.
 camera=DATA/'motioneye/camera-1.conf'
 if camera.exists():
  before=camera.read_text();after=before.replace('/etc/motioneye/queue-telegram.py','/helpers/queue_motion.py')
  if after!=before:
   atomic(camera,after.encode())
   if os.geteuid()==0:os.chown(camera,1000,1000)
 save_settings(settings)
 write_runtime_settings(settings)
 return settings

def write_runtime_settings(settings):
 spool=DATA/'recordings/.telegram-spool'
 atomic(spool/'settings.json',json.dumps({k:settings[k] for k in ['duration','pre_roll']}).encode())
 # The event hook runs as UID 1000 inside MotionEye.
 if os.geteuid()==0:os.chown(spool/'settings.json',1000,1000)
 if settings['enabled'] and settings['notifications']:
  (spool/'enabled').touch(mode=0o600,exist_ok=True)
  if os.geteuid()==0:os.chown(spool/'enabled',1000,1000)
 else:(spool/'enabled').unlink(missing_ok=True)
 control=DATA/'control';control.mkdir(exist_ok=True,mode=0o755)
 if os.geteuid()==0:os.chown(control,1000,1000)
 atomic(control/'selector.json',json.dumps({k:settings[k] for k in ['receivers','duration','pre_roll']}).encode(),0o644)

def validate_setting(name,value):
 if name=='sensitivity':
  if value not in ['low','medium','high']:raise ValueError('Use low, medium, or high.')
  return value
 if name in ['notifications','outage_alerts','enabled']:
  if isinstance(value,bool):return value
  if value.lower() not in ['on','off']:raise ValueError('Use on or off.')
  return value.lower()=='on'
 limits={'retention_days':(1,30),'duration':(5,120),'receivers':(1,4),'pre_roll':(0,10),'storage_gb':(1,1000),'reserve_gb':(.5,100)}
 if name not in limits:raise ValueError('Unknown camera setting')
 try:
  number=float(value) if name in ['storage_gb','reserve_gb'] else int(value)
 except ValueError:raise ValueError('Enter a number.') from None
 low,high=limits[name]
 if not low<=number<=high:raise ValueError('Allowed range: '+str(low)+' to '+str(high)+'.')
 return number
