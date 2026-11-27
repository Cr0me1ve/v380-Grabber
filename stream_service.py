#!/usr/bin/env python3
"""Expose decoded camera frames to the existing redundant NVR selector."""
import argparse,collections,http.server,json,logging,os,random,signal,socket,subprocess,threading,time,uuid
from pathlib import Path
from direct_client import Client
LOG=logging.getLogger('v380-source')

def detect_replica_index():
 try:address=socket.gethostbyname(socket.gethostname())
 except OSError:return 1
 for index in range(1,5):
  try:
   if socket.gethostbyname('v380-grabber-receiver-'+str(index))==address:return index
  except OSError:continue
 return 1

class Source:
 def __init__(self,credentials,quality,replica_index=1):
  self.credentials=credentials;self.config=json.loads(Path(credentials).read_text());self.quality=quality;self.lock=threading.Condition();self.boot=uuid.uuid4().hex;self.epoch=0;self.seq=0;self.jpeg=None;self.frame_at=0.;self.camera_at=0.;self.camera_stamp=None;self.camera_ticks=0;self.live_at=0.;self.connected=False;self.reason='starting';self.stop=threading.Event();self.times=collections.deque(maxlen=90);self.attrs={};self.decoder=None;self.client=None;self.decoder_at=0.
  self.replica_index=replica_index;self.attempt_at=time.monotonic()
 def status(self):
  now=time.monotonic();healthy=self.connected and self.jpeg is not None and now-self.frame_at<2 and now-self.camera_at<2
  fps=(len(self.times)-1)/(self.times[-1]-self.times[0]) if len(self.times)>1 else 0
  client=self.client
  return {'healthy':bool(healthy),'fps':round(fps,2),'age':round(now-self.camera_at,2) if self.camera_at else 999999,'generation':self.boot+':'+str(self.epoch),'reason':'ok' if healthy else self.reason if not self.connected else 'decoder or camera frames stalled','timestamp_present':bool(healthy),'clock_ticks':self.camera_ticks,'clock_match_score':0,'window_valid':bool(healthy),'healthy_for':round(now-self.live_at,2) if healthy and self.live_at else 0,'transport':'authenticated V380 Neo TCP','timestamp_source':'camera media header','width':640,'height':360,'camera_attributes':self.attrs,
          'replica_index':self.replica_index,'connection_phase':client.phase if client else 'retry wait',
          'attempt_age':round(now-self.attempt_at,2),'media_peer':client.media_host if client else None}
 def invalidate(self,reason):
  with self.lock:
   if self.connected or self.jpeg is not None:self.epoch+=1
   self.connected=False;self.jpeg=None;self.reason=reason;self.live_at=0.;self.times.clear();self.lock.notify_all()
 def decoder_loop(self,proc,dead):
  buf=b''
  try:
   while not self.stop.is_set():
    chunk=proc.stdout.read1(65536)
    if not chunk:break
    buf+=chunk
    while True:
     a=buf.find(b'\xff\xd8');b=buf.find(b'\xff\xd9',a+2) if a>=0 else -1
     if b<0:break
     jpeg=buf[a:b+2];buf=buf[b+2:];now=time.monotonic()
     with self.lock:
      if self.connected and now-self.camera_at<2:
       self.jpeg=jpeg;self.frame_at=now;self.seq+=1;self.times.append(now)
       if not self.live_at:self.live_at=now
       self.lock.notify_all()
    if len(buf)>2*1024*1024:raise ValueError('Oversized decoded frame')
  except (OSError,ValueError):pass
  finally:
   dead.set();self.invalidate('video decoder stopped')
   try:
    if self.client and self.client.sock:self.client.sock.shutdown(2)
   except OSError:pass
 def run(self):
  backoff=1
  while not self.stop.is_set():
   proc=None;reader=None;client=None;dead=threading.Event();beg=time.monotonic()
   self.attempt_at=beg
   try:
    client=Client(self.config,self.replica_index);self.client=client;client.start(self.quality)
    ff='/usr/bin/ffmpeg'
    if not Path(ff).exists():raise RuntimeError('FFmpeg is not installed at /usr/bin/ffmpeg')
    env=os.environ.copy()
    cmd=[ff,'-hide_banner','-loglevel','error','-threads','1','-fflags','nobuffer','-flags','low_delay','-probesize','32','-analyzeduration','0','-r','15','-f','h264','-i','pipe:0','-an','-vf','fps=15,scale=640:360','-c:v','mjpeg','-q:v','5','-threads','1','-f','image2pipe','pipe:1']
    proc=subprocess.Popen(cmd,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,env=env);self.decoder=proc;self.decoder_at=time.monotonic()
    with self.lock:self.connected=True;self.reason='waiting for first decoded frame';self.camera_at=time.monotonic();self.camera_stamp=None
    reader=threading.Thread(target=self.decoder_loop,args=(proc,dead),daemon=True);reader.start();waiting_i=True
    while not self.stop.is_set() and not dead.is_set():
     kind,data=client.receive()
     if kind=='rpc':
      if data.get('method_id')==197123 and (data.get('result') or {}).get('code')!=1000:raise RuntimeError('Camera rejected video subscription')
      if data.get('method_id')==197124:
       with self.lock:self.attrs=data.get('params') or data.get('data') or {}
      continue
     if kind!='media' or data['codec'] not in [1,2,3]:continue
     if waiting_i and data['codec']!=1:continue
     waiting_i=False
     with self.lock:
      if self.camera_stamp is None or data['timestamp_ms']>self.camera_stamp:self.camera_stamp=data['timestamp_ms'];self.camera_at=time.monotonic();self.camera_ticks+=1
     proc.stdin.write(data['payload']);proc.stdin.flush()
    if not self.stop.is_set():raise RuntimeError('Decoder stream ended')
   except Exception as e:
    if client:client.mark_failure()
    phase=client.phase if client else 'startup'
    LOG.warning('Camera connection interrupted during %s: %s %s',phase,type(e).__name__,str(e)[:120]);self.invalidate('camera connection interrupted')
   finally:
    if client:client.close()
    self.client=None
    if proc:
     try:proc.stdin.close()
     except OSError:pass
     if proc.poll() is None:
      proc.terminate()
      try:proc.wait(timeout=3)
      except subprocess.TimeoutExpired:proc.kill();proc.wait()
    if reader:reader.join(timeout=3)
    self.decoder=None
   if time.monotonic()-beg>30:backoff=1
   self.stop.wait(backoff+random.uniform(0,.6));backoff=min(backoff*2,5)
class Handler(http.server.BaseHTTPRequestHandler):
 def setup(self):
  super().setup();self.connection.settimeout(5)
 def log_message(self,*a):pass
 def do_GET(self):
  source=self.server.source
  if self.path not in ['/health','/health.json','/snapshot.jpg','/video.mjpg']:self.send_error(404);return
  with source.lock:state=source.status();frame=source.jpeg;seq=source.seq
  if self.path=='/health.json':
   raw=json.dumps(state).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(raw);return
  if not state['healthy'] or not frame:self.send_error(503);return
  self.send_response(200);self.send_header('Cache-Control','no-store')
  if self.path in ['/health','/snapshot.jpg']:
   raw=b'OK' if self.path=='/health' else frame;self.send_header('Content-Type','text/plain' if self.path=='/health' else 'image/jpeg');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw);return
  self.send_header('Content-Type','multipart/x-mixed-replace; boundary=frame');self.end_headers();generation=state['generation']
  try:
   while not source.stop.is_set():
    with source.lock:
     source.lock.wait_for(lambda:source.seq!=seq or not source.status()['healthy'],timeout=1);state=source.status()
     if not state['healthy'] or state['generation']!=generation:return
     frame=source.jpeg;seq=source.seq
    self.wfile.write(b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: '+str(len(frame)).encode()+b'\r\n\r\n'+frame+b'\r\n');self.wfile.flush()
  except (BrokenPipeError,ConnectionResetError):pass
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--credentials',required=True);p.add_argument('--quality',type=int,default=1);p.add_argument('--port',type=int,default=8081);p.add_argument('--replica-index',type=int,choices=range(1,5));a=p.parse_args();logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s');source=Source(a.credentials,a.quality,a.replica_index or detect_replica_index())
 LOG.info('Receiver slot %s started',source.replica_index)
 server=http.server.ThreadingHTTPServer(('0.0.0.0',a.port),Handler);server.source=source
 worker=threading.Thread(target=source.run,daemon=True);worker.start()
 def shutdown(signum,frame):
  source.stop.set()
  if source.decoder and source.decoder.poll() is None:source.decoder.terminate()
  os._exit(0)
 signal.signal(signal.SIGTERM,shutdown)
 def supervisor():
  while True:
   if not worker.is_alive():os._exit(1)
   with source.lock:stalled=source.connected and source.decoder and time.monotonic()-source.decoder_at>12 and time.monotonic()-source.frame_at>8
   if stalled and source.decoder.poll() is None:source.decoder.kill()
   time.sleep(2)
 threading.Thread(target=supervisor,daemon=True).start();server.serve_forever()
