"""Recognize the camera OSD, never the Windows client's connection/error screen."""
import hashlib,json,math,subprocess,sys,os
from pathlib import Path
from PIL import Image,ImageChops,ImageFilter
VERSION=5
def normalize_glyph(im):
 return ImageChops.subtract(im,im.filter(ImageFilter.GaussianBlur(2)),scale=1,offset=128)
class ClockGuard:
 def __init__(self,path=None,_data=None):
  d=_data if _data is not None else json.loads(Path(path or os.environ.get('V380_CLOCK_TEMPLATE') or Path(__file__).with_name('clock-template.json')).read_text());self.alternates=[ClockGuard(_data=x) for x in d.get('alternates',[])];self.box=d['box'];self.time_box=d['time_box'];b=d['box'];v=list(normalize_glyph(Image.frombytes('L',(b[2]-b[0],b[3]-b[1]),bytes(d['pixels']))).tobytes());m=sum(v)/len(v);self.pattern=[x-m for x in v];self.norm=sum(x*x for x in self.pattern)
  self.markers=[]
  for marker in d.get('markers',[]):
   box=marker['box'];values=list(normalize_glyph(Image.frombytes('L',(box[2]-box[0],box[3]-box[1]),bytes(marker['pixels']))).tobytes());avg=sum(values)/len(values);pat=[x-avg for x in values];self.markers.append((box,pat,sum(x*x for x in pat)))
 def inspect(self,image):
  im=image.convert('L');best=(-1,0,0)
  for dx,dy in [(0,0),(-1,0),(1,0),(0,-1),(0,1),(-1,-1),(-1,1),(1,-1),(1,1)]:
   b=self.box;a=list(normalize_glyph(im.crop((b[0]+dx,b[1]+dy,b[2]+dx,b[3]+dy))).tobytes());mean=sum(a)/len(a);var=sum((x-mean)**2 for x in a)
   corr=sum(x*(y-mean) for x,y in zip(self.pattern,a))/math.sqrt(self.norm*var) if var else 0
   if corr>best[0]:best=(corr,dx,dy)
   if corr>=.90:break
  if best[0]<0.75:
   found=[]
   for box,pattern,norm in self.markers:
    match=(-1,0,0)
    for dy in [-1,0,1]:
     for dx in [-1,0,1]:
      a=list(normalize_glyph(im.crop((box[0]+dx,box[1]+dy,box[2]+dx,box[3]+dy))).tobytes());avg=sum(a)/len(a);var=sum((x-avg)**2 for x in a)
      score=sum(x*(y-avg) for x,y in zip(pattern,a))/math.sqrt(norm*var) if norm and var else 0
      if score>match[0]:match=(score,dx,dy)
    found.append(match)
   if len(found)<2 or min(x[0] for x in found)<.85:
    for alternate in self.alternates:
     present,sig,score=alternate.inspect(image)
     if present:return present,sig,score
    return False,None,best[0]
   best=(min(x[0] for x in found),found[0][1],found[0][2])
  b=self.time_box;dx,dy=best[1:];sig=im.crop((b[0]+dx,b[1]+dy,b[2]+dx,b[3]+dy)).tobytes()
  return True,sig,best[0]
 @staticmethod
 def changed(a,b):return a is None or sum(abs(x-y) for x,y in zip(a,b))/len(b)>0.5
 @staticmethod
 def unverified_flat_frame(image):
  hist=image.convert('L').crop((16,40,624,300)).resize((152,65)).histogram();mode=max(range(256),key=lambda x:hist[x]);return sum(hist[max(0,mode-3):min(256,mode+4)])/sum(hist)>0.90
 def validate_video(self,path,min_frames=145):
  cmd=['ffmpeg','-v','error','-i',str(path),'-an','-vf','fps=5,scale=640:360,format=gray','-threads','1','-f','rawvideo','pipe:1']
  p=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE);count=0;changes=0;old=None;first_bad=None;size=640*360;missing=0;max_missing=0;clear=0;dropouts=0
  try:
   while True:
    b=p.stdout.read(size)
    if not b:break
    if len(b)!=size:raise ValueError('Incomplete decoded frame')
    im=Image.frombytes('L',(640,360),b);present,sig,score=self.inspect(im);count+=1
    if not present:
     missing+=1;dropouts+=1;max_missing=max(max_missing,missing)
     if self.unverified_flat_frame(im) or missing>20:first_bad=count;break
    else:
     clear+=1;missing=0
     if self.changed(old,sig):changes+=1;old=sig
  finally:
   if first_bad:p.terminate()
   try:rc=p.wait(timeout=10)
   except subprocess.TimeoutExpired:p.kill();p.wait();rc=-1
  result={'version':VERSION,'frames':count,'clock_changes':changes,'first_bad_frame':first_bad,'osd_dropout_frames':dropouts,'max_osd_dropout_frames':max_missing,'valid':first_bad is None and count>=min_frames and clear>=count*2/3 and changes>=min(10,max(2,count//10)) and rc==0}
  if not result['valid']:raise ValueError(json.dumps(result))
  return result
if __name__=='__main__':
 try:print(json.dumps(ClockGuard().validate_video(sys.argv[1],int(sys.argv[2]) if len(sys.argv)>2 else 145)))
 except Exception as e:print(str(e),file=sys.stderr);raise SystemExit(1)
