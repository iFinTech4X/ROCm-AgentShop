"""Controlled full role handoff; not a real-model accuracy claim."""
from copy import deepcopy
import pytest
from app.store import Store

@pytest.mark.parametrize('authority,expected',[('granted','completed'),('withheld','clarification')])
def test_full_application_uses_native_quote_review_and_commit(tmp_path,monkeypatch,authority,expected):
    from app import integration as mod
    from three_domain import environment
    store=Store(tmp_path/'app.sqlite');sid=store.create_session()['id']
    content='帮我买白色听山 Air 一副，含运费不超过320元，符合条件就下单' if authority=='granted' else '白色听山 Air 一副，含运费不超过320元，先别下单'
    rid=store.submit(sid,content,'full-flow-test');store.start(rid)
    def rpc(self,method,path,body=None):
        if path=='checkpoint':return store.checkpoint(rid)
        if path=='quote':return store.record_quote(rid,body['receipt'],body['intent'])
        if path=='commit':return store.commit(rid,body['receipt_id'],body['intent'],body['alignment'])
        raise AssertionError(path)
    monkeypatch.setattr(mod.Context,'rpc',rpc)
    calls=[]
    class Model:
        def with_structured_output(self,schema):
            class Chain:
                def invoke(self,messages):
                    calls.append(schema.__name__)
                    import json
                    from test_hierarchy_live import reading
                    if schema.__name__=='LiveIntentDraft':
                        return schema(product_name=reading('听山 Air','听山 Air'),color=reading('白色','白色','hard'),category=reading(),quantity=reading(1,'一副'),execution_authorization=authority,authorization_evidence=content,selection_mode='selected',selection_evidence='听山 Air',commitments=[dict(rule='不超过320元',level='inviolable',category='total_price',value=320,unit='CNY',direction='max',evidence='不超过320元')])
                    if schema.__name__=='Route':return schema(next=json.loads(messages[-1].content)['I']['allowed_next'][0],messages='按当前证据执行职责')
                    if schema.__name__=='Lookup':return schema(query='听山 Air')
                    if schema.__name__=='Plan':return schema(candidates=[dict(item_id='A01',reason='指定白色型号')],quantity=1,explanation='当前商品符合条件')
                    if schema.__name__=='DecisionReview':return schema(decision_supported=True,reason='符合当前依据')
                    if schema.__name__=='Revision':return schema(resolution='accept',explanation='当前方案已核对')
                    if schema.__name__=='SemanticVerdict':return schema(verdict='PASS',observations=[],alignment_risk='low',reason='一致',evidence_quotes=[],mismatches=[])
                    raise AssertionError(schema)
            return Chain()
    saved={k:getattr(mod.runner,k) for k in ('EnvironmentLedger','TrajectoryRecorder','build_team_info','get_payment_tools')}
    saved_validator=environment.validate_world;events=[]
    try:
        result=mod.run_live({'run_id':rid,'backend_url':'http://127.0.0.1','token':'test','model':{},'recursion_limit':60},events.append,llm_override=Model())
    finally:
        environment.validate_world=saved_validator
        for k,v in saved.items():setattr(mod.runner,k,v)
    assert result['status']==expected,result
    orders=store.session(sid)['orders'];assert len(orders)==(authority=='granted')
    if orders:
        assert orders[0]['total_minor']==29900 and orders[0]['item_id']=='A01'
        assert calls.count('DecisionReview')==3 and 'SemanticVerdict' in calls and 'Revision' in calls
        assert any(e['kind']=='boundary' for e in events)
