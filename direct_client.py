#!/usr/bin/env python3
"""Authenticated V380 Neo cloud receiver, independent of Wine and the GUI."""
import argparse,base64,hashlib,http.client,json,logging,os,secrets,socket,struct,time
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey,X25519PublicKey
from cryptography.hazmat.primitives.ciphers import Cipher,algorithms,modes
from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat
LOG=logging.getLogger('v380-direct')
class CloudHTTPError(RuntimeError):pass
class CameraAPIError(RuntimeError):
 def __init__(self,code):self.code=code;super().__init__('Camera authorization code '+str(code))
class CloudAPIError(RuntimeError):
 def __init__(self,code):self.code=code;super().__init__('Cloud API code '+str(code))

def compact(obj):return json.dumps(obj,separators=(',',':'),ensure_ascii=False).encode()
def exact(sock,n):
 out=bytearray()
 while len(out)<n:
  chunk=sock.recv(n-len(out))
  if not chunk:raise EOFError('Camera transport closed')
  out.extend(chunk)
 return bytes(out)
def encrypt(raw,key):
 pad=16-len(raw)%16;enc=Cipher(algorithms.AES(key[:16]),modes.CBC(key[16:])).encryptor()
 return enc.update(raw+bytes([pad])*pad)+enc.finalize()
def decrypt(raw,key):
 dec=Cipher(algorithms.AES(key[:16]),modes.CBC(key[16:])).decryptor();out=dec.update(raw)+dec.finalize()
 pad=out[-1] if out else 0
 if not 1<=pad<=16 or out[-pad:]!=bytes([pad])*pad:raise ValueError('Invalid encrypted RPC padding')
 return out[:-pad]
def decode_media(body,key):
 if len(body)<32:raise ValueError('Truncated media header')
 dec=Cipher(algorithms.AES(key[:16]),modes.CBC(key[16:])).decryptor();h=dec.update(body[:32]);size=struct.unpack_from('<I',h,6)[0]
 if size>len(body)-32:raise ValueError('Truncated media payload')
 codec=h[1];start,num,skip=struct.unpack_from('<III',h,18);media=bytearray(body[32:])
 if codec in [1,2,3] and num:
  if num%16:raise ValueError('Invalid media cipher length')
  while start+num<=len(media):
   media[start:start+num]=dec.update(bytes(media[start:start+num]))
   if skip==0xffffffff:break
   start+=num+skip
 elif codec==4 and num:
  if num%16 or num>len(media):raise ValueError('Invalid audio cipher length')
  media[:num]=dec.update(bytes(media[:num]))
 return {'kind':h[0],'codec':codec,'frame_id':struct.unpack_from('<I',h,2)[0],'timestamp_ms':struct.unpack_from('<Q',h,10)[0],'payload':bytes(media[:size])}

class Client:
 def __init__(self,config,replica_index=1):
  self.c=config;self.replica_index=replica_index;self.sock=None;self.key=None;self.sid=None
  self.counter=secrets.randbelow(256);self.active_relay=None;self.media_host=None;self.phase='controller'
 def post(self,host,port,path,obj,host_header=None):
  raw=compact(obj);stamp=str(obj['timestamp']).encode();first=hashlib.md5(raw+stamp).hexdigest()
  signature=hashlib.md5((first+self.c['signature_suffix']).encode()).hexdigest()
  headers={'Content-Type':'application/json','Connection':'close','Signature':signature,'User-Agent':self.c.get('secret_headers',{}).get('user-agent','V380Direct/1')}
  if host_header:headers['Host']=host_header
  conn=http.client.HTTPConnection(host,port,timeout=self.c.get('request_timeout',4))
  try:
   conn.request('POST',path,raw,headers);response=conn.getresponse()
   if response.status!=200:raise CloudHTTPError('Cloud HTTP status '+str(response.status))
   data=response.read(256*1024+1)
   if len(data)>256*1024:raise ValueError('Oversized cloud reply')
   result=json.loads(data)
   if result.get('code') not in [1000,2000]:raise CloudAPIError(result.get('code'))
   return result
  finally:conn.close()
 def _authorize_once(self):
  private=X25519PrivateKey.generate();public=private.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw);peer=base64.b64decode(self.c['device_public_key'],validate=True)
  signature=bytes(a^b for a,b in zip(hashlib.md5(public).digest(),hashlib.md5(peer).digest()));now=int(time.time())
  body={'mr':{'params':{'access_token':self.c['token'],'device_id':self.c['device_id']}},'tr':{'id':secrets.randbits(32),'method_id':196866,'chn':0,'ts':now,'params':{'cKM':base64.b64encode(public).decode(),'dS':base64.b64encode(signature).decode()}},'timestamp':now}
  hosts=[self.c.get('secret_ip')]+self.c.get('secret_ips',[])
  for name in [self.c['secret_host'].split(':')[0]]+self.c.get('secret_hosts',[]):
   try:hosts.extend(x[4][0] for x in socket.getaddrinfo(name,80,socket.AF_INET,socket.SOCK_STREAM))
   except OSError:pass
  hosts=list(dict.fromkeys(x for x in hosts if x))[:8];result=None;tr=None;camera_code=None
  for host in hosts:
   try:
    candidate=self.post(host,80,self.c['secret_path'],body,self.c['secret_host'])
    answer=candidate.get('tr') or {};code=answer.get('result',{}).get('code')
    if code!=1000:
     camera_code=code
     LOG.warning('Controller %s could not authorize camera: code %s',host,code);continue
    if answer.get('data',{}).get('secure')!=2:raise RuntimeError('Unsupported camera encryption mode')
    result=candidate;tr=answer;self.c['secret_ip']=host;break
   except CloudAPIError as e:
    if e.code not in [1012,1005,1006]:raise
    LOG.info('Controller %s unavailable: %s; trying another address',host,e)
   except (OSError,http.client.HTTPException,CloudHTTPError,ValueError) as e:
    detail=str(e) if isinstance(e,CloudHTTPError) else type(e).__name__
    LOG.info('Controller %s request failed: %s; trying another address',host,detail)
  if result is None:
   if camera_code is not None:raise CameraAPIError(camera_code)
   raise RuntimeError('No controller could reach this camera')
  self.sid=tr['data']['sid'];self.key=private.exchange(X25519PublicKey.from_public_bytes(peer));LOG.info('Camera session authorized')
 def authorize(self):
  try:self._authorize_once()
  except CameraAPIError as error:
   if error.code!=1103:raise
   now=time.monotonic()
   if now<self.c.get('_key_refresh_after',0):raise
   self.c['_key_refresh_after']=now+30
   from account_auth import AccountError,refresh_public_key
   LOG.info('Refreshing camera public key through the cloud API')
   try:self.c['device_public_key']=refresh_public_key(self.c['token'],self.c['device_id'])
   except AccountError:
    self.c['_key_refresh_after']=time.monotonic()+8
    raise
   self._authorize_once()
 def refresh_relays(self):
  stamp=int(time.time());platform=self.c.get('dispatch_platform',20000)
  material=f"dev_id={self.c['device_id']}&platform={platform}&timestamp={stamp}"+self.c.get('dispatch_salt','hsdata2022')
  body={'dev_id':self.c['device_id'],'timestamp':stamp,'platform':platform,'sign':hashlib.sha1(material.encode()).hexdigest()}
  cached=self.c.get('relays',[]);fresh=[]
  hosts=list(dict.fromkeys([self.c.get('dispatch_host','dispatch.av380.net')]+self.c.get('dispatch_hosts',[])+['dispatch.av380.net','dispa1.av380.net']))[:4]
  for host in hosts:
   conn=http.client.HTTPConnection(host,self.c.get('dispatch_port',8001),timeout=3)
   try:
    conn.request('POST','/api/v1/get_stream_server',compact(body),{'Content-Type':'application/json','Connection':'close'});response=conn.getresponse();result=json.loads(response.read(65536))
    if result.get('code')==2000 and isinstance(result.get('data'),list) and result['data']:
     fresh=result['data'];self.c['dispatch_host']=host;break
   except (OSError,ValueError,http.client.HTTPException):LOG.info('Relay discovery endpoint unavailable; trying another address')
   finally:conn.close()
  # A regional DNS answer can point to another region: never discard known relays.
  pool=[];seen=set()
  for relay in cached[:6]+fresh:
   if not isinstance(relay,dict) or not isinstance(relay.get('ip'),str):continue
   identity=(relay['ip'],relay.get('tp',{}).get('call_port',80))
   if identity in seen:continue
   seen.add(identity);pool.append(relay)
  self.c['relays']=pool[:12]
  if not fresh:LOG.warning('Relay discovery failed; using cached authorized relays')
 def allocate(self,relay):
  body={'mr':{'params':{'access_token':self.c['token'],'device_id':self.c['device_id'],'call':self.c['call_value']}},'timestamp':int(time.time())}
  result=self.post(relay['ip'],relay.get('tp',{}).get('call_port',80),self.c['call_path'],body);answer=result.get('mr',{}).get('result',{})
  if not answer.get('channel_handle'):raise RuntimeError('Cloud relay did not allocate a channel')
  return answer
 def connect(self,relay):
  channel=self.allocate(relay);host=channel.get('app_trs_ip') or channel['trs_ip'];port=int(channel.get('app_trs_port') or channel['trs_port'])
  self.sock=socket.create_connection((host,port),timeout=4);self.sock.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1);self.sock.settimeout(10)
  hello=bytearray(1500);struct.pack_into('<III',hello,0,1100,self.c['device_id'],int(channel['channel_handle']));self.sock.sendall(hello);reply=exact(self.sock,64)
  cmd,code=struct.unpack_from('<II',reply)
  if cmd!=1101 or code!=1000:raise RuntimeError('Relay channel code '+str(code))
  self.active_relay=relay
  self.media_host=self.sock.getpeername()[0]
  LOG.info('Cloud video transport connected via relay %s, media peer %s',relay['ip'],self.media_host)
 def rpc(self,method,params):
  obj={'id':secrets.randbits(32),'chn':0,'method_id':method,'ts':int(time.time()),'params':params};body=encrypt(compact(obj),self.key);self.counter=(self.counter+1)&255
  magic=0x00030990 if method==199056 else 0x38030818
  request=struct.pack('<IBBBBII',magic,0,2,self.sid&255,self.counter,0,len(body))+body
  outer=struct.pack('<II',1200,len(request))+bytes(56);self.sock.sendall(outer+request)
 def receive(self):
  header=exact(self.sock,16);magic,chn,mode,sid,seq,res,n=struct.unpack('<IBBBBII',header)
  if n>2*1024*1024:raise ValueError('Oversized camera frame')
  body=exact(self.sock,n);kind=magic&0xfff
  if kind==0x820:return 'media',decode_media(body,self.key)
  if mode==2:body=decrypt(body,self.key)
  try:return 'rpc',json.loads(body)
  except (UnicodeDecodeError,json.JSONDecodeError):return 'other',{'kind':kind,'bytes':n}
 def close(self):
  if self.sock:
   try:self.sock.close()
   finally:self.sock=None
 def mark_failure(self):
  if not self.active_relay:return
  self.cool_down_relay(self.active_relay)
  LOG.info('Transport failed; temporarily preferring alternate relays')
 def cool_down_relay(self,relay):
  identity=relay['ip']+':'+str(relay.get('tp',{}).get('call_port',80))
  self.c.setdefault('_relay_cooldowns',{})[identity]=time.monotonic()+60
 def relay_candidates(self):
  relays=self.c['relays']
  if not relays:return []
  offset=(self.replica_index-1)%len(relays)
  # Keep the dispatch order stable so replicas keep separate first choices.
  choices=relays[offset:]+relays[:offset]
  cooldowns=self.c.get('_relay_cooldowns',{});now=time.monotonic()
  def rank(relay):
   identity=relay['ip']+':'+str(relay.get('tp',{}).get('call_port',80));until=cooldowns.get(identity,0)
   return (until>now,until if until>now else 0)
  return sorted(choices,key=rank)
 def start(self,quality=1):
  self.authorize();self.phase='relay discovery';self.refresh_relays()
  self.phase='relay'
  for relay in self.relay_candidates():
   try:
    self.connect(relay);break
   except (OSError,RuntimeError,ValueError) as e:
    self.close();self.cool_down_relay(relay)
    LOG.warning('Relay %s attempt failed: %s; trying next address',relay['ip'],type(e).__name__)
  else:raise RuntimeError('No cloud video relay available')
  self.phase='video'
  self.rpc(197122,{'up':{'quality':quality,'video':1,'audio':0,'vsspec':0},'down':{'vattr':{'codec':0,'width':0,'height':0},'aattr':{'codec':0,'samplerate':0,'bits':0,'chns':0}}})
 def capture(self,path,seconds,quality):
  self.start(quality)
  start=time.monotonic();heartbeat=start;first=None;last=None;frames=0;waiting_i=True;rpc_codes=[]
  fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
  try:
   with os.fdopen(fd,'wb') as out:
    while time.monotonic()-start<seconds:
     kind,data=self.receive()
     if kind=='rpc':
      code=(data.get('result') or {}).get('code')
      if data.get('method_id')!=197120:rpc_codes.append({'method':data.get('method_id'),'code':code});LOG.info('RPC method=%s code=%s',data.get('method_id'),code)
     elif kind=='media' and data['codec'] in [1,2,3]:
      if waiting_i and data['codec']!=1:continue
      waiting_i=False;out.write(data['payload']);frames+=1;first=data['timestamp_ms'] if first is None else first;last=data['timestamp_ms']
    out.flush();os.fsync(out.fileno())
   if not frames:raise RuntimeError('No decodable video frames received')
   return {'frames':frames,'camera_seconds':round((last-first)/1000,3),'wall_seconds':round(time.monotonic()-start,2),'rpc':rpc_codes}
  finally:self.close()

def main():
 p=argparse.ArgumentParser();p.add_argument('--credentials',required=True);p.add_argument('--output',required=True);p.add_argument('--seconds',type=float,default=30);p.add_argument('--quality',type=int,default=1);a=p.parse_args();logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
 c=json.loads(Path(a.credentials).read_text());client=Client(c)
 try:print(json.dumps(client.capture(a.output,a.seconds,a.quality)))
 finally:client.close()
if __name__=='__main__':main()
