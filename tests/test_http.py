import json
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from run import Handler
from store import Store

class HttpTests(unittest.TestCase):
    def test_actual_commands_replays_fencing_and_http_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.store=Store(Path(tmp)/'db',lambda:10000);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            def request(method,path,body=None,headers=None):
                c=HTTPConnection(*server.server_address,timeout=3);h={'Content-Type':'application/json',**(headers or {})};c.request(method,path,None if body is None else json.dumps(body),h);r=c.getresponse();status=r.status;raw=r.read();c.close();return status,json.loads(raw) if raw else None
            try:
                self.assertEqual(request('POST','/api/resources',{'id':'demo'})[0],201)
                a={'action':'acquire','holder':'A','request_key':'a','ttl_ms':1000};first=request('POST','/api/resources/demo/commands',a);self.assertEqual(first[0],200)
                self.assertTrue(request('POST','/api/resources/demo/commands',a)[1]['replayed'])
                b={**a,'holder':'B','request_key':'b'};self.assertEqual(request('POST','/api/resources/demo/commands',b)[0],409)
                server.store.clock=lambda:12000;b['request_key']='b2';self.assertEqual(request('POST','/api/resources/demo/commands',b)[1]['receipt']['after']['fence'],2)
                stale={'action':'write','holder':'A','request_key':'late','token':1,'expected_version':0,'value':'late'};status,result=request('POST','/api/resources/demo/commands',stale);self.assertEqual(status,409);self.assertEqual(result['receipt']['reason'],'stale_token')
                self.assertEqual(request('GET','/api/resources/demo')[1]['state']['value'],'')
                self.assertEqual(request('HEAD','/api/resources/demo'),(200,None));self.assertEqual(request('GET','/store.py')[0],404)
                self.assertEqual(request('GET','/api/resources',headers={'Host':'bad.example'})[0],400)
                self.assertEqual(request('POST','/api/resources',{'id':'other'},headers={'Origin':'https://example.com'})[0],403)
                self.assertEqual(request('POST','/api/resources',[],headers={'Content-Type':'application/json'})[0],400)
            finally:server.shutdown();server.server_close();thread.join()
