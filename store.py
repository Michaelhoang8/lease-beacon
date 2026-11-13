"""SQLite lease authority and fenced document store, sharing one transaction boundary."""
import hashlib
import json
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


class Conflict(ValueError): pass
class Missing(ValueError): pass


def canonical(value): return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False)
def digest(text): return hashlib.sha256(text.encode()).hexdigest()
def identifier(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,47}',value): raise ValueError('Use 1–48 ASCII letters, digits, dots, hyphens, or underscores, starting with a letter or digit.')
    return value
def integer(value,low,high):
    if type(value) is not int or not low<=value<=high: raise ValueError(f'Expected an integer from {low} to {high}.')
    return value
def normalize(request):
    if not isinstance(request,dict): raise ValueError('A command must be an object.')
    action=request.get('action');fields={'action','holder','request_key'}
    if action in ('acquire','renew'): fields.add('ttl_ms')
    if action in ('renew','release','write'): fields.add('token')
    if action=='write': fields|={'expected_version','value'}
    if action not in ('acquire','renew','release','write') or set(request)!=fields: raise ValueError('Unknown command or unexpected command fields.')
    r=dict(request);identifier(r['holder']);identifier(r['request_key'])
    if 'ttl_ms' in r: integer(r['ttl_ms'],1000,60000)
    if 'token' in r: integer(r['token'],1,1000000000)
    if action=='write':
        integer(r['expected_version'],0,1000000000)
        if not isinstance(r['value'],str) or '\0' in r['value'] or any(0xd800<=ord(x)<=0xdfff for x in r['value']) or len(r['value'].encode())>2048: raise ValueError('Document values must be UTF-8 text up to 2 KiB, without NUL or unpaired surrogates.')
    return r
def initial(): return {'fence':0,'holder':None,'expires_ms':0,'version':0,'value':''}


def transition(before,request,now):
    after=dict(before);action=request['action'];active=before['holder'] is not None and now<before['expires_ms']
    if action=='acquire':
        if active: return after,False,'busy'
        after.update(fence=before['fence']+1,holder=request['holder'],expires_ms=now+request['ttl_ms'])
    else:
        if request['token']!=before['fence']: return after,False,'stale_token'
        if request['holder']!=before['holder']: return after,False,'wrong_holder'
        if not active: return after,False,'expired'
        if action=='renew': after['expires_ms']=max(before['expires_ms'],now+request['ttl_ms'])
        if action=='release': after.update(holder=None,expires_ms=0)
        if action=='write':
            if request['expected_version']!=before['version']: return after,False,'version_conflict'
            after.update(value=request['value'],version=before['version']+1)
    return after,True,'accepted'


class Store:
    def __init__(self,path,clock=None):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True);self.clock=clock or (lambda:time.time_ns()//1000000)
        with self.db() as c:
            c.executescript('''PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS resources(id TEXT PRIMARY KEY,state TEXT NOT NULL,clock INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS receipts(resource TEXT NOT NULL REFERENCES resources(id),request_key TEXT NOT NULL,request TEXT NOT NULL,sequence INTEGER NOT NULL,payload TEXT NOT NULL,hash TEXT NOT NULL,PRIMARY KEY(resource,request_key),UNIQUE(resource,sequence));
            CREATE TRIGGER IF NOT EXISTS receipts_no_update BEFORE UPDATE ON receipts BEGIN SELECT RAISE(ABORT,'immutable receipt'); END;
            CREATE TRIGGER IF NOT EXISTS receipts_no_delete BEFORE DELETE ON receipts BEGIN SELECT RAISE(ABORT,'immutable receipt'); END;''')
    @contextmanager
    def db(self):
        c=sqlite3.connect(self.path,timeout=10,isolation_level=None);c.row_factory=sqlite3.Row;c.execute('PRAGMA foreign_keys=ON')
        try: yield c
        finally: c.close()
    def _row(self,c,key):
        row=c.execute('SELECT * FROM resources WHERE id=?',(key,)).fetchone()
        if row is None: raise Missing('Resource not found.')
        return row
    def _time(self,c,row):
        now=max(integer(self.clock(),0,9007199254680000),row['clock'])
        c.execute('UPDATE resources SET clock=? WHERE id=?',(now,row['id']))
        return now
    def _audit(self,c,row):
        state=initial();previous='0'*64;entries=[];last_time=0
        for sequence,r in enumerate(c.execute('SELECT * FROM receipts WHERE resource=? ORDER BY sequence',(row['id'],)),1):
            try:
                p=json.loads(r['payload']);request=normalize(p['request']);after,allowed,reason=transition(state,request,p['at_ms'])
                valid=(r['sequence']==sequence==p['sequence'] and p['resource']==row['id'] and p['previous_hash']==previous and p['before']==state and p['after']==after and p['allowed']==allowed and p['reason']==reason and p['at_ms']>=last_time and canonical(request)==r['request'] and request['request_key']==r['request_key'] and digest(r['payload'])==r['hash'])
                if not valid: raise Conflict('Receipt chain or transition mismatch.')
            except (KeyError,TypeError,ValueError) as e: raise Conflict('Stored receipt integrity check failed.') from e
            state=after;previous=r['hash'];last_time=p['at_ms'];entries.append({'receipt':p,'hash':r['hash'],'canonical_payload':r['payload']})
        if canonical(state)!=row['state'] or row['clock']<last_time: raise Conflict('Stored state does not match receipt replay.')
        return state,entries,previous
    def create(self,key):
        identifier(key)
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE');row=c.execute('SELECT * FROM resources WHERE id=?',(key,)).fetchone()
            if row: c.execute('COMMIT');return {'id':key,'replayed':True}
            if c.execute('SELECT COUNT(*) FROM resources').fetchone()[0]>=100: raise Conflict('The workspace is limited to 100 resources.')
            c.execute('INSERT INTO resources VALUES(?,?,?)',(key,canonical(initial()),integer(self.clock(),0,9007199254680000)));c.execute('COMMIT');return {'id':key,'replayed':False}
    def list(self):
        with self.db() as c:return [r[0] for r in c.execute('SELECT id FROM resources ORDER BY id')]
    def snapshot(self,key):
        identifier(key)
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE');row=self._row(c,key);state,entries,_=self._audit(c,row);now=self._time(c,row);c.execute('COMMIT')
            return {'id':key,'now_ms':now,'state':state,'active':state['holder'] is not None and now<state['expires_ms'],'remaining_ms':max(0,state['expires_ms']-now),'entries':entries,'verified':True}
    def command(self,key,request):
        identifier(key);request=normalize(request);request_text=canonical(request)
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE');row=self._row(c,key);state,entries,previous=self._audit(c,row);now=self._time(c,row)
            cached=c.execute('SELECT * FROM receipts WHERE resource=? AND request_key=?',(key,request['request_key'])).fetchone()
            if cached:
                if cached['request']!=request_text: raise Conflict('Request key already belongs to a different command.')
                c.execute('COMMIT');return {'receipt':json.loads(cached['payload']),'hash':cached['hash'],'canonical_payload':cached['payload'],'replayed':True}
            if len(entries)>=1000 or c.execute('SELECT COUNT(*) FROM receipts').fetchone()[0]>=10000: raise Conflict('Receipt storage limit reached; use another workspace database.')
            after,allowed,reason=transition(state,request,now)
            payload={'format':'lease-beacon-receipt-v1','resource':key,'sequence':len(entries)+1,'at_ms':now,'request':request,'before':state,'after':after,'allowed':allowed,'reason':reason,'previous_hash':previous}
            raw=canonical(payload);sha=digest(raw)
            c.execute('UPDATE resources SET state=? WHERE id=?',(canonical(after),key))
            c.execute('INSERT INTO receipts VALUES(?,?,?,?,?,?)',(key,request['request_key'],request_text,len(entries)+1,raw,sha));c.execute('COMMIT')
            return {'receipt':payload,'hash':sha,'canonical_payload':raw,'replayed':False}
