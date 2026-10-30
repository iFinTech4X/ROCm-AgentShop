"""Real CNY ledger and hooks, controlled intent; this is not a model ACC test."""
from copy import deepcopy
from types import SimpleNamespace
import json
from app.store import Store

def test_native_cny_quote_and_dynamic_hooks(tmp_path,monkeypatch):
    from app import integration as mod
    from payment.h1.schema import StructuredIntent,FieldReading,Commitment
    from adapters.commerce import CommerceH1
    from three_domain import environment
    store=Store(tmp_path/'x.sqlite');session=store.create_session();sid=session['id']
    content='买白色听山 Air，最多320元，可以直接下单'
    rid=store.submit(sid,content,'controlled-test');store.start(rid)
    def rpc(self,method,path,body=None):
        if path=='checkpoint':return store.checkpoint(rid)
        if path=='quote':return store.record_quote(rid,body['receipt'],body['intent'])
        if path=='commit':return store.commit(rid,body['receipt_id'],body['intent'],body['alignment'])
        raise AssertionError(path)
    monkeypatch.setattr(mod.Context,'rpc',rpc)
    intent=StructuredIntent(scheme='purchase',fields={k:FieldReading(value=v,status='explicit',evidence=content,hardness='hard') for k,v in {'operation':'purchase','item_id':'A01','quantity':1,'account_id':'sandbox-wallet','execution_authorization':'granted'}.items()},commitments=[Commitment(rule='总价最多320元',category='total_price',level='inviolable',value=320,unit='CNY',direction='max',evidence='最多320元')])
    monkeypatch.setattr(CommerceH1,'_call',lambda *a,**kw:deepcopy(intent))
    original_validator=environment.validate_world
    original={k:getattr(mod.runner,k) for k in ('run_case','EnvironmentLedger','TrajectoryRecorder','build_team_info','get_payment_tools')}
    collected=[]
    def controlled(case,llm,hooks,**kw):
        ledger=mod.runner.EnvironmentLedger(case,'disabled');hooks.h1.configure(environment_snapshot=ledger.peek_catalog())
        current=hooks.h1.structure(case.render_visible_conversation());ledger.intent_version=current.version
        env=json.loads(ledger.environment_query());assert env['facts']['currency']=='CNY'
        quote=json.loads(ledger.simulate('purchase',{'item_id':'A01','quantity':1,'account_id':'sandbox-wallet'}))['receipt']
        assert quote['total_price']==299 and quote['currency']=='CNY'
        checked=hooks.h2._rule_layer({'operation':'purchase','action_id':'purchase','parameters':quote['parameters']},ledger.receipts,ledger.current_state,'EXECUTE',current)
        assert checked['hard_gate'] is None,checked
        before=deepcopy(ledger.current_state['facts']);store.change_environment(sid,{'kind':'price','item_id':'A01','value':34900});ledger.refresh_live()
        directive=hooks.h3.on_environment_event(['涨价'],before,ledger.current_state['facts'],intent=current)
        assert directive and ledger.current_version==2
        assert ledger.validate_prediction({'final_behavior':'EXECUTE','action_id':'purchase','parameters':quote['parameters'],'receipt_id':quote['receipt_id']})['status']=='REJECTED'
        new=json.loads(ledger.simulate('purchase',quote['parameters']))['receipt'];assert new['total_price']==349
        checked=hooks.h2._rule_layer({'operation':'purchase','action_id':'purchase','parameters':new['parameters']},ledger.receipts,ledger.current_state,'EXECUTE',current)
        assert checked['hard_gate']=='REJECT',checked
        return SimpleNamespace(prediction=SimpleNamespace(failure='CONTROLLED_STOP'),prediction_record=lambda:{'failure':'CONTROLLED_STOP'})
    mod.runner.run_case=controlled
    try:mod.run_live({'run_id':rid,'backend_url':'http://127.0.0.1','token':'test','model':{},'recursion_limit':60,'engine':'legacy'},collected.append,llm_override=object())
    finally:
        environment.validate_world=original_validator
        for k,v in original.items():setattr(mod.runner,k,v)
    assert any(e['kind']=='dynamic' for e in collected)
    assert len(store.session(sid)['quotes'])==2
