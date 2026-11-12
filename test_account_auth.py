"""Account contract checks based on the native client's QR states."""
import unittest
from unittest.mock import patch
import account_auth as auth

class AccountTests(unittest.TestCase):
 def test_scanned_qr_keeps_waiting_for_approval(self):
  session={'mac':'02:00:00:00:00:01','key':'a'*32}
  # Native client treats 30003/30004 as waiting, and 30002 as expired.
  for code,state in [(30001,'pending'),(30003,'scanned'),(30004,'scanned'),(30002,'expired')]:
   with patch.object(auth,'_account_request',return_value={'result':10022,'error_code':code}):
    self.assertEqual(auth.poll_qr(session)['state'],state)
 def test_confirmed_login_extracts_nested_token(self):
  with patch.object(auth,'_account_request',return_value={'result':0,'data':{'access_token':'0'*64,'user_id':1},'user_position':'US'}):
   result=auth.poll_qr({'mac':'02:00:00:00:00:01','key':'a'*32})
  self.assertEqual(result['state'],'confirmed');self.assertEqual(result['account']['token'],'0'*64)
 def test_invalid_qr_response_never_returns_arbitrary_payload(self):
  with patch.object(auth,'_account_request',return_value={'result':10022,'key':'not-a-qr-key'}):
   with self.assertRaises(auth.AccountError):auth.start_qr()
 def test_error_message_does_not_include_raw_server_secrets(self):
  with patch.object(auth,'_account_request',return_value={'result':10007,'error_code':10007,'data':{'access_token':'secret-value'}}):
   with self.assertRaises(auth.AccountError) as failure:auth.list_cameras('0'*64)
  self.assertNotIn('secret-value',str(failure.exception))
if __name__=='__main__':unittest.main()
