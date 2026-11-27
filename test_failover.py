"""Offline checks for endpoint fallback and cache preservation."""
import base64,json,unittest
from unittest.mock import patch
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat
from direct_client import Client,CloudAPIError,CloudHTTPError,CameraAPIError

class FailoverTests(unittest.TestCase):
 def test_controller_falls_back_from_offline_and_remembers_success(self):
  peer=X25519PrivateKey.generate().public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)
  c=Client({'device_public_key':base64.b64encode(peer).decode(),'token':'0'*64,'device_id':1,'secret_ip':'192.0.2.1','secret_ips':['192.0.2.2'],'secret_host':'example.invalid','secret_path':'/exch'})
  attempts=[]
  def post(host,*args):
   attempts.append(host)
   if host=='192.0.2.1':raise CloudAPIError(1012)
   return {'code':1000,'tr':{'result':{'code':1000},'data':{'secure':2,'sid':1}}}
  c.post=post
  with patch('direct_client.socket.getaddrinfo',return_value=[]):c.authorize()
  self.assertEqual(attempts,['192.0.2.1','192.0.2.2']);self.assertEqual(c.c['secret_ip'],'192.0.2.2');self.assertEqual(len(c.key),32)
 def test_dispatch_failure_uses_alternate_and_preserves_cached_region(self):
  cached={'ip':'192.0.2.10','tp':{'call_port':80}};fresh={'ip':'192.0.2.20','tp':{'call_port':80}}
  c=Client({'device_id':1,'relays':[cached],'dispatch_host':'preferred.invalid'})
  hosts=[]
  class Connection:
   def __init__(self,host,*args,**kwargs):self.host=host;hosts.append(host)
   def request(self,*args):
    if self.host=='preferred.invalid':raise OSError('unavailable')
   def getresponse(self):return self
   def read(self,*args):return json.dumps({'code':2000,'data':[fresh]}).encode()
   def close(self):pass
  with patch('direct_client.http.client.HTTPConnection',Connection):c.refresh_relays()
  self.assertEqual(hosts,['preferred.invalid','dispatch.av380.net']);self.assertEqual(c.c['relays'],[cached,fresh])
 def test_relay_failure_tries_backup_and_cools_failed_address(self):
  first={'ip':'192.0.2.1'};second={'ip':'192.0.2.2'};c=Client({'relays':[first,second]});attempts=[]
  c.authorize=lambda:None;c.refresh_relays=lambda:None;c.rpc=lambda *a:None
  def connect(relay):
   attempts.append(relay)
   if relay==first:raise OSError('connection reset')
  c.connect=connect;c.start()
  self.assertEqual(attempts,[first,second])
  self.assertEqual(c.relay_candidates(),[second,first])
 def test_replicas_prefer_different_relays_from_the_same_pool(self):
  relays=[{'ip':f'192.0.2.{i}'} for i in range(1,4)]
  config={'relays':relays}
  self.assertEqual([Client(config,i).relay_candidates()[0] for i in range(1,5)],
                   [*relays,relays[0]])
 def test_http_redirect_does_not_prevent_controller_fallback(self):
  peer=X25519PrivateKey.generate().public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)
  c=Client({'device_public_key':base64.b64encode(peer).decode(),'token':'0'*64,'device_id':1,'secret_ip':'192.0.2.1','secret_ips':['192.0.2.2'],'secret_host':'example.invalid','secret_path':'/exch'})
  def post(host,*args):
   if host=='192.0.2.1':raise CloudHTTPError('Cloud HTTP status 301')
   return {'code':1000,'tr':{'result':{'code':1000},'data':{'secure':2,'sid':1}}}
  c.post=post
  with patch('direct_client.socket.getaddrinfo',return_value=[]):c.authorize()
  self.assertEqual(c.c['secret_ip'],'192.0.2.2')
 def test_broken_transport_prefers_backup_on_next_connection(self):
  first={'ip':'192.0.2.1'};second={'ip':'192.0.2.2'};config={'relays':[first,second]}
  client=Client(config);client.active_relay=first;client.mark_failure()
  replacement=Client(config)
  self.assertEqual(replacement.relay_candidates(),[second,first])
  with patch('direct_client.time.monotonic',return_value=10**12):
   self.assertEqual(replacement.relay_candidates(),[first,second])
 def test_stale_camera_key_refreshes_and_retries_once(self):
  c=Client({'device_public_key':'stale','token':'0'*64,'device_id':1})
  with patch.object(c,'_authorize_once',side_effect=[CameraAPIError(1103),None]) as attempt:
   with patch('account_auth.refresh_public_key',return_value='fresh') as refresh:c.authorize()
  self.assertEqual(attempt.call_count,2);refresh.assert_called_once_with('0'*64,1)
  self.assertEqual(c.c['device_public_key'],'fresh')
 def test_key_refresh_is_rate_limited_when_camera_is_unavailable(self):
  c=Client({'device_public_key':'stale','token':'0'*64,'device_id':1})
  with patch.object(c,'_authorize_once',side_effect=CameraAPIError(1103)):
   with patch('account_auth.refresh_public_key',return_value='fresh') as refresh:
    for _ in range(2):
     with self.assertRaises(CameraAPIError):c.authorize()
  self.assertEqual(refresh.call_count,1)
if __name__=='__main__':unittest.main()
