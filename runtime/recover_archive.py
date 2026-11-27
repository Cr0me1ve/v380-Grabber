#!/usr/bin/python3
"""Recover a completed Motion event under the same durable per-job lock."""
import fcntl,json,math,os,re,subprocess,sys
from pathlib import Path
from clock_guard import ClockGuard
from scene_guard import validate_video,EmptyScene
from queue_movie import MIN_CLIP_SECONDS
u=sys.argv[1];src=Path(sys.argv[2]);root=Path('/var/lib/motioneye')/os.environ.get('V380_ARCHIVE_SUBDIR','camera')
seconds=int(sys.argv[3]) if len(sys.argv)>3 else 30
offset=float(sys.argv[4]) if len(sys.argv)>4 else 0
if not 5<=seconds<=120 or not math.isfinite(offset) or offset<0:raise SystemExit(2)
if not re.fullmatch('[0-9a-f]{32}',u) or not src.resolve().is_relative_to(root):raise SystemExit(2)
spool=Path('/var/lib/motioneye/.telegram-spool');out=spool/(u+'.mp4');done=spool/(u+'.complete')
with open(spool/(u+'.lock'),'a') as lock:
 fcntl.flock(lock,fcntl.LOCK_EX)
 if done.exists() and out.exists():print(done.read_text());raise SystemExit(0)
 part=spool/(u+'.archive.part.mp4')
 subprocess.run(['ffmpeg','-v','error','-y','-ss',str(offset),'-i',str(src),'-t',str(seconds),'-an','-vf','fps=15,format=yuv420p','-c:v','libx264','-preset','ultrafast','-crf','25','-threads','1','-movflags','+faststart',str(part)],check=True,timeout=max(25,seconds))
 duration=float(subprocess.check_output(['ffprobe','-v','error','-show_entries','format=duration','-of','default=nw=1:nk=1',str(part)],text=True))
 if not MIN_CLIP_SECONDS-.1<=duration<=seconds+.1:raise SystemExit(4)
 minimum=max(3,int(duration*5)-2)
 try:
  checked=ClockGuard().validate_video(part,minimum);scene=validate_video(part,minimum)
 except EmptyScene:raise SystemExit(3)
 except ValueError:raise SystemExit(4)
 with open(part,'rb') as f:os.fsync(f.fileno())
 part.replace(out);report={'duration':duration,'recovered_from_archive':str(src.relative_to(root)),'validator':checked,'scene':scene}
 temp=done.with_suffix('.complete.tmp')
 with open(temp,'w') as f:json.dump(report,f);f.flush();os.fsync(f.fileno())
 temp.replace(done);fd=os.open(spool,os.O_RDONLY|os.O_DIRECTORY)
 try:os.fsync(fd)
 finally:os.close(fd)
 print(json.dumps(report))
