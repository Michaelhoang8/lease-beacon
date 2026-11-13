import concurrent.futures
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from store import Store,Conflict


class LeaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.now=10000;self.path=Path(self.tmp.name)/'leases.db';self.s=Store(self.path,lambda:self.now);self.s.create('document');self.sequence=0
    def tearDown(self): self.tmp.cleanup()
    def command(self,action,holder='A',**kw):
        self.sequence+=1;return self.s.command('document',{'action':action,'holder':holder,'request_key':str(self.sequence),**kw})['receipt']
    def test_expiry_boundary_takeover_and_delayed_write(self):
        self.assertEqual(self.command('acquire',ttl_ms=1000)['after']['fence'],1)
        self.command('write',token=1,expected_version=0,value='first')
        self.now=11000;self.assertEqual(self.command('renew',token=1,ttl_ms=1000)['reason'],'expired')
        self.assertEqual(self.command('acquire','B',ttl_ms=1000)['after']['fence'],2)
        self.command('write','B',token=2,expected_version=1,value='new owner')
        stale=self.command('write',token=1,expected_version=1,value='late old owner');self.assertEqual(stale['reason'],'stale_token');self.assertEqual(stale['after']['value'],'new owner')
    def test_concurrent_acquisitions_have_one_winner(self):
        def acquire(i):return self.s.command('document',{'action':'acquire','holder':f'C{i}','request_key':f'k{i}','ttl_ms':1000})
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:r=list(pool.map(acquire,range(20)))
        self.assertEqual(sum(x['receipt']['allowed'] for x in r),1);self.assertEqual(self.s.snapshot('document')['state']['fence'],1)
    def test_concurrent_same_request_is_one_effect_and_expired_replay_stays_old(self):
        request={'action':'acquire','holder':'A','request_key':'same','ttl_ms':1000}
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:r=list(pool.map(lambda _:self.s.command('document',request),range(20)))
        self.assertEqual(sum(not x['replayed'] for x in r),1);self.assertEqual(len({x['hash'] for x in r}),1)
        self.now+=2000;again=self.s.command('document',request);self.assertTrue(again['replayed']);self.assertEqual(again['receipt']['after']['expires_ms'],11000);self.assertFalse(self.s.snapshot('document')['active'])
        with self.assertRaises(Conflict):self.s.command('document',{**request,'ttl_ms':2000})
    def test_denials_replay_without_becoming_allowed(self):
        self.command('acquire',ttl_ms=1000);r={'action':'acquire','holder':'B','request_key':'busy','ttl_ms':1000};first=self.s.command('document',r)
        self.now+=2000;again=self.s.command('document',r);self.assertFalse(again['receipt']['allowed']);self.assertEqual(first['hash'],again['hash']);self.assertEqual(self.s.snapshot('document')['state']['fence'],1)
    def test_version_compare_and_swap_and_wrong_holder(self):
        self.command('acquire',ttl_ms=1000)
        def write(i):return self.s.command('document',{'action':'write','holder':'A','token':1,'expected_version':0,'value':str(i),'request_key':f'w{i}'})
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:r=list(pool.map(write,range(16)))
        self.assertEqual(sum(x['receipt']['allowed'] for x in r),1)
        self.assertEqual(self.command('write','B',token=1,expected_version=1,value='forged')['reason'],'wrong_holder')
    def test_renewal_never_shortens_lease_release_preserves_fence(self):
        self.command('acquire',ttl_ms=60000);self.now+=100
        self.assertEqual(self.command('renew',token=1,ttl_ms=1000)['after']['expires_ms'],70000)
        self.command('release',token=1);self.assertFalse(self.s.snapshot('document')['active'])
        self.assertEqual(self.command('acquire',ttl_ms=1000)['after']['fence'],2)
        self.assertEqual(self.command('release',token=1)['reason'],'stale_token')
    def test_clock_high_water_persists_across_reads_and_restart(self):
        self.command('acquire',ttl_ms=1000);self.now=12000;self.assertFalse(self.s.snapshot('document')['active'])
        self.now=9000;self.s=Store(self.path,lambda:self.now);self.assertFalse(self.s.snapshot('document')['active']);self.assertEqual(self.s.snapshot('document')['now_ms'],12000)
        self.assertEqual(self.command('acquire','B',ttl_ms=1000)['after']['expires_ms'],13000)
    def test_transaction_rolls_back_clock_state_and_receipt_on_failure(self):
        with self.s.db() as c:c.execute("CREATE TRIGGER fail BEFORE INSERT ON receipts BEGIN SELECT RAISE(ABORT,'injected'); END")
        self.now=15000
        with self.assertRaises(sqlite3.IntegrityError):self.command('acquire',ttl_ms=1000)
        with self.s.db() as c:
            row=c.execute('SELECT * FROM resources').fetchone();self.assertEqual(row['clock'],10000);self.assertEqual(json.loads(row['state'])['fence'],0);self.assertEqual(c.execute('SELECT COUNT(*) FROM receipts').fetchone()[0],0)
    def test_receipts_are_immutable_and_state_tampering_is_detected(self):
        self.command('acquire',ttl_ms=1000)
        with self.s.db() as c:
            with self.assertRaises(sqlite3.IntegrityError):c.execute('DELETE FROM receipts')
            c.execute("UPDATE resources SET state='{}'")
        with self.assertRaises(Conflict):self.s.snapshot('document')
        with self.assertRaises(Conflict):self.command('release',token=1)
    def test_validation_and_repeated_create_do_not_reset_state(self):
        self.command('acquire',ttl_ms=1000);self.assertTrue(self.s.create('document')['replayed']);self.assertEqual(self.s.snapshot('document')['state']['fence'],1)
        for ttl in [True,0,1000.0,60001]:
            with self.assertRaises(ValueError):self.command('acquire',ttl_ms=ttl)
        with self.assertRaises(ValueError):self.command('write',token=1,expected_version=0,value='\ud800')
        with self.assertRaises(ValueError):self.command('write',token=1,expected_version=0,value='x'*2049)
