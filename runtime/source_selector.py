#!/usr/bin/python3
"""Publish a live camera stream from either independent, validated V380 client."""
import hashlib
import http.server, threading, time, json, urllib.request, urllib.parse, io, uuid, os, math
from collections import deque
from pathlib import Path
LOCK=threading.Condition(); FRAME=None; SEQ=0; SELECTED=None; READY=False; LIVE_AT=0.; EPOCH=0; SWITCHES=0; BOOT=uuid.uuid4().hex;LAST_SWITCH=0.
RING=deque();RING_BYTES=0;RING_SECONDS=12;RING_MAX_BYTES=24*1024*1024;PRE_ROLL=3;FPS=15
CONFIG_PATH=Path(os.environ.get('V380_SELECTOR_CONFIG','/config/selector.json'))
CONFIG={'receivers':2,'duration':30,'pre_roll':3};CONFIG_AT=0.
BACKENDS=[{'name':'receiver'+str(i),'index':i,'url':'http://v380-grabber-receiver-'+str(i)+':8081','health':{},'health_at':0.,'frame':None,'frame_at':0.,'clock_at':0.,'signature':None,'ticks':0,'generation':None,'error':'starting'} for i in range(1,5)]
def settings():
 global CONFIG,CONFIG_AT
 if time.monotonic()-CONFIG_AT>1:
  try:
   d=json.loads(CONFIG_PATH.read_text())
   if not 1<=int(d['receivers'])<=4:raise ValueError('Invalid receiver count')
   CONFIG=d
  except (OSError,ValueError,KeyError):pass
  CONFIG_AT=time.monotonic()
 return CONFIG
def enabled(b):return b['index']<=int(settings().get('receivers',2))

def read(url,limit):
 with urllib.request.urlopen(url,timeout=1.5) as r:
  b=r.read(limit+1)
  if len(b)>limit:raise ValueError('Oversized backend response')
  return b

def health_loop(b):
 # Health is diagnostic here: public MJPEG already contains only verified frames.
 while True:
  if not enabled(b):time.sleep(1);continue
  start=time.monotonic()
  try:
   h=json.loads(read(b['url']+'/health.json',65536))
   with LOCK:b.update(health=h,health_at=time.monotonic(),generation=h.get('generation'))
  except Exception:
   pass
  time.sleep(max(0.1,1-(time.monotonic()-start)))

def backend_loop(b):
 backoff=.25
 while True:
  if not enabled(b):
   with LOCK:b.update(streaming=False,frame=None)
   time.sleep(1);continue
  try:
   with urllib.request.urlopen(b['url']+'/video.mjpg',timeout=3) as r:
    buf=b''
    while enabled(b):
     data=r.read1(65536)
     if not data:raise EOFError('Verified source stream ended')
     buf+=data
     while b'\xff\xd9' in buf:
      end=buf.index(b'\xff\xd9')+2;piece=buf[:end];buf=buf[end:];start=piece.find(b'\xff\xd8')
      if start<0:continue
      raw=piece[start:]
      if len(raw)>1024*1024:raise ValueError('Oversized verified frame')
      now=time.monotonic()
      with LOCK:
       b.update(frame=raw,frame_at=now,streaming=True,error=None,ticks=b['ticks']+1)
       LOCK.notify_all()
      backoff=.25
     if len(buf)>2*1024*1024:raise ValueError('Invalid MJPEG framing')
  except Exception as e:
   with LOCK:
    b.update(error=type(e).__name__,streaming=False,frame=None)
    LOCK.notify_all()
  time.sleep(backoff);backoff=min(1,backoff*2)

def eligible(b,now):
 return bool(enabled(b) and b.get('streaming') and b['frame'] and now-b['frame_at']<2)

def select():
 global FRAME,SELECTED,READY,LIVE_AT,EPOCH,SWITCHES,LAST_SWITCH
 now=time.monotonic();old=SELECTED
 choices=[b for b in BACKENDS if eligible(b,now)]
 chosen=next((b for b in choices if b['name']==SELECTED),choices[0] if choices else None)
 # Clock age is a preference for a warmer peer, never a second outage detector.
 if chosen and now-LAST_SWITCH>5 and now-chosen['health_at']<2:
  age=chosen['health'].get('age',0)+now-chosen['health_at']
  fresher=[x for x in choices if x is not chosen and now-x['health_at']<2 and x['health'].get('healthy') and x['health'].get('age',99)+now-x['health_at']<1.2]
  if age>2.5 and fresher:chosen=min(fresher,key=lambda x:x['health'].get('age',99)+now-x['health_at'])
 ok=chosen is not None
 if READY and not ok:EPOCH+=1
 if ok and not READY:LIVE_AT=now
 if chosen:
  SELECTED=chosen['name'];FRAME=chosen['frame']
  if old and old!=SELECTED:
   LAST_SWITCH=now;SWITCHES+=1;print(time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'Switched',old,'to',SELECTED,flush=True)
 else:FRAME=None
 READY=ok
 return chosen,now

def status():
 b,now=select();h=b['health'] if b else {};age=min(6,max(0,h.get('age',0)+now-b['health_at'])) if b and now-b['health_at']<3 else (now-b['frame_at'] if b else 999999)
 return {'healthy':READY,'fps':15,'age':round(age,2),'generation':BOOT+':'+str(EPOCH),'reason':'ok' if READY else 'all camera sources unavailable','timestamp_present':READY,'clock_ticks':h.get('clock_ticks',b['ticks']) if b else 0,'clock_match_score':h.get('clock_match_score',0),'window_valid':READY,'healthy_for':round(now-LIVE_AT,2) if READY else 0,'active_source':SELECTED if READY else None,'switches':SWITCHES,'buffer_seconds':round(RING[-1][2]-RING[0][2],2) if len(RING)>1 else 0,'buffer_frames':len(RING),'buffer_bytes':RING_BYTES,'backends':[{'name':x['name'],'eligible':eligible(x,now),'healthy':eligible(x,now),'source_healthy':bool(x['health'].get('healthy')),'reason':x['health'].get('reason',x['error']),'clock_age':round(x['health'].get('age',0)+now-x['health_at'],2) if x['health_at'] else None,'frame_age':round(now-x['frame_at'],2) if x['frame_at'] else None,'generation':x['generation']} for x in BACKENDS if enabled(x)]}

def publish_loop():
 global SEQ,RING_BYTES
 while True:
  with LOCK:
   select();SEQ+=1;now=time.monotonic();generation=BOOT+':'+str(EPOCH)
   if not READY or (RING and RING[-1][3]!=generation):RING.clear();RING_BYTES=0
   if READY:
    RING.append((SEQ,time.time(),now,generation,FRAME));RING_BYTES+=len(FRAME)
    while RING and (now-RING[0][2]>RING_SECONDS or RING_BYTES>RING_MAX_BYTES):RING_BYTES-=len(RING.popleft()[4])
   LOCK.notify_all()
  time.sleep(1/15)

def event_plan(event_at,expected,duration=None,pre_roll=None):
 duration=int(settings().get("duration",30) if duration is None else duration);pre_roll=float(settings().get("pre_roll",3) if pre_roll is None else pre_roll)
 if not 5<=duration<=120 or not math.isfinite(pre_roll) or not 0<=pre_roll<=10 or pre_roll>=duration:raise ValueError("Invalid clip settings")
 st=status();now=time.time()
 if not st['healthy'] or not RING:raise RuntimeError('No verified buffered video')
 if expected and expected!=st['generation']:raise ValueError('Event generation changed')
 if not math.isfinite(event_at) or event_at>now+1 or now-event_at>8:raise ValueError('Event is outside the capture window')
 target=event_at-pre_roll
 if RING[0][3]!=st['generation']:raise RuntimeError('Buffer is warming up')
 if target<RING[0][1]-.1 and RING[0][2]-LIVE_AT>.5:raise ValueError('Required event history expired')
 first=next((f for f in RING if f[1]>=target),None)
 if first is None:raise RuntimeError('Event history not ready')
 return {'generation':st['generation'],'event_at':event_at,'recording_start':first[1],'pre_roll':max(0,round(event_at-first[1],3)),'first_seq':first[0],'first_sha256':hashlib.sha256(first[4]).hexdigest(),'fps':FPS,'duration':duration}

class Handler(http.server.BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def event_response(self,parsed):
  try:
   q=urllib.parse.parse_qs(parsed.query);at=float(q['at'][0]);expected=q.get('generation',[''])[0]
   with LOCK:plan=event_plan(at,expected,int(q.get('duration',[settings().get('duration',30)])[0]),float(q.get('pre_roll',[settings().get('pre_roll',3)])[0]))
  except (KeyError,TypeError,ValueError):self.send_error(410,'Event history unavailable');return
  except RuntimeError:self.send_error(503,'Verified buffer not ready');return
  if parsed.path=='/event-info.json':
   raw=json.dumps(plan).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw);return
  self.send_response(200);self.send_header('Cache-Control','no-store');self.send_header('Content-Type','multipart/x-mixed-replace; boundary=frame');self.send_header('X-V380-Recording-Start',str(plan['recording_start']));self.send_header('X-V380-Pre-Roll',str(plan['pre_roll']));self.end_headers()
  cursor=plan['first_seq'];beg=time.monotonic()
  try:
   for number in range(FPS*plan['duration']+1):
    with LOCK:
     while True:
      st=status()
      if not st['healthy'] or st['generation']!=plan['generation'] or not RING:return
      index=cursor-RING[0][0]
      if index<0:return
      if index<len(RING):
       item=RING[index]
       if item[0]!=cursor or item[3]!=plan['generation']:return
       frame=item[4];break
      if time.monotonic()-beg>plan['duration']+15:return
      LOCK.wait(timeout=.1)
    delay=beg+number/FPS-time.monotonic()
    if delay>0:time.sleep(delay)
    self.wfile.write(b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: '+str(len(frame)).encode()+b'\r\n\r\n'+frame+b'\r\n');self.wfile.flush();cursor+=1
  except (BrokenPipeError,ConnectionResetError):pass

 def do_GET(self):
  parsed=urllib.parse.urlsplit(self.path)
  if parsed.path in ['/event.mjpg','/event-info.json']:self.event_response(parsed);return
  if self.path not in ['/health.json','/health','/snapshot.jpg','/video.mjpg']:self.send_error(404);return
  with LOCK:st=status();frame=FRAME;seq=SEQ
  if self.path=='/health.json':
   raw=json.dumps(st).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw);return
  if not st['healthy'] or not frame:self.send_error(503);return
  self.send_response(200);self.send_header('Cache-Control','no-store')
  if self.path=='/health':
   raw=('OK '+st['active_source']).encode();self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw);return
  if self.path=='/snapshot.jpg':
   self.send_header('Content-Type','image/jpeg');self.send_header('Content-Length',str(len(frame)));self.end_headers();self.wfile.write(frame);return
  self.send_header('Content-Type','multipart/x-mixed-replace; boundary=frame');self.end_headers();generation=st['generation']
  try:
   while True:
    with LOCK:
     LOCK.wait_for(lambda:SEQ!=seq,timeout=1);st=status()
     if not st['healthy'] or st['generation']!=generation:return
     frame=FRAME;seq=SEQ
    self.wfile.write(b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: '+str(len(frame)).encode()+b'\r\n\r\n'+frame+b'\r\n');self.wfile.flush()
  except (BrokenPipeError,ConnectionResetError):pass

if __name__=='__main__':
 threads=[threading.Thread(target=fn,args=(b,),daemon=True) for b in BACKENDS for fn in [backend_loop,health_loop]]+[threading.Thread(target=publish_loop,daemon=True)]
 for t in threads:t.start()
 def watchdog():
  while True:
   if not all(t.is_alive() for t in threads):os._exit(1)
   time.sleep(5)
 threading.Thread(target=watchdog,daemon=True).start()
 http.server.ThreadingHTTPServer(('0.0.0.0',int(os.environ.get('V380_RELAY_PORT','8081'))),Handler).serve_forever()
