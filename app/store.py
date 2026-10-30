"""Single-server SQLite owner. Workers use scoped RPC, never share DB handles."""
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from pathlib import Path
from .catalog import initial_state, now

def uid(prefix): return f'{prefix}_{uuid.uuid4().hex[:16]}'
def encode(x): return json.dumps(x,ensure_ascii=False,separators=(',',':'),allow_nan=False)
def decode(x): return json.loads(x)
ACTIVE = ('queued','running')

class Conflict(Exception): pass
class NotFound(Exception): pass

class Store:
    def __init__(self,path):
        self.path=Path(path); self.path.parent.mkdir(parents=True,exist_ok=True)
        self.lock=threading.RLock()
        with self.tx() as c:
            # DELETE journal also works on the platform's persistent home mount;
            # only this server process opens SQLite, not isolated model workers.
            c.executescript('''
            CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,title TEXT,created TEXT,updated TEXT,state TEXT,messages TEXT,active_run TEXT,task_start INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,session_id TEXT,status TEXT,created TEXT,finished TEXT,result TEXT,cancelled INTEGER DEFAULT 0,request_key TEXT,UNIQUE(session_id,request_key));
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,session_id TEXT,run_id TEXT,kind TEXT,role TEXT,title TEXT,payload TEXT,created TEXT);
            CREATE INDEX IF NOT EXISTS events_session ON events(session_id,id);
            CREATE TABLE IF NOT EXISTS quotes(id TEXT PRIMARY KEY,session_id TEXT,run_id TEXT,payload TEXT,created TEXT);
            CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY,session_id TEXT,run_id TEXT UNIQUE,payload TEXT,created TEXT);
            ''')

    @contextmanager
    def tx(self):
        with self.lock:
            c=sqlite3.connect(str(self.path),timeout=20,isolation_level=None)
            c.row_factory=sqlite3.Row
            try:
                c.execute('BEGIN IMMEDIATE')
                yield c
                if c.in_transaction:c.commit()
            except BaseException:
                if c.in_transaction:c.rollback()
                raise
            finally:c.close()

    def _session(self,c,sid):
        row=c.execute('SELECT * FROM sessions WHERE id=?',(sid,)).fetchone()
        if row is None:raise NotFound('会话不存在')
        d=dict(row);d['state']=decode(d['state']);d['messages']=decode(d['messages'])
        return d

    def _event(self,c,sid,rid,kind,role,title,payload=None):
        cur=c.execute('INSERT INTO events(session_id,run_id,kind,role,title,payload,created) VALUES(?,?,?,?,?,?,?)',
                      (sid,rid,kind,role,title,encode(payload or {}),now()))
        return cur.lastrowid

    def event(self,sid,rid,kind,role,title,payload=None):
        with self.tx() as c:return self._event(c,sid,rid,kind,role,title,payload)

    def create_session(self):
        sid=uid('s');t=now()
        with self.tx() as c:
            c.execute('INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?)',(sid,'新的购物任务',t,t,encode(initial_state()),'[]',None,0))
        return self.session(sid)

    def list_sessions(self):
        with self.tx() as c:
            return [dict(x) for x in c.execute('SELECT id,title,created,updated,active_run FROM sessions ORDER BY updated DESC')]

    def _expire(self,c,s):
        state=s['state'];acct=state['account'];deadline=acct.get('authorization_expires_at')
        if acct['authorized'] and deadline and datetime.fromisoformat(deadline)<=datetime.now(timezone.utc):
            acct['authorized']=False;acct['authorization_version']+=1
            state['version']+=1;state['updated_at']=now()
            c.execute('UPDATE sessions SET state=? WHERE id=?',(encode(state),s['id']))
            self._event(c,s['id'],s['active_run'],'environment','商城','支付授权已到期',{'state_version':state['version'],'change':'authorization_expired'})

    def session(self,sid):
        with self.tx() as c:
            s=self._session(c,sid);self._expire(c,s)
            s['runs']=[{**dict(x),'result':decode(x['result']) if x['result'] else None} for x in c.execute('SELECT * FROM runs WHERE session_id=? ORDER BY created',(sid,))]
            s['orders']=[decode(x['payload']) for x in c.execute('SELECT payload FROM orders WHERE session_id=? ORDER BY created DESC',(sid,))]
            s['quotes']=[decode(x['payload']) for x in c.execute('SELECT payload FROM quotes WHERE session_id=? ORDER BY created DESC LIMIT 5',(sid,))]
            return s

    def submit(self,sid,content,request_key):
        content=content.strip()
        if not content or len(content)>5000:raise ValueError('请输入 1–5000 字的需求')
        with self.tx() as c:
            s=self._session(c,sid)
            duplicate=c.execute('SELECT id FROM runs WHERE session_id=? AND request_key=?',(sid,request_key)).fetchone()
            if duplicate:return duplicate['id']
            last=c.execute('SELECT status FROM runs WHERE session_id=? ORDER BY created DESC LIMIT 1',(sid,)).fetchone()
            if last and last['status']=='completed':s['task_start']=len(s['messages'])
            if s['active_run']:
                c.execute('UPDATE runs SET cancelled=1 WHERE id=? AND status IN (\'queued\',\'running\')',(s['active_run'],))
            rid=uid('r');message={'id':uid('m'),'role':'user','content':content,'created':now()}
            s['messages'].append(message)
            c.execute('INSERT INTO runs(id,session_id,status,created,request_key) VALUES(?,?,?,?,?)',(rid,sid,'queued',now(),request_key))
            c.execute('UPDATE sessions SET title=?,messages=?,active_run=?,updated=?,task_start=? WHERE id=?',
                      (content[:24] if s['title']=='新的购物任务' else s['title'],encode(s['messages']),rid,now(),s['task_start'],sid))
            self._event(c,sid,rid,'user','你','收到购物需求',{'content':content})
            self._event(c,sid,rid,'status','任务','等待运行',{})
            return rid

    def events(self,sid,after=0):
        with self.tx() as c:
            self._session(c,sid)
            return [{**dict(x),'payload':decode(x['payload'])} for x in c.execute('SELECT * FROM events WHERE session_id=? AND id>? ORDER BY id LIMIT 300',(sid,after))]

    def run(self,rid):
        with self.tx() as c:
            r=c.execute('SELECT * FROM runs WHERE id=?',(rid,)).fetchone()
            if not r:raise NotFound('任务不存在')
            return dict(r)

    def start(self,rid):
        with self.tx() as c:
            r=c.execute('SELECT * FROM runs WHERE id=?',(rid,)).fetchone()
            if not r or r['cancelled'] or r['status']!='queued':return False
            c.execute('UPDATE runs SET status=\'running\' WHERE id=?',(rid,));return True

    def checkpoint(self,rid):
        with self.tx() as c:
            r=c.execute('SELECT * FROM runs WHERE id=?',(rid,)).fetchone()
            if not r:raise NotFound('任务不存在')
            s=self._session(c,r['session_id']);self._expire(c,s)
            if r['cancelled'] or s['active_run']!=rid:raise Conflict('任务已取消或已被新需求替代')
            task_messages=s['messages'][s['task_start']:]
            previous=next((m for m in reversed(task_messages) if m['role']=='assistant' and m.get('run_id')),None)
            prior={}
            if previous:
                row=c.execute('SELECT result FROM runs WHERE id=? AND session_id=?',(previous['run_id'],s['id'])).fetchone()
                if row:prior=decode(row['result']) or {}
            return {'session_id':s['id'],'run_id':rid,'state':s['state'],'messages':task_messages,
                    'previous_intent':prior.get('current_intent'),'previous_offers':prior.get('offers',[]),
                    'prior_orders':[decode(x['payload']) for x in c.execute('SELECT payload FROM orders WHERE session_id=? ORDER BY created DESC LIMIT 3',(s['id'],))]}

    def cancel(self,sid):
        with self.tx() as c:
            s=self._session(c,sid)
            if s['active_run']:
                c.execute('UPDATE runs SET cancelled=1 WHERE id=? AND status IN (\'queued\',\'running\')',(s['active_run'],))
                self._event(c,sid,s['active_run'],'status','任务','已请求停止',{})

    def finish(self,rid,status,result,content):
        with self.tx() as c:
            r=c.execute('SELECT * FROM runs WHERE id=?',(rid,)).fetchone()
            if not r:return
            if r['status'] not in ACTIVE:return
            s=self._session(c,r['session_id'])
            if r['cancelled'] and status!='completed':status='cancelled'
            c.execute('UPDATE runs SET status=?,finished=?,result=? WHERE id=?',(status,now(),encode(result),rid))
            if s['active_run']==rid:
                if content:s['messages'].append({'id':uid('m'),'role':'assistant','content':content,'created':now(),'run_id':rid,'status':status})
                c.execute('UPDATE sessions SET active_run=NULL,messages=?,updated=? WHERE id=?',(encode(s['messages']),now(),s['id']))
            self._event(c,s['id'],rid,'finished','TAPay',content or '任务已停止',{'status':status,'result':result})

    def interrupt_previous(self):
        with self.tx() as c:
            rows=list(c.execute("SELECT id,session_id FROM runs WHERE status IN ('running','queued')"))
        for r in rows:self.finish(r['id'],'error',{'failure':'SERVER_RESTART'},'服务已重启，上一任务停止。订单记录已保留，请重新发起需求。')

    def change_environment(self,sid,change):
        with self.tx() as c:
            s=self._session(c,sid);state=s['state'];kind=change['kind']
            if kind in ('price','stock'):
                item=next((x for x in state['items'] if x['item_id']==change.get('item_id')),None)
                if not item:raise ValueError('商品不存在')
                value=change.get('value')
                if type(value) is not int or value<0 or value>1000000 or (kind=='price' and value==0):raise ValueError('数值不合法')
                key='unit_price_minor' if kind=='price' else 'stock';item[key]=value;item['quote_version']+=1
                label=f"{item['attributes']['name']} 的{'价格' if kind=='price' else '库存'}已更新"
            elif kind in ('revoke','restore','expire'):
                account=state['account'];account['authorized']=kind=='restore';account['authorization_version']+=1
                if kind=='expire':account['authorization_expires_at']=now()
                else:account.pop('authorization_expires_at',None)
                label={'revoke':'沙盒钱包授权已撤销','restore':'沙盒钱包授权已恢复','expire':'沙盒钱包授权已过期'}[kind]
            else:raise ValueError('不支持的环境变更')
            state['version']+=1;state['updated_at']=now()
            c.execute('UPDATE sessions SET state=?,updated=? WHERE id=?',(encode(state),now(),sid))
            self._event(c,sid,s['active_run'],'environment','商城',label,{**change,'state_version':state['version']})
        return self.session(sid)

    def record_quote(self,rid,receipt,intent):
        with self.tx() as c:
            r=c.execute('SELECT * FROM runs WHERE id=?',(rid,)).fetchone()
            if not r:raise NotFound('任务不存在')
            qid=receipt.get('receipt_id')
            if receipt.get('status')!='SIMULATED_EXECUTED' or not qid:raise ValueError('报价未成功')
            quote={'id':qid,'run_id':rid,'state_version':receipt['state_version'],'intent_version':receipt.get('intent_version'),
                   'parameters':receipt['parameters'],'economics':receipt['economics'],'created':now(),
                   'intent':deepcopy(intent),'receipt':deepcopy(receipt)}
            c.execute('INSERT OR IGNORE INTO quotes VALUES(?,?,?,?,?)',(qid,r['session_id'],rid,encode(quote),now()))
            return quote

    def commit(self,rid,receipt_id,intent,alignment):
        with self.tx() as c:
            existing=c.execute('SELECT payload FROM orders WHERE run_id=?',(rid,)).fetchone()
            if existing:return decode(existing['payload'])
            r=c.execute('SELECT * FROM runs WHERE id=?',(rid,)).fetchone()
            if not r:raise NotFound('任务不存在')
            s=self._session(c,r['session_id']);self._expire(c,s)
            if r['cancelled'] or r['status']!='running' or s['active_run']!=rid:raise Conflict('任务已经停止，不能提交订单')
            q=c.execute('SELECT * FROM quotes WHERE id=? AND run_id=? AND session_id=?',(receipt_id,rid,s['id'])).fetchone()
            if not q:raise Conflict('缺少当前任务的有效报价')
            quote=decode(q['payload']);state=s['state'];a=state['account'];params=quote['parameters']
            if quote['state_version']!=state['version']:raise Conflict('商城状态已变化，请重新核验')
            if (datetime.now(timezone.utc)-datetime.fromisoformat(quote['created'])).total_seconds()>900:raise Conflict('报价已过期，请重新查询')
            if quote['intent_version']!=intent.get('version') or quote['intent']!=intent:raise Conflict('意图版本与报价不匹配')
            auth=intent.get('fields',{}).get('execution_authorization',{})
            source='\n'.join(x['content'] for x in s['messages'][s['task_start']:] if x['role']=='user')
            span=auth.get('evidence','').strip()
            if auth.get('value')!='granted' or not span or span not in source or auth.get('evidence_verified') is False:
                raise Conflict('需要用户明确授权本次购买')
            if not alignment or alignment.get('overall') not in ('PASS','PASS_WITH_OBSERVATIONS') or alignment.get('state_version')!=state['version']:raise Conflict('缺少当前交易的通过核验')
            if not a['authorized'] or params.get('account_id')!=a['account_id']:raise Conflict('钱包授权不可用')
            item=next((x for x in state['items'] if x['item_id']==params.get('item_id')),None)
            quantity=params.get('quantity')
            if not item or type(quantity) is not int or not 1<=quantity<=10 or item['stock']<quantity:raise Conflict('商品或库存不可用')
            base=item['unit_price_minor']*quantity;shipping=0 if base>=state['free_shipping_min_minor'] else state['shipping_minor'];total=base+shipping
            if total!=quote['economics']['amount_due_minor'] or total>a['balance_minor'] or total>a['limit_minor']:raise Conflict('当前付款金额发生变化或超过钱包限制')
            for rule in intent.get('commitments',[]):
                if rule.get('category')=='total_price' and rule.get('level')=='inviolable':
                    if rule.get('unit')!='CNY' or rule.get('value') is None:raise Conflict('硬预算的币种或数值尚未查明')
                    limit=round(float(rule['value'])*100);direction=rule.get('direction')
                    if direction not in ('max','min','exact'):raise Conflict('硬预算比较关系尚未查明')
                    if (direction=='max' and total>limit) or (direction=='min' and total<limit) or (direction=='exact' and total!=limit):raise Conflict('当前付款不满足硬预算')
            order={'id':uid('TAP'),'session_id':s['id'],'run_id':rid,'item_id':item['item_id'],
                   'name':item['attributes']['name'],'color':item['attributes']['color'],'quantity':quantity,
                   'subtotal_minor':base,'shipping_minor':shipping,'total_minor':total,'currency':'CNY',
                   'receipt_id':receipt_id,'intent_version':intent['version'],'state_version':state['version'],
                   'created':now(),'status':'sandbox_paid'}
            item['stock']-=quantity;a['balance_minor']-=total;state['version']+=1;state['updated_at']=now()
            c.execute('INSERT INTO orders VALUES(?,?,?,?,?)',(order['id'],s['id'],rid,encode(order),now()))
            c.execute('UPDATE sessions SET state=? WHERE id=?',(encode(state),s['id']))
            self._event(c,s['id'],rid,'order','订单服务','沙盒订单已生成',order)
            return order
