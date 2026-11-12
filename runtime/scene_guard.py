"""Reject empty black camera scenes; ignore timestamps and the client border."""
import json,subprocess,sys
from PIL import Image
VERSION=1
BOX=(16,40,624,300)
SIZE=(152,65)
class EmptyScene(ValueError):pass

def stats(image):
 im=image.convert('L').crop(BOX).resize(SIZE)
 return small_stats(im)
def small_stats(im):
 hist=im.histogram();n=sum(hist);mean=sum(i*v for i,v in enumerate(hist))/n;bright=sum(hist[16:])/n
 return {'mean':round(mean,4),'bright_fraction':round(bright,6),'black':mean<4 and bright<0.005}
def validate_video(path,min_frames=145):
 cmd=['ffmpeg','-v','error','-i',str(path),'-an','-vf','fps=5,crop=608:260:16:40,scale=152:65,format=gray','-threads','1','-f','rawvideo','pipe:1']
 p=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL);count=visible=0;max_mean=max_bright=0.;length=SIZE[0]*SIZE[1]
 try:
  while True:
   raw=p.stdout.read(length)
   if not raw:break
   if len(raw)!=length:raise ValueError('Incomplete decoded scene frame')
   s=small_stats(Image.frombytes('L',SIZE,raw));count+=1;visible+=not s['black'];max_mean=max(max_mean,s['mean']);max_bright=max(max_bright,s['bright_fraction'])
 finally:
  if p.poll() is None:
   try:p.wait(timeout=10)
   except subprocess.TimeoutExpired:p.kill();p.wait()
 result={'version':VERSION,'frames':count,'visible_frames':visible,'max_mean':max_mean,'max_bright_fraction':max_bright,'valid':count>=min_frames and visible>=2}
 if p.returncode or count<min_frames:raise ValueError('Invalid decoded footage')
 if not result['valid']:raise EmptyScene(json.dumps(result))
 return result
if __name__=='__main__':
 try:print(json.dumps(validate_video(sys.argv[1],int(sys.argv[2]) if len(sys.argv)>2 else 145)))
 except EmptyScene as e:print(str(e));raise SystemExit(3)
 except Exception as e:print(type(e).__name__,file=sys.stderr);raise SystemExit(1)
