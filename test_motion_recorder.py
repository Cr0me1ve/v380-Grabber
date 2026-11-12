"""Verify pre-roll, empty-frame rejection and acknowledged clip persistence."""
import io,json,subprocess,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image,ImageDraw
from motion_recorder import MotionRecorder,_scene,motion_fraction

def frame(color=80,box=False):
 image=Image.new('RGB',(640,360),(color,)*3)
 if box:ImageDraw.Draw(image).rectangle((100,100,180,220),fill='white')
 out=io.BytesIO();image.save(out,'JPEG');return out.getvalue()

class RecorderTests(unittest.TestCase):
 def test_black_and_white_are_excluded_but_motion_is_detected(self):
  self.assertFalse(_scene(frame(0))[1]);self.assertFalse(_scene(frame(255))[1])
  old,valid=_scene(frame());new,_=_scene(frame(box=True))
  self.assertTrue(valid);self.assertGreater(motion_fraction(old,new),.012)
 def test_30_second_clip_has_preroll_and_survives_retry_until_ack(self):
  with tempfile.TemporaryDirectory() as temp:
   settings=lambda:{'enabled':True,'retention_days':7}
   recorder=MotionRecorder('http://127.0.0.1:1/video.mjpg',temp,1,settings)
   now=time.time();jpeg=frame(box=True);frames=[(i/15,jpeg,now+i/15) for i in range(451)]
   with patch.object(recorder,'_space_available',return_value=True):recorder._save(frames,now+3)
   pending=recorder.pending();self.assertEqual(len(pending),1)
   item=pending[0];self.assertAlmostEqual(item['pre_roll_seconds'],3,places=2)
   result=json.loads(subprocess.check_output(['ffprobe','-v','error','-count_frames','-show_entries','format=duration:stream=nb_read_frames,r_frame_rate','-of','json',item['path']]))
   self.assertAlmostEqual(float(result['format']['duration']),30,places=2)
   self.assertEqual(int(result['streams'][0]['nb_read_frames']),450)
   reloaded=MotionRecorder('http://127.0.0.1:1/video.mjpg',temp,1,settings)
   self.assertEqual(reloaded.pending()[0]['id'],item['id'])
   reloaded.ack(item['id']);self.assertEqual(reloaded.pending(),[])
   self.assertTrue(Path(item['path']).exists())
if __name__=='__main__':unittest.main()
