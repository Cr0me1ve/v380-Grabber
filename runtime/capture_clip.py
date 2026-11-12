#!/usr/bin/python3
"""Capture only one uninterrupted, validated camera generation."""
import fcntl,json,os,re,subprocess,sys,time,urllib.request,urllib.parse
from pathlib import Path
from clock_guard import ClockGuard,VERSION
BASE=os.environ.get('V380_SOURCE_URL','http://v380-source:8081')
duration=int(sys.argv[4]) if len(sys.argv)>4 else 30;pre_roll=float(sys.argv[5]) if len(sys.argv)>5 else 3
if not 5<=duration<=120 or not 0<=pre_roll<=10:raise SystemExit(2)
u=sys.argv[1];expected=(sys.argv[2] or None) if len(sys.argv)>2 else None;event_at=float(sys.argv[3]) if len(sys.argv)>3 else time.time()
if not re.fullmatch('[0-9a-f]{32}',u):raise SystemExit(2)
p=Path('/var/lib/motioneye/.telegram-spool');final=p/(u+'.mp4');done=p/(u+'.complete')
def health():return json.load(urllib.request.urlopen(BASE+'/health.json',timeout=2))
with open(p/(u+'.lock'),'a') as lock:
 fcntl.flock(lock,fcntl.LOCK_EX)
 if done.exists() and final.exists():
  ClockGuard().validate_video(final,max(3,duration*5-2));raise SystemExit(0)
 start=health()
 if not start['healthy'] or (expected and start['generation']!=expected):raise RuntimeError('Camera unavailable or event generation changed')
 query=urllib.parse.urlencode({'at':event_at,'generation':start['generation'],'duration':duration,'pre_roll':pre_roll})
 info=json.load(urllib.request.urlopen(BASE+'/event-info.json?'+query,timeout=3))
 part=p/(u+'.part.mp4')
 cmd=['ffmpeg','-hide_banner','-loglevel','error','-y','-rw_timeout','3000000','-analyzeduration','500000','-probesize','32768','-f','mjpeg','-framerate','15','-i',BASE+'/event.mjpg?'+query,'-t',str(duration),'-an','-vf','fps=15,format=yuv420p','-c:v','libx264','-preset','ultrafast','-crf','25','-threads','1','-movflags','+faststart',str(part)]
 with open(p/(u+'.ffmpeg.log'),'wb') as log:
  proc=subprocess.Popen(cmd,stdout=subprocess.DEVNULL,stderr=log)
  try:
   deadline=time.monotonic()+duration+20
   while proc.poll() is None:
    st=health()
    if not st['healthy'] or st['generation']!=start['generation']:raise RuntimeError('Video continuity lost')
    if time.monotonic()>deadline:raise TimeoutError('Capture timeout')
    time.sleep(0.5)
   if proc.returncode:raise RuntimeError('Capture failed')
   st=health()
   if not st['healthy'] or st['generation']!=start['generation']:raise RuntimeError('Video continuity lost')
  finally:
   if proc.poll() is None:
    proc.terminate()
    try:proc.wait(timeout=3)
    except subprocess.TimeoutExpired:proc.kill();proc.wait()
 probe=subprocess.run(['ffprobe','-v','error','-show_entries','format=duration','-of','default=nw=1:nk=1',str(part)],capture_output=True,text=True,check=True,timeout=10)
 actual=float(probe.stdout.strip())
 if not duration-.2<=actual<=duration+.5:raise RuntimeError('Incomplete capture')
 checked=ClockGuard().validate_video(part,max(3,duration*5-2))
 with open(part,'rb') as f:os.fsync(f.fileno())
 part.replace(final)
 with open(done,'w') as f:json.dump({'duration':duration,'generation':start['generation'],'validator':checked,'event_at':event_at,'recording_start':info['recording_start'],'pre_roll':info['pre_roll'],'first_frame_sha256':info['first_sha256']},f);f.flush();os.fsync(f.fileno())
 fd=os.open(p,os.O_RDONLY|os.O_DIRECTORY)
 try:os.fsync(fd)
 finally:os.close(fd)
