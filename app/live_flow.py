"""Bounded application workflow over original SIRA/TIAA/DBDA interfaces.

The experimental free-routing graph remains in vendor. This application uses
explicit role handoff so an 8B model cannot route itself into a tool loop.
"""
import json
from types import SimpleNamespace
from typing import Literal
from pydantic import BaseModel, Field
from langchain_core.messages import SystemMessage, HumanMessage

class ShoppingPlan(BaseModel):
    decision: Literal['EXECUTE','BLOCK','REQUEST_CLARIFICATION']
    action_id: Literal['purchase']
    item_id: str | None
    quantity: int = Field(ge=1,le=10)
    account_id: Literal['sandbox-wallet']
    explanation: str = Field(description='简短中文理由或澄清问题，不得声称订单已完成')

class SourceReview(BaseModel):
    consistent: bool
    source_quote: str = Field(description='发生矛盾时引用最新相关用户原文，无矛盾可为空')
    reason: str = Field(description='中文核验结论，检查商品名称型号颜色数量及授权范围')

def compact_catalog(ledger):
    obs=ledger.public_environment.query()
    return {'currency':'CNY','state_version':ledger.current_version,
            'items':[{'item_id':x['item_id'],**x['attributes'],'price_yuan':x['unit_price_minor']/100,'stock':x['stock']} for x in obs['items']],
            'accounts':obs['accounts'],'shipping_yuan':obs['policy']['shipping_minor']/100,
            'free_shipping_yuan':obs['policy']['shipping_tiers'][0]['min_subtotal_minor']/100}

def run_application(case,llm,hooks,ctx,ledger):
    from payment.prediction import Prediction
    conversation=case.render_visible_conversation();trail=[];reviews=[]
    def enter(role):
        prior=trail[-1] if trail else None;trail.append(role);ctx.role=role
        ctx.event('handoff',role,'开始处理',{'from':prior,'to':role})
    def result(behavior,reason,params=None,receipt=None,evidence=None,failure=None):
        pred=Prediction(final_behavior=behavior,operation='purchase' if params else None,
                        action_id='purchase' if params else None,parameters=params,
                        receipt_id=(receipt or {}).get('receipt_id'),receipt=receipt,rationale=reason,
                        failure=failure,legal_finish=False)
        record={**pred.to_dict(),'engine':'live-explicit-role-workflow-v1',
                'application_finalized':not failure,'agent_trajectory':trail,'reviews':reviews,
                'receipts':ledger.receipts,'tool_log':ledger.tool_log,'alignment':evidence}
        return SimpleNamespace(prediction=pred,prediction_record=lambda:record)
    hooks.h1.configure(catalog_required={'purchase':['item_id','quantity','account_id']},
                       environment_snapshot=ledger.peek_catalog(),conversation_start_at=ledger.conversation_start_at,timezone='UTC')
    hooks.h2.configure(visible_conversation=conversation,simulation_max_calls=20)
    hooks.h3.configure(visible_conversation=conversation)
    enter('Payment Main Supervisor')
    # Each replan is caused by new state or a specific source contradiction.
    for attempt in range(3):
        ledger.refresh_live()
        if attempt==0 or getattr(hooks.h1,'repair_feedback',None):
            enter('Intent Structurer');intent=hooks.h1.structure(conversation)
            hooks.h1.repair_feedback=None;ledger.intent_version=intent.version
        enter('Payment Action Generator')
        catalog=compact_catalog(ledger)
        plan=llm.with_structured_output(ShoppingPlan).invoke([
            SystemMessage(content='你是购物规划角色。只根据当前用户会话、SIRA意图和公开目录形成计划。唯一 action_id 为 purchase。商品名称、型号、颜色必须一致；不能捏造编号或回执。缺商品选择或购买授权时澄清。硬预算含运费不可超过，软预算可说明差异。已有明确要求就不要重复询问。用中文说明。'),
            HumanMessage(content=json.dumps({'conversation':conversation,'intent':intent.compact(),'catalog':catalog},ensure_ascii=False))])
        ctx.event('proposal',ctx.role,'购物方案已生成',plan.model_dump())
        auth=intent.field('execution_authorization')
        if plan.decision=='REQUEST_CLARIFICATION' or intent.requires_clarification or auth.value!='granted':
            reason=intent.clarification_question or plan.explanation
            if auth.value!='granted' and not reason:reason='请确认商品和是否授权我完成本次沙盒购买。'
            return result('REQUEST_CLARIFICATION',reason)
        if plan.decision=='BLOCK' or getattr(intent,'requires_block',False):
            return result('BLOCK',plan.explanation)
        item=next((x for x in catalog['items'] if x['item_id']==plan.item_id),None)
        if item is None:return result(None,'模型选择了目录外商品，未下单。',failure='INVALID_PRODUCT')
        enter('Request/Intent Consistency Evaluator')
        check=llm.with_structured_output(SourceReview).invoke([
            SystemMessage(content='你独立核验用户原文与候选商品事实。检查型号后缀、颜色、数量、选择权限和预算软硬。最近修订只替代对应要求。目录编号是公开解析结果，不需用户知道编号。不能仅相信SIRA；商品颜色与原文明显不同必须指出。不要重新计算已公开价格。不要引入用户未说的要求。'),
            HumanMessage(content=json.dumps({'conversation':conversation,'intent':intent.compact(),'plan':plan.model_dump(),'product':item},ensure_ascii=False))])
        reviews.append(check.model_dump());ctx.event('review',ctx.role,'原文与商品核对完成',check.model_dump())
        if not check.consistent:
            if not check.source_quote or check.source_quote not in conversation:
                return result(None,'核验异议缺少可核对的原文，未下单。',failure='UNSUPPORTED_REVIEW')
            hooks.h1.repair_feedback=check.model_dump()
            ctx.event('repair','Intent Structurer','交回意图整理核对原文',check.model_dump());continue
        params={'item_id':plan.item_id,'quantity':plan.quantity,'account_id':plan.account_id}
        enter('Payment Action Generator')
        ledger.environment_query();ledger.shop_get_product_details(plan.item_id)
        payload=json.loads(ledger.simulate('purchase',params));receipt=payload.get('receipt',{})
        if receipt.get('status')!='SIMULATED_EXECUTED':
            ctx.event('alignment','H2','当前交易无法报价',payload)
            return result('BLOCK','当前库存或钱包授权不满足购买条件，请查看报价记录。',params,receipt)
        enter('H2')
        decision={'final_behavior':'EXECUTE','operation':'purchase','action_id':'purchase','parameters':params,'receipt_id':receipt['receipt_id'],'rationale':plan.explanation}
        evidence=hooks.h2.revalidate_final(decision,ledger.receipts,ledger.current_state,intent=intent,execution_context=ledger.execution_context())
        if evidence.get('semantic_status')=='unavailable':
            return result(None,'语义核验暂未完成，未下单。',params,receipt,evidence,failure='REVIEW_UNAVAILABLE')
        if evidence.get('overall') not in ('PASS','PASS_WITH_OBSERVATIONS'):
            why='当前交易未满足你的硬条件，未下单。'
            if any(c.get('check')=='economic_consistency' and c.get('verdict')=='FAIL' for c in evidence.get('rule_checks',[])):
                why=f"当前含运费总价为 ¥{receipt.get('total_price',0):.2f}，不满足你的硬预算，未下单。"
            return result('BLOCK',why,params,receipt,evidence)
        enter('H3');previous=ledger.current_version;ledger.refresh_live()
        if ledger.current_version!=previous:
            for transition in ledger.state_transitions:
                hooks.h3.on_environment_event(transition['events'],transition['previous_facts'],transition['current_facts'],intent=intent)
            ctx.event('replan','H3','状态变化，重新生成当前方案',{'state_version':ledger.current_version});continue
        bounded=hooks.h3.decide(decision,ledger.current_state,intent,evidence,ledger)
        if bounded.get('final_behavior')!='EXECUTE':return result('BLOCK','当前授权或环境条件已变化，未下单。',params,receipt,evidence)
        enter('Payment Main Supervisor')
        ctx.event('final_check',ctx.role,'当前方案已完成评审，准备提交沙盒订单',{'receipt_id':receipt['receipt_id'],'state_version':ledger.current_version})
        return result('EXECUTE',plan.explanation,params,receipt,evidence)
    return result(None,'环境或意图仍存在未解决变化，未下单。请补充需求后重试。',failure='UNRESOLVED_REPAIR')
