"""Exercise the real nested graphs, tools, version updates and commit boundary.

Replies are controlled; these are protocol tests, not model-accuracy results.
"""
import json
from copy import deepcopy
import pytest
from app.store import Store
from app import integration as mod
from three_domain import environment


def reading(value=None,evidence='',hardness='soft'):
    return dict(value=value,status='explicit' if value is not None else 'missing',hardness=hardness,evidence=evidence)


class Model:
    def __init__(self,draft,candidates=('A05',),query='耳机',quantity=1):
        self.draft=draft;self.candidates=candidates;self.calls=[];self.query=query;self.quantity=quantity
    def with_structured_output(self,schema):
        outer=self
        class Chain:
            def invoke(self,messages):
                outer.calls.append(schema.__name__)
                payload=json.loads(messages[-1].content)
                if schema.__name__=='LiveIntentDraft':
                    draft=outer.draft(payload) if callable(outer.draft) else deepcopy(outer.draft)
                    return schema(**draft)
                if schema.__name__=='OfferBindingPatch':return schema(confirmed_offer_id=None,confirmation_evidence='')
                if schema.__name__=='AuthorizationUpdate':return schema(value=payload['draft_authorization'],source_message_index=len(payload['user_messages'])-1)
                if schema.__name__=='ProductCategoryPatch':return schema(value='保温杯',evidence=payload['user_messages'][-1])
                if schema.__name__=='Route':return schema(next=payload['I']['allowed_next'][0],messages='按当前依据处理你的职责')
                if schema.__name__=='Lookup':return schema(query=outer.query,category='耳机')
                if schema.__name__=='Plan':return schema(candidates=[dict(item_id=x,reason='与预算偏好接近') for x in outer.candidates],quantity=outer.quantity,explanation='以下是当前可核对的商品方案。')
                if schema.__name__=='DecisionReview':return schema(decision_supported=True,reason='当前建议与已有事实相符')
                if schema.__name__=='Revision':return schema(resolution='accept',explanation='方案已完成独立核对。')
                if schema.__name__=='SemanticVerdict':return schema(verdict='PASS',observations=[],alignment_risk='low',reason='一致',evidence_quotes=[],mismatches=[])
                raise AssertionError(schema.__name__)
        return Chain()


def generic_draft():
    return dict(product_name=reading(),category=reading('耳机','耳机'),color=reading(),quantity=reading(1,'个'),
        execution_authorization='granted',authorization_evidence='帮我买',selection_mode='open',selection_evidence='',
        commitments=[dict(rule='价格230左右',level='strong_preference',category='total_price',value=230,unit='CNY',direction='target',evidence='价格230左右')])


def run(store,sid,text,model,on_event=None):
    rid=store.submit(sid,text,text);store.start(rid);events=[]
    original_rpc=mod.Context.rpc
    saved={k:getattr(mod.runner,k) for k in ('EnvironmentLedger','TrajectoryRecorder','build_team_info','get_payment_tools')}
    validator=environment.validate_world
    def rpc(self,method,path,body=None):
        if path=='checkpoint':return store.checkpoint(rid)
        if path=='quote':return store.record_quote(rid,body['receipt'],body['intent'])
        if path=='commit':return store.commit(rid,body['receipt_id'],body['intent'],body['alignment'])
        raise AssertionError(path)
    mod.Context.rpc=rpc
    try:
        def emit(e):
            events.append(e)
            if on_event:on_event(e)
        result=mod.run_live({'run_id':rid,'backend_url':'http://127.0.0.1','token':'test','model':{},'recursion_limit':60},emit,llm_override=model)
    finally:
        mod.Context.rpc=original_rpc;environment.validate_world=validator
        for k,v in saved.items():setattr(mod.runner,k,v)
    store.finish(rid,result['status'],result['result'],result['content'])
    return result,events


def test_category_search_recommend_confirm_preserves_soft_budget(tmp_path):
    store=Store(tmp_path/'app.sqlite');sid=store.create_session()['id'];model=Model(generic_draft())
    result,events=run(store,sid,'帮我买个耳机，价格230左右。',model)
    assert result['status']=='clarification'
    assert result['result']['offers'][0]['total_minor']==17700
    assert not store.session(sid)['orders']
    assert any(e['kind']=='tool_end' and e['payload'].get('tool')=='shop_search_products' for e in events)
    roles=[e['payload']['role_key'] for e in events if e['kind']=='agent_start']
    for role in ('Payment Main Supervisor','Payment Evaluation Supervisor','Payment Revisor',
                 'Environment/Feasibility Evaluator','Transaction/Outcome Evaluator'):
        assert role in roles
    def confirmed(payload):
        d=generic_draft();d.update(confirmed_offer_id=payload['previous_offers'][0]['offer_id'],
            confirmation_evidence='确认这个方案，可以下单',authorization_evidence='可以下单',selection_mode='selected')
        return d
    result2,events2=run(store,sid,'确认这个方案，可以下单',Model(confirmed))
    assert result2['status']=='completed',result2
    assert len(store.session(sid)['orders'])==1
    intent=result2['result']['current_intent']
    assert intent['commitments'][0]['level']=='strong_preference'
    assert intent['commitments'][0]['direction']=='target'
    assert intent['version']>result['result']['current_intent']['version']
    assert any(e['kind']=='alignment' for e in events2)


def test_bare_yes_cannot_select_between_candidates(tmp_path):
    store=Store(tmp_path/'app.sqlite');sid=store.create_session()['id']
    run(store,sid,'帮我买个耳机，价格230左右。',Model(generic_draft(),('A05','A01')))
    def wrong_confirmation(payload):
        d=generic_draft();d.update(confirmed_offer_id=payload['previous_offers'][0]['offer_id'],confirmation_evidence='是的')
        return d
    result,_=run(store,sid,'是的',Model(wrong_confirmation,('A05','A01')))
    assert result['status']=='clarification'
    assert not store.session(sid)['orders']


def test_hard_budget_rejects_quote_even_if_review_says_pass(tmp_path):
    store=Store(tmp_path/'app.sqlite');sid=store.create_session()['id'];d=generic_draft()
    d['commitments']=[dict(rule='不超过150元',level='inviolable',category='total_price',value=150,unit='CNY',direction='max',evidence='不超过150元')]
    result,events=run(store,sid,'帮我买个耳机，不超过150元。',Model(d))
    assert result['status']=='blocked'
    assert not result['result']['offers'] and not store.session(sid)['orders']


def test_confirmation_offer_is_isolated_by_session(tmp_path):
    store=Store(tmp_path/'app.sqlite');sid=store.create_session()['id'];other=store.create_session()['id']
    first,_=run(store,sid,'帮我买个耳机，价格230左右。',Model(generic_draft()))
    d=generic_draft();d.update(confirmed_offer_id=first['result']['offers'][0]['offer_id'],confirmation_evidence='确认这个方案')
    with pytest.raises(ValueError,match='INVALID_OFFER'):
        run(store,other,'帮我买个耳机，价格230左右。确认这个方案',Model(d))

def named_draft():
    d=generic_draft()
    d.update(product_name=reading('听山 Air','听山 Air'),color=reading('白色','白色','hard'),selection_mode='selected',selection_evidence='听山 Air')
    d['commitments']=[dict(rule='不超过320元',level='inviolable',category='total_price',value=320,unit='CNY',direction='max',evidence='不超过320元')]
    return d

def test_wrong_color_cannot_be_authorized_by_review_pass(tmp_path):
    store=Store(tmp_path/'app.sqlite');sid=store.create_session()['id']
    with pytest.raises(ValueError,match='literal_error|INVALID_PLANNER_CANDIDATE'):
        run(store,sid,'帮我买个白色听山 Air耳机，不超过320元。',Model(named_draft(),('A02',)))
    assert not store.session(sid)['orders']

def test_dynamic_price_rechecks_and_does_not_submit_stale_quote(tmp_path):
    store=Store(tmp_path/'app.sqlite');sid=store.create_session()['id'];changed=[]
    def change(e):
        if e['kind']=='quote' and not changed:
            changed.append(True);store.change_environment(sid,dict(kind='price',item_id='A01',value=34900))
    result,events=run(store,sid,'帮我买个白色听山 Air耳机，不超过320元。',Model(named_draft(),('A01',)),change)
    assert changed and result['status']=='blocked'
    assert not store.session(sid)['orders']
    assert any(e['kind']=='dynamic' and e['payload']['relevant'] for e in events)

def test_review_cannot_reinterpret_soft_preference_as_hard_veto():
    from app.hierarchy import Review,classify_review
    from payment.h1.schema import StructuredIntent,Commitment
    intent=StructuredIntent(commitments=[Commitment(rule='大约230元',level='strong_preference',category='total_price',value=230,unit='CNY',direction='target',evidence='大约230元')])
    review=Review(verdict='FAIL',reason='299高于230',issue_kind='constraint',target='commitments[0]')
    result=classify_review(review,intent)
    assert result['verdict']=='FAIL' and not result['blocking']
    assert intent.commitments[0].level=='strong_preference'
    intent.commitments[0].level='inviolable';intent.commitments[0].direction='max'
    assert classify_review(review,intent)['blocking']
    with pytest.raises(ValueError,match='TARGET'):
        classify_review(review.model_copy(update={'target':'nonexistent'}),intent)

def test_query_can_quote_without_inventing_required_quantity(tmp_path):
    store=Store(tmp_path/'app.sqlite');sid=store.create_session()['id'];draft=generic_draft()
    draft.update(quantity=reading(),execution_authorization='withheld',authorization_evidence='先推荐')
    result,_=run(store,sid,'先推荐耳机，价格230左右。',Model(draft))
    assert result['status']=='clarification' and result['result']['offers']
    assert result['result']['current_intent']['fields']['quantity']['value'] is None
    assert not store.session(sid)['orders']

def test_source_repair_can_only_change_citations():
    from app.intent_live import LiveIntentDraft,repair_citations
    d=generic_draft();d['color']=reading('白色','先看黑色。改白色。','hard');draft=LiveIntentDraft(**d)
    saved=draft.model_dump()
    class PatchModel:
        def with_structured_output(self,schema):
            class Chain:
                def invoke(self,_):return schema(color_evidence=2)
            return Chain()
    repair_citations(PatchModel(),draft,['帮我买个耳机，价格230左右。','先看黑色。','改白色。'])
    after=draft.model_dump();after['color']['evidence']=saved['color']['evidence']
    assert after==saved

def test_quote_projection_keeps_a_mismatched_quantity_visible():
    from app.hierarchy import quote_comparisons,quote_rules
    p={'offers':[dict(offer_id='one',receipt_id='r',item_id='A01',quantity=2,total_minor=59800,state_version=1,intent_version=2)]}
    receipt=dict(receipt_id='r',parameters={'item_id':'A01','quantity':1},economics={'amount_due_minor':29900},state_version=1,intent_version=2,status='SIMULATED_EXECUTED')
    row=quote_comparisons(p,[receipt])[0]
    assert row['proposed']['quantity']==2 and row['observed_quote']['quantity']==1
    assert row['observed_quote']['total_minor']==29900
    checks=quote_rules([row])
    assert not checks['consistent'] and {x['field'] for x in checks['mismatches']}=={'quantity','total_minor'}

@pytest.mark.parametrize('change',[dict(kind='stock',item_id='A01',value=0),dict(kind='expire')])
def test_dynamic_rejection_without_success_receipt_can_finish(tmp_path,change):
    store=Store(tmp_path/'app.sqlite');sid=store.create_session()['id'];changed=[]
    def apply(e):
        if e['kind']=='quote' and not changed:
            changed.append(True);store.change_environment(sid,change)
    result,events=run(store,sid,'帮我买个白色听山 Air耳机，不超过320元。',Model(named_draft(),('A01',)),apply)
    assert result['status']=='blocked' and not store.session(sid)['orders']

def test_compound_search_resolves_selected_soft_color_without_rewriting_it(tmp_path):
    store=Store(tmp_path/'app.sqlite');sid=store.create_session()['id'];draft=named_draft()
    draft['color']['hardness']='soft'
    result,events=run(store,sid,'帮我买个白色听山 Air耳机，不超过320元。',Model(draft,('A01',),query='听山 Air 白色'))
    assert result['status']=='completed'
    assert result['result']['current_intent']['fields']['color']['hardness']=='soft'
    queries=[e['payload']['arguments']['query'] for e in events if e['kind']=='tool_start' and e['payload'].get('tool')=='shop_search_products']
    assert queries==['听山 Air 白色','听山 Air']

@pytest.mark.parametrize('source_name',['耳机','蓝色拾音 Lite','拾音 Lite（A05）','蓝色拾音 Lite（A05）'])
def test_h1_binds_confirmed_identity_without_rewriting_source_name(tmp_path,source_name):
    store=Store(tmp_path/'app.sqlite');sid=store.create_session()['id']
    run(store,sid,'帮我买个耳机，价格230左右。',Model(generic_draft(),('A05','A01')))
    def confirmed(payload):
        d=generic_draft();d.update(product_name=reading(source_name,'确认买蓝色拾音 Lite，可以下单'),color=reading('蓝色','蓝色'),selection_mode='selected',
            confirmed_offer_id=payload['previous_offers'][0]['offer_id'],confirmation_evidence='确认买蓝色拾音 Lite，可以下单',authorization_evidence='可以下单')
        return d
    model=Model(confirmed)
    result,_=run(store,sid,'确认买蓝色拾音 Lite，可以下单',model)
    assert result['status']=='completed'
    intent=result['result']['current_intent']
    assert intent['fields']['product_name']['value']==source_name
    assert intent['fields']['product_name']['hardness']=='soft'
    assert intent['commitments'][0]['value']==230 and intent['commitments'][0]['level']=='strong_preference'
    assert intent['fields']['item_id']['value']=='A05'
    assert 'ProductNamePatch' not in model.calls

def test_h1_model_receives_utterances_without_ui_metadata():
    from app.intent_live import build_intent
    seen=[]
    class InspectModel:
        def with_structured_output(self,schema):
            class Chain:
                def invoke(self,messages):
                    seen.append(json.loads(messages[-1].content))
                    return schema(**generic_draft())
            return Chain()
    text='帮我买个耳机，价格230左右。'
    build_intent(InspectModel(),[dict(role='user',content=text,id='m_ui',created='2026-10-30T02:30:00Z',run_id='r_ui')])
    assert seen[0]['conversation']==[dict(role='user',content=text)]

def test_public_identity_aliases_keep_pro_suffix_and_color_distinct():
    from app.intent_live import matches_product_name
    from app.catalog import products
    air=next(p for p in products() if p['item_id']=='A01')
    assert matches_product_name('白色听山 Air（A01）',air)
    assert not matches_product_name('白色听山 Air Pro（A01）',air)
    assert not matches_product_name('黑色听山 Air',air)
    assert not matches_product_name('耳机',air)
    assert matches_product_name('耳机',air,allow_category=True)


@pytest.mark.parametrize('permission,index',[('granted',1),('withheld',1),('unclear',None)])
def test_authorization_handoff_cannot_change_selection_or_budget(permission,index):
    from app.intent_live import LiveIntentDraft,update_authorization
    draft=LiveIntentDraft(**named_draft());before=draft.model_dump();calls=[]
    class PermissionModel:
        def with_structured_output(self,schema):
            class Chain:
                def invoke(self,messages):
                    calls.append(json.loads(messages[-1].content))
                    return schema(value=permission,source_message_index=index)
            return Chain()
    users=['先看白色听山 Air，不超过320元，先别买。','可以下单。' if permission=='granted' else '先不要下单。']
    update_authorization(PermissionModel(),draft,users)
    after=draft.model_dump()
    assert len(calls)==1 and after['execution_authorization']==permission
    assert after['authorization_evidence']==('' if index is None else users[index])
    for key in ('execution_authorization','authorization_evidence'):after[key]=before[key]
    assert after==before


def test_soft_budget_wire_alias_preserves_value_and_strength():
    from app.intent_live import build_intent
    draft=generic_draft();draft['commitments'][0].update(unit='元',direction='around')
    result=build_intent(Model(draft),[dict(role='user',content='帮我买个耳机，价格230左右。')])
    assert result.commitments[0].value==230
    assert result.commitments[0].level=='strong_preference'
    assert result.commitments[0].unit=='CNY' and result.commitments[0].direction=='target'
    draft['commitments'][0]['level']='inviolable'
    with pytest.raises(ValueError,match='BUDGET_REPRESENTATION'):
        build_intent(Model(draft),[dict(role='user',content='帮我买个耳机，价格230左右。')])


def test_selected_soft_variant_can_resolve_despite_open_label(tmp_path):
    store=Store(tmp_path/'app.sqlite');sid=store.create_session()['id'];draft=named_draft()
    draft['color']['hardness']='soft';draft['selection_mode']='open'
    result,_=run(store,sid,'帮我买个白色听山 Air耳机，不超过320元。',Model(draft,('A01',)))
    assert result['status']=='completed'
    intent=result['result']['current_intent']
    assert intent['fields']['item_id']['value']=='A01'
    assert intent['fields']['color']['hardness']=='soft'


def test_real_graph_route_schema_exposes_only_current_legal_destinations(tmp_path):
    class InspectRoutes(Model):
        def with_structured_output(self,schema):
            original=super().with_structured_output(schema)
            class Chain:
                def invoke(self,messages):
                    if schema.__name__=='Route':
                        payload=json.loads(messages[-1].content)
                        allowed=payload['I']['allowed_next']
                        wire=schema.model_json_schema()['properties']['next']
                        exposed=wire.get('enum',[wire['const']] if 'const' in wire else [])
                        assert set(exposed)==set(allowed)
                        with pytest.raises(ValueError):
                            schema(next='Finished reviewer from previous stage',messages='repeat')
                    return original.invoke(messages)
            return Chain()
    store=Store(tmp_path/'app.sqlite');sid=store.create_session()['id']
    result,_=run(store,sid,'帮我买个白色听山 Air耳机，不超过320元。',InspectRoutes(named_draft(),('A01',)))
    assert result['status']=='completed'


def test_payment_category_is_repaired_by_h1_before_product_search(tmp_path):
    text='买2个白色山野随行杯，含运费不超过200元，可以下单。'
    draft=named_draft()
    draft.update(product_name=reading('山野随行杯',text,'hard'),category=reading('total_price',text,'hard'),
        color=reading('白色',text,'hard'),quantity=reading(2,text,'hard'),authorization_evidence=text,selection_evidence=text)
    draft['commitments']=[dict(rule='含运费不超过200元',level='inviolable',category='total_price',value=200,unit='CNY',direction='max',evidence=text)]
    store=Store(tmp_path/'app.sqlite');sid=store.create_session()['id']
    model=Model(draft,('D02',),query='山野随行杯',quantity=2)
    result,_=run(store,sid,text,model)
    assert result['status']=='completed'
    intent=result['result']['current_intent']
    assert intent['fields']['category']['value']=='保温杯'
    assert intent['fields']['quantity']['value']==2
    assert intent['commitments'][0]['value']==200 and intent['commitments'][0]['level']=='inviolable'
    assert 'ProductCategoryPatch' in model.calls
    assert store.session(sid)['orders'][0]['total_minor']==18600


def test_public_identity_accepts_category_suffix_without_losing_model_suffix():
    from app.intent_live import matches_product_name
    from app.catalog import products
    keyboard=next(p for p in products() if p['item_id']=='K05')
    assert matches_product_name('白色轻敲 Slim 键盘',keyboard)
    assert not matches_product_name('白色轻敲 Slim Pro 键盘',keyboard)
