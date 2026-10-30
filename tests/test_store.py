import copy
import concurrent.futures
import pytest
from app.store import Store,Conflict

@pytest.fixture
def store(tmp_path):return Store(tmp_path/'sandbox.sqlite3')

def prepared(store,content='买白色听山 Air，可以直接下单',budget=320,hard=True):
    s=store.create_session();rid=store.submit(s['id'],content,'request-001');store.start(rid)
    intent={'version':1,'fields':{'execution_authorization':{'value':'granted','evidence':content,'evidence_verified':True}},
            'commitments':[{'category':'total_price','value':budget,'unit':'CNY','direction':'max','level':'inviolable' if hard else 'strong_preference'}]}
    receipt={'receipt_id':'receipt-'+rid,'status':'SIMULATED_EXECUTED','state_version':1,'intent_version':1,
             'parameters':{'item_id':'A01','quantity':1,'account_id':'sandbox-wallet'},'economics':{'amount_due_minor':29900}}
    store.record_quote(rid,receipt,intent)
    return s['id'],rid,intent,receipt,{'overall':'PASS','state_version':1}

def test_atomic_idempotent_order(store):
    sid,rid,intent,q,h2=prepared(store)
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        orders=list(pool.map(lambda _:store.commit(rid,q['receipt_id'],intent,h2),range(2)))
    assert orders[0]['id']==orders[1]['id']
    s=store.session(sid);assert len(s['orders'])==1
    assert s['state']['items'][0]['stock']==17
    assert s['state']['account']['balance_minor']==970100

@pytest.mark.parametrize('change',[{'kind':'price','item_id':'A01','value':39900},{'kind':'stock','item_id':'A01','value':0},{'kind':'revoke'},{'kind':'expire'}])
def test_changed_state_rejects_old_quote(store,change):
    sid,rid,intent,q,h2=prepared(store);store.change_environment(sid,change)
    with pytest.raises(Conflict):store.commit(rid,q['receipt_id'],intent,h2)
    assert store.session(sid)['orders']==[]

@pytest.mark.parametrize('hard,expected',[(True,False),(False,True)])
def test_budget_semantics(store,hard,expected):
    sid,rid,intent,q,h2=prepared(store,budget=290,hard=hard)
    if expected:assert store.commit(rid,q['receipt_id'],intent,h2)['total_minor']==29900
    else:
        with pytest.raises(Conflict,match='预算'):store.commit(rid,q['receipt_id'],intent,h2)

def test_cancel_and_revision_prevent_stale_commit(store):
    sid,rid,intent,q,h2=prepared(store);store.cancel(sid)
    with pytest.raises(Conflict):store.commit(rid,q['receipt_id'],intent,h2)
    new=store.submit(sid,'改买黑色，先不要下单','request-002')
    with pytest.raises(Conflict):store.checkpoint(rid)
    assert store.checkpoint(new)['messages'][-1]['content'].startswith('改买')

def test_authorization_cannot_come_from_assistant_or_prior_task(store):
    sid,rid,intent,q,h2=prepared(store)
    intent['fields']['execution_authorization']['evidence']='助手说允许购买'
    store.record_quote(rid,{**q,'receipt_id':'new'},intent)
    with pytest.raises(Conflict,match='明确授权'):store.commit(rid,'new',intent,h2)
    sid,rid,intent,q,h2=prepared(store);store.commit(rid,q['receipt_id'],intent,h2);store.finish(rid,'completed',{},'订单完成')
    next_rid=store.submit(sid,'看看键盘','next-task')
    assert [m['content'] for m in store.checkpoint(next_rid)['messages']]==['看看键盘']

def test_session_isolation_and_request_dedup(store):
    sid,rid,intent,q,h2=prepared(store)
    assert store.submit(sid,'完全相同的重发','request-001')==rid
    s2=store.create_session();r2=store.submit(s2['id'],'买一个','another-request');store.start(r2)
    with pytest.raises(Conflict,match='报价'):store.commit(r2,q['receipt_id'],intent,h2)
    assert store.session(s2['id'])['state']['items'][0]['stock']==18

def test_intent_h2_and_money_bindings(store):
    sid,rid,intent,q,h2=prepared(store)
    with pytest.raises(Conflict,match='意图'):store.commit(rid,q['receipt_id'],{**intent,'version':2},h2)
    with pytest.raises(Conflict,match='核验'):store.commit(rid,q['receipt_id'],intent,{'overall':'REJECT','state_version':1})
    q['receipt_id']='bad-amount';q['economics']['amount_due_minor']=1;store.record_quote(rid,q,intent)
    with pytest.raises(Conflict,match='金额'):store.commit(rid,q['receipt_id'],intent,h2)

def test_events_replay_without_duplicates(store):
    sid,rid,*_=prepared(store);a=store.events(sid);b=store.events(sid,a[-1]['id']);assert b==[]
    store.event(sid,rid,'test','工具','已返回',{'actual':True})
    assert len(store.events(sid,a[-1]['id']))==1
