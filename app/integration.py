"""Live session adapter. The 49 archived RQ2 source files remain unchanged.

This module installs per-process adapters only. Each run starts a fresh process.
H1 finalize_intent, H2 rule/semantic verification and TalkHier routing are reused.
"""
from pathlib import Path
import sys
import json
import time
import threading
import uuid
from copy import deepcopy
import httpx

BASE=Path(__file__).resolve().parents[1]/'vendor/rq2'
for p in (BASE, BASE/'src', BASE/'business'):
    if str(p) not in sys.path:sys.path.insert(0,str(p))

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import SystemMessage, HumanMessage
from .model_adapter import LlamaCppChat
from payment.cases import PaymentCase
from payment.hooks import HookSet
from payment.h1.schema import StructuredIntent, SCHEME_REQUIRED_FIELDS
from multiagent.trajectory import TrajectoryRecorder
from adapters.commerce import BoundCommerceLedger, CommerceH1, CommerceH2, CommerceH3
import payment.runner as runner
import payment.tools as payment_tools
import three_domain.environment as business_environment
from .catalog import public_world

ROLE_NAMES={
 'Payment Main Supervisor':'总协调','Payment Action Generator':'购物规划',
 'Payment Evaluation Supervisor':'评审协调','Payment Revisor':'方案整理',
 'Request/Intent Consistency Evaluator':'需求一致性评审',
 'Environment/Feasibility Evaluator':'可行性评审','Transaction/Outcome Evaluator':'交易结果评审',
 'Intent Structurer':'意图整理 · SIRA','H2':'交易核验 · TIAA','H3':'动态调整 · DBDA'}

def public_payload(value):
    if isinstance(value,dict):return {k:public_payload(v) for k,v in value.items() if k not in ('thoughts','reasoning_content','api_key','token','background')}
    if isinstance(value,list):return [public_payload(v) for v in value]
    return value

class Context:
    def __init__(self,boot,emit):
        self.boot=boot;self.emit=emit;self.role='Intent Structurer';self.intent=None;self.ledger=None;self.last_h2=None
        self.node_id=None;self.last_node_id=None;self.intent_history=[];self.messages=[]
        self.client=httpx.Client(base_url=boot['backend_url'],headers={'X-Worker-Token':boot['token']},timeout=20,trust_env=False)
    def rpc(self,method,path,body=None):
        r=self.client.request(method,f"/api/_worker/{self.boot['run_id']}/{path}",json=body)
        if r.status_code==409:raise RuntimeError('TASK_CANCELLED_OR_STALE: '+r.json().get('detail',''))
        r.raise_for_status();return r.json()
    def event(self,kind,role,title,payload=None):
        self.emit({'type':'event','kind':kind,'role':ROLE_NAMES.get(role,role),'title':title,'payload':public_payload({'invocation_id':self.node_id,**(payload or {})})})
    def enter(self,role,kind='agent'):
        old=(self.role,self.node_id);node=uuid.uuid4().hex[:12]
        self.role=role;self.node_id=node
        self.event('agent_start',role,'开始处理',{'node_id':node,'from_node_id':self.last_node_id,'parent_node_id':old[1],'node_kind':kind,'role_key':role})
        self.last_node_id=node
        return old
    def leave(self,old):
        error=sys.exc_info()[0]
        self.event('agent_end',self.role,'本次处理异常' if error else '本次处理结束',
            {'node_id':self.node_id,'outcome':'error' if error else 'returned','error_type':error.__name__ if error else None})
        self.role,self.node_id=old

class ModelEvents(BaseCallbackHandler):
    def __init__(self,ctx):self.ctx=ctx;self.calls={};self.lock=threading.Lock()
    def on_chat_model_start(self,serialized,messages,*,run_id,**kwargs):
        role=self.ctx.role
        with self.lock:self.calls[str(run_id)]=(time.monotonic(),role)
        self.ctx.event('model_start',role,'正在调用模型',{'call_id':str(run_id)})
    def on_llm_end(self,response,*,run_id,**kwargs):
        with self.lock:started,role=self.calls.pop(str(run_id),(time.monotonic(),self.ctx.role))
        calls=[]
        for g in response.generations:
            for item in g:
                message=getattr(item,'message',None)
                calls.extend([{'name':t.get('name'),'arguments':t.get('args')} for t in getattr(message,'tool_calls',[])])
        self.ctx.event('model_end',role,'模型已返回',{'call_id':str(run_id),'seconds':round(time.monotonic()-started,2),
                       'tools':calls,'usage':(response.llm_output or {}).get('token_usage')})
    def on_llm_error(self,error,*,run_id,**kwargs):
        self.ctx.event('model_error',self.ctx.role,'模型调用失败',{'call_id':str(run_id),'error_type':type(error).__name__})

APP_PROMPT='''
LIVE SHOPPING APPLICATION CONTRACT:
This is a Chinese interactive shopping assistant. All user-facing rationale and clarification questions MUST be Chinese.
The public catalog is CNY (yuan); *_minor fields are integer fen (100 fen = 1 CNY). Never treat these as USD or perform FX.
The exact purchase parameters are item_id, quantity, account_id. No legacy sku/payment_method/amount/max_total_usd belongs in parameters.
The current SIRA structured intent is authoritative for the interpreted user requirements. Execution authorization is separate: fields.execution_authorization must be granted with a real user quote before EXECUTE. If the user only requests recommendations or asks to confirm first, return REQUEST_CLARIFICATION with the choice or authorization question.
Explicit product names may resolve through the public catalog. Preserve model suffix, color, quantity, the allowed selection conditions and all hard commitments. A soft budget remains soft.
This live session has no scripted future stages. The supplied public state is current; there is no obligation to advance any simulated scenario. Query before simulating; a simulation is a quote, NOT a completed order.
If a public change is observed, inspect affected goods and obtain a fresh bound simulation. Do not fabricate BLOCK for stale receipts, internal parse failures, or missing optional metadata.
Only exact receipt IDs from tool returns may be cited. Report concise findings, preserve the current proposal through evaluation, and FINISH when the current decision has been reviewed. No score/benchmark/gold exists.
'''

def run_live(boot,emit,llm_override=None):
    ctx=Context(boot,emit)
    data=ctx.rpc('GET','checkpoint');state=data['state'];messages=data['messages']
    ctx.messages=messages
    original_validate=business_environment.validate_world
    def cny_validator(spec):
        if spec.get('currency')!='CNY':raise ValueError('Live sandbox supports CNY only')
        # Reuse the original structural validator. Its USD-only pilot restriction
        # is lifted solely at this application boundary, with no monetary conversion.
        check=deepcopy(spec);check['currency']='USD';original_validate(check)
        return deepcopy(spec)
    business_environment.validate_world=cny_validator
    SCHEME_REQUIRED_FIELDS['purchase']=('item_id','quantity','account_id')

    class LiveLedger(BoundCommerceLedger):
        def __init__(self,case,release_policy='disabled'):
            super().__init__(case,release_policy=release_policy)
            self.public_environment.stage_index=1
            self.public_environment.state_version=state['version']
            self._sync();self.initial_state=deepcopy(self.current_state);ctx.ledger=self
        def refresh_live(self):
            snapshot=ctx.rpc('GET','checkpoint');new=snapshot['state']
            if new['version']!=self.current_version:
                previous=deepcopy(self.current_state)
                w=public_world(new,messages)
                self.public_environment.world=w;self.public_environment.now=new['updated_at']
                self.public_environment.state_version=new['version'];self._sync()
                self.observed_products=[]
                notice={'role':'environment','content':f"商城状态已更新至 v{new['version']}，请查询当前价格、库存与钱包授权。"}
                self.released_events.append(notice)
                self.state_transitions.append({'previous_facts':previous['facts'],'current_facts':deepcopy(self.current_state['facts']),
                                              'events':[notice],'state_version':new['version'],'chain_exhausted':True})
            return snapshot
        def environment_query(self,scope='current'):
            self.refresh_live();ctx.event('tool_start',ctx.role,'查询当前商城',{'tool':'environment_query'})
            result=json.loads(super().environment_query(scope))
            result['field_semantics']['amount_unit']='所有金额 *_minor 为整数分；币种 CNY，100 分等于 1 元。'
            result['field_semantics']['business_stages']='实时商城，无预设未来阶段。随时查询当前状态。'
            ctx.event('tool_end',ctx.role,'已读取商城状态',{'tool':'environment_query','state_version':self.current_version})
            return json.dumps(result,ensure_ascii=False)
        def shop_search_products(self,*a,**kw):
            self.refresh_live();ctx.event('tool_start',ctx.role,'搜索商品',{'tool':'shop_search_products','arguments':{'query':a[0] if a else kw.get('query',''),'category':a[1] if len(a)>1 else kw.get('category')}})
            result=super().shop_search_products(*a,**kw)
            ctx.event('tool_end',ctx.role,'商品搜索已返回',{'tool':'shop_search_products','result':json.loads(result) if result.startswith('{') else result});return result
        def shop_get_product_details(self,*a,**kw):
            self.refresh_live();ctx.event('tool_start',ctx.role,'查看商品详情',{'tool':'shop_get_product_details','arguments':list(a)})
            result=super().shop_get_product_details(*a,**kw)
            ctx.event('tool_end',ctx.role,'商品详情已返回',{'tool':'shop_get_product_details','result':json.loads(result) if result.startswith('{') else result});return result
        def simulate(self,action_id,parameters):
            self.refresh_live();ctx.event('tool_start',ctx.role,'计算并核验报价',{'tool':'simulate_transaction','action_id':action_id,'parameters':parameters})
            self.select_candidate(action_id,parameters)
            # CommerceLedger simulation, without BoundCommerceLedger's USD aliases.
            from adapters.commerce_ledger_source import CommerceLedger
            raw=CommerceLedger.simulate(self,action_id,parameters);payload=json.loads(raw)
            receipt=payload.get('receipt',{})
            if receipt.get('status')=='SIMULATED_EXECUTED':
                for r in self.receipts:
                    if r.get('receipt_id')==receipt['receipt_id']:
                        eco=r['economics'];r.update(authorization_version=self.execution_context()['authorization_version'],
                          total_price=eco['amount_due_minor']/100,total_fee=eco['fee_minor']/100,total_price_unit='CNY',total_fee_unit='CNY',fee_asset='CNY',currency='CNY')
                        receipt=deepcopy(r);payload['receipt']=receipt
                ctx.rpc('POST','quote',{'receipt':receipt,'intent':ctx.intent.compact() if ctx.intent else {}})
            ctx.event('quote',ctx.role,'报价已返回',{'tool':'simulate_transaction','receipt':receipt})
            return json.dumps(payload,ensure_ascii=False)

    class LiveH1(CommerceH1):
        def structure(self,conversation):
            if boot.get('engine')=='legacy':return super().structure(conversation)
            from .intent_live import build_intent
            old=ctx.enter('Intent Structurer')
            try:
                prior=ctx.intent.compact() if ctx.intent else data.get('previous_intent')
                intent=build_intent(self.llm,messages,prior,data.get('previous_offers'),getattr(self,'repair_feedback',None))
                self.repair_feedback=None;self._conversation=conversation;self._current=intent
                ctx.intent=intent;ctx.intent_history.append(intent.compact());ctx.ledger.intent_version=intent.version
                ctx.event('intent','Intent Structurer','当前意图已生成',intent.compact())
                return intent
            finally:ctx.leave(old)
        def on_tool_evidence(self,observations,state_version):
            return self._ground(observations,state_version)
        def bind_candidate(self,item_id):
            return self._ground(ctx.ledger.observed_products,ctx.ledger.current_version,item_id)
        def _ground(self,observations,state_version,item_id=None):
            if not ctx.intent:return None
            from .intent_live import ground_observed
            products=list({p['item_id']:p for p in observations if 'item_id' in p}.values())
            updated=ground_observed(ctx.intent,products,state_version,item_id)
            if updated:
                self._current=updated;ctx.intent=updated;ctx.ledger.intent_version=updated.version
                ctx.intent_history.append(updated.compact())
                old=ctx.enter('Intent Structurer','module')
                try:ctx.event('intent','Intent Structurer','公开查询证据已更新当前意图',updated.compact())
                finally:ctx.leave(old)
            return updated

    ledger_ref=[]
    def ledger_factory(case,release_policy):
        ledger=LiveLedger(case,release_policy);ledger_ref.append(ledger);return ledger

    class LiveH2(CommerceH2):
        def verify(self,*a,**kw):
            old=ctx.enter('H2')
            try:
                result=super().verify(*a,**kw)
                if result:ctx.last_h2=result;ctx.event('alignment','H2','交易核验已返回',result)
                return result
            finally:ctx.leave(old)
        def revalidate_final(self,decision,receipts,snapshot,intent=None,**kw):
            return super().revalidate_final(decision,receipts,snapshot,intent=intent,**kw)

    class LiveH3(CommerceH3):
        def on_environment_event(self,events,previous_facts,current_facts,**kw):
            original=super().on_environment_event(events,previous_facts,current_facts,**kw)
            # The legacy fast loop tracked DeFi risk/expiry. Commerce adds
            # candidate price/stock and wallet authorization to the same hook.
            old=previous_facts.get('commerce_public_observation',{});new=current_facts.get('commerce_public_observation',{})
            candidate=current_facts.get('commerce_candidate') or {}
            item_id=candidate.get('item_id')
            old_items=[x for x in old.get('items',[]) if not item_id or x['item_id']==item_id]
            new_items=[x for x in new.get('items',[]) if not item_id or x['item_id']==item_id]
            relevant=old_items!=new_items or old.get('accounts')!=new.get('accounts')
            directive=original or ('当前交易相关价格、库存或授权已变化；重新查询并获取当前版本报价与核验，旧回执不可执行。' if relevant else None)
            ctx.event('dynamic','H3','已检查商城变化',{'relevant':relevant,'directive':directive,'state_version':ctx.ledger.current_version})
            return directive
        def decide(self,*a,**kw):
            old=ctx.enter('H3','module')
            try:
                ctx.ledger.refresh_live()
                result=super().decide(*a,**kw)
                ctx.event('boundary','H3','动态边界检查完成',{'decision':result,'reason':self.last_adjustment_reason})
                return result
            finally:ctx.leave(old)

    class LiveRecorder(TrajectoryRecorder):
        def enter(self,node):
            if ctx.ledger:ctx.ledger.refresh_live()
            previous=self.entries[-1] if self.entries else None
            super().enter(node);ctx.role=node
            ctx.event('handoff',node,'开始处理',{'from':ROLE_NAMES.get(previous,previous),'to':ROLE_NAMES.get(node,node),'node':node})

    original_build=runner.build_team_info
    def build(*args,**kwargs):
        kwargs['shop_enabled']=False
        team,desc=original_build(*args,**kwargs)
        def adapt(obj):
            if isinstance(obj,dict):
                for k,v in list(obj.items()):
                    if k in ('prompt','additional_prompt') and isinstance(v,str):obj[k]=v+APP_PROMPT
                    elif k=='tools' and isinstance(v,list) and 'environment_query' in v:
                        obj[k]=list(dict.fromkeys(v+['shop_search_products','shop_get_product_details']))
                    elif isinstance(v,dict):adapt(v)
        adapt(team);return team,desc
    original_tools=payment_tools.get_payment_tools
    def tools(selector,ledger):
        result=original_tools(selector,ledger)
        for t in result:
            if t.name=='environment_query':t.description='查询当前人民币商城、商品目录、价格、库存和钱包状态；不会推进任何阶段。'
            if t.name=='simulate_transaction':t.description='核验 purchase 报价。参数只有 item_id、quantity、account_id；金额以 CNY 整数分计算。不创建订单。'
            if t.name=='shop_get_product_details':t.description='查询商品详情；参数 sku 接受目录的 item_id。'
        return result
    runner.EnvironmentLedger=ledger_factory;runner.TrajectoryRecorder=LiveRecorder;runner.build_team_info=build;runner.get_payment_tools=tools
    conf=boot['model']
    llm=llm_override or LlamaCppChat(model=conf['name'],base_url=conf['url'],api_key=conf['api_key'],
                   temperature=0,timeout=conf['timeout'],max_retries=2,max_tokens=4096,
                   extra_body={'chat_template_kwargs':{'enable_thinking':False}},callbacks=[ModelEvents(ctx)])
    hooks=HookSet(h1=LiveH1(llm),h2=LiveH2(llm,ledger_ref),h3=LiveH3())
    case=PaymentCase(case_id=boot['run_id'],dataset_kind='live',conversation=[{'role':m['role'],'content':m['content']} for m in messages],
                     environment={'public_spec':public_world(state,messages)},gold={},task_reference={},workflow_reference=None,split='live',
                     runtime_policy={'simulation_permitted':True,'simulation_max_calls':20})
    try:
        if boot.get('engine')=='legacy':
            result=runner.run_case(case,llm,hooks,release_policy='disabled',recursion_limit=boot['recursion_limit'],verbose=False)
        else:
            from .hierarchy import run_application
            result=run_application(case,llm,hooks,ctx,ledger_factory(case,'disabled'))
        record=result.prediction_record()
        if result.prediction.failure:
            return {'status':'error','content':'本次任务未完成，运行记录已保留。可以调整需求后重试。','result':record}
        intent=ctx.intent.compact() if ctx.intent else {};record['current_intent']=intent
        behavior=result.prediction.final_behavior
        if behavior=='EXECUTE':
            ctx.ledger.refresh_live()
            checked=ctx.ledger.validate_prediction(result.prediction)
            if checked.get('status')!='VALIDATED':
                return {'status':'error','content':'当前交易证据已失效，未生成订单。请重新发起核验。','result':{**record,'submission':checked}}
            # The exact final action is independently checked once more by H2.
            evidence=hooks.h2.revalidate_final(result.prediction.to_dict(),ctx.ledger.receipts,ctx.ledger.current_state,intent=ctx.intent,execution_context=ctx.ledger.execution_context())
            if evidence.get('semantic_status')=='unavailable' or evidence.get('overall') not in ('PASS','PASS_WITH_OBSERVATIONS'):
                return {'status':'error','content':'提交前的当前核验未通过，未生成订单。','result':{**record,'final_alignment':evidence}}
            auth=intent.get('fields',{}).get('execution_authorization',{})
            if auth.get('value')!='granted':
                return {'status':'clarification','content':'商品方案已准备好。是否授权我按当前商品和数量完成这次沙盒购买？','result':record}
            order=ctx.rpc('POST','commit',{'receipt_id':result.prediction.receipt_id,'intent':intent,'alignment':evidence})
            return {'status':'completed','content':f"已完成沙盒购买：{order['name']}（{order['color']}）× {order['quantity']}，实付 ¥{order['total_minor']/100:.2f}。订单 {order['id']} 已保存。",'result':{**record,'order':order}}
        if behavior=='REQUEST_CLARIFICATION':
            if record.get('offers'):
                offers=record['offers']
                lines=[result.prediction.rationale or '已按当前需求查询到这些候选：']
                for i,o in enumerate(offers,1):
                    lines.append(f"{i}. {o['color']}{o['name']} × {o['quantity']}，含运费 ¥{o['total_minor']/100:.2f}。{o['reason']}")
                lines.append('请选择一个具体方案并确认是否下单；当前尚未创建订单。')
                return {'status':'clarification','content':'\n'.join(lines),'result':record}
            question=intent.get('clarification_question') or result.prediction.rationale or '请补充你希望购买的商品和授权范围。'
            return {'status':'clarification','content':question,'result':record}
        return {'status':'blocked','content':result.prediction.rationale or '当前条件不能完成购买，请查看核验结果。','result':record}
    finally:ctx.client.close()
