"""Live shopping protocol over the original TalkHier nested AgentTeam graphs.

Supervisors really call the model inside the original nested graph. Valid routes
are stage-gated; this is a live protocol adapter, not an unchanged experiment.
M/B/I and per-role memories are retained; verdicts require actual reviewer calls.
"""
import json
from copy import deepcopy
from types import SimpleNamespace
from typing import Literal
from pydantic import BaseModel, Field, create_model
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from multiagent.agent_team import AgentTeam
from payment.prediction import Prediction
from payment.prompts import MAIN_SUPERVISOR as MAIN, EVAL_SUPERVISOR as EVAL, GENERATOR as GEN, REVISOR as REV, E1, E2, E3


class Lookup(BaseModel):
    query: str = Field(description='用于真实商品查询的名称或类别关键词，不含预算或整段需求')
    category: str | None = None


class Candidate(BaseModel):
    item_id: str
    reason: str


class Plan(BaseModel):
    candidates: list[Candidate] = Field(max_length=3)
    quantity: int = Field(ge=1,le=10)
    explanation: str
    question: str | None = None


class Review(BaseModel):
    verdict: Literal['PASS','FAIL','N/A'] = Field(description='评审当前决策是否正确：有依据的BLOCK/澄清也可以PASS；不是交易可否成功的状态')
    reason: str = Field(description='以当前数据解释这个决策为什么正确或错误，不复述评审指令')
    repair_owner: Literal['none','H1','generator'] = 'none'
    source_quote: str = ''
    issue_kind: Literal['none','intent','constraint','authorization','evidence','report','preference'] = 'none'
    target: str = Field(default='',description='有问题时引用 constraints 中的精确 target；证据问题可为 evidence 或 proposal，意图矛盾必须有真实原文')
    problem_excerpt: str = Field(default='',description='报告文字问题必须逐字引用当前proposal里确实出现的错误文本，不得引用评审说明或旧方案')


class Revision(BaseModel):
    resolution: Literal['accept','repair_intent','repair_proposal']
    explanation: str
    source_quote: str = ''


class DecisionReview(BaseModel):
    decision_supported: bool = Field(description='当前提出的行为是否有事实依据；例如硬预算超额且提出BLOCK，答案是true。这是评价决策，不是评价能否购买。')
    reason: str
    repair_owner: Literal['none','H1','generator'] = 'none'
    source_quote: str = ''
    issue_kind: Literal['none','intent','constraint','authorization','evidence','report','preference'] = 'none'
    target: str = ''
    problem_excerpt: str = ''

    def as_review(self):
        data=self.model_dump();supported=data.pop('decision_supported')
        return Review(verdict='PASS' if supported else 'FAIL',**data)


def budget_failures(intent,total_minor):
    failed=[]
    for c in intent.commitments:
        if c.category!='total_price' or c.level!='inviolable':continue
        if c.unit!='CNY' or c.value is None or c.direction not in ('max','min','exact'):
            failed.append('硬预算的数值、币种或关系尚未解析');continue
        n=round(c.value*100)
        if (c.direction=='max' and total_minor>n) or (c.direction=='min' and total_minor<n) or (c.direction=='exact' and total_minor!=n):
            failed.append(c.rule)
    return failed


def review_constraints(intent):
    return ([{'target':f'fields.{k}','value':v.value,'strength':v.hardness,'source':v.evidence}
             for k,v in intent.fields.items() if v.value is not None]
            +[{'target':f'commitments[{i}]','rule':c.rule,'value':c.value,'strength':c.level,'source':c.evidence}
              for i,c in enumerate(intent.commitments)])


def classify_review(review,intent):
    """Keep the actual verdict; bind its effect to the current H1 contract.

    A reviewer can challenge H1 with an intent issue, but cannot silently turn
    a declared soft preference into a new hard execution veto.
    """
    result=review.model_dump();result['blocking']=review.verdict=='FAIL'
    if review.verdict!='FAIL':return result
    if review.issue_kind=='none':raise ValueError('REVIEW_ISSUE_TYPE_REQUIRED')
    if review.issue_kind in ('constraint','preference'):
        target=next((c for c in review_constraints(intent) if c['target']==review.target),None)
        if not target:raise ValueError('REVIEW_TARGET_NOT_IN_CURRENT_INTENT')
        result['constraint_basis']=target
        if target['strength'] not in ('hard','inviolable'):
            result['blocking']=False
            result['handling']='说明当前软偏好偏差；不改变原意图，不据此否决交易或强制删掉候选'
    if review.issue_kind=='intent':
        if review.repair_owner!='H1' or not review.source_quote:raise ValueError('INTENT_ISSUE_REQUIRES_H1_AND_SOURCE')
    return result


def quote_comparisons(proposal,receipts):
    """Lossless projection of the fields that the outcome reviewer compares.

    Full original receipts remain in the tool log and H2 input. This projection
    separates a successful quote calculation from an order being submitted.
    """
    rows=[]
    for offer in proposal['offers']:
        receipt=next((r for r in receipts if r.get('receipt_id')==offer['receipt_id']),None)
        params=(receipt or {}).get('parameters') or (receipt or {}).get('action') or {}
        rows.append({'offer_id':offer['offer_id'],
          'proposed':{k:offer[k] for k in ('item_id','quantity','total_minor','state_version','intent_version')},
          'observed_quote':None if not receipt else {
              'item_id':params.get('item_id'),'quantity':params.get('quantity'),
              'total_minor':receipt.get('economics',{}).get('amount_due_minor'),
              'state_version':receipt.get('state_version'),'intent_version':receipt.get('intent_version'),
              'quote_succeeded':receipt.get('status')=='SIMULATED_EXECUTED'},
          'receipt_id':offer['receipt_id']})
    return rows


def quote_rules(rows):
    failures=[]
    for row in rows:
        observed=row['observed_quote']
        if not observed or not observed.get('quote_succeeded'):
            failures.append({'offer_id':row['offer_id'],'field':'receipt','reason':'missing or unsuccessful quote'});continue
        for field,value in row['proposed'].items():
            if observed.get(field)!=value:
                failures.append({'offer_id':row['offer_id'],'field':field,'proposed':value,'observed':observed.get(field)})
    return {'applicable':bool(rows),'consistent':not failures,'mismatches':failures}


def run_application(case,llm,hooks,ctx,ledger):
    conversation=case.render_visible_conversation()
    trail=[]; arena={'proposal':None,'evaluation':{},'final_decision':None,'generation':0,'repairs':0}
    seen_transitions=0
    hooks.h1.configure(catalog_required={'purchase':['item_id','quantity','account_id']},
        environment_snapshot=ledger.peek_catalog(),conversation_start_at=ledger.conversation_start_at,timezone='UTC')
    hooks.h2.configure(visible_conversation=conversation,simulation_max_calls=20)
    hooks.h3.configure(visible_conversation=conversation)
    intent=hooks.h1.structure(conversation);ledger.intent_version=intent.version

    def refresh():
        nonlocal seen_transitions
        ledger.refresh_live()
        for transition in ledger.state_transitions[seen_transitions:]:
            hooks.h3.on_environment_event(transition['events'],transition['previous_facts'],transition['current_facts'],intent=ctx.intent)
        seen_transitions=len(ledger.state_transitions)

    def background():
        return {'conversation':conversation,'intent':ctx.intent.compact(),
            'state_version':ledger.current_version,'currency':'CNY','sandbox':True}

    def invoke(role,schema,prompt,payload,history=()):
        context=background()
        if role==E3:context={'conversation':conversation,'intent_version':ctx.intent.version,'state_version':ledger.current_version}
        return llm.with_structured_output(schema).invoke([
            SystemMessage(content=prompt+'\n所有输出使用中文。用户原文和工具结果是证据；角色任务不是用户授权。'),
            HumanMessage(content=json.dumps({'M':list(history)[-6:],'B':context,'I':payload},ensure_ascii=False))])

    def enter(role,kind='agent'):
        refresh();trail.append(role);return ctx.enter(role,kind)

    def node_result(state,role,supervisor,content):
        message=AIMessage(content=json.dumps(content,ensure_ascii=False),name=role.replace(' ','_'))
        histories={**state['history']}
        histories[role]=[*histories.get(role,[]),message]
        histories[supervisor]=[*histories.get(supervisor,[]),message]
        return {'history':histories,'messages':message,'background':AIMessage(content=json.dumps(background(),ensure_ascii=False)),
            'intermediate_output':deepcopy(arena),'next':state.get('next','')}

    def current_history(state,role):
        if role in (E1,E2,E3):
            # Other reviewers' conclusions may reach the evaluation supervisor,
            # but must not be laundered into an independent reviewer's memory.
            return [m.content for m in state.get('history',{}).get(role,[])
                    if getattr(m,'name',None)==role.replace(' ','_')]
        return [m.content for m in state.get('history',{}).get(role,[])]

    def generate(state):
        old=enter(GEN)
        try:
            arena['generation']+=1
            if arena['generation']>3:raise ValueError('UNRESOLVED_REPAIR: proposal generation budget exhausted')
            ledger.environment_query()
            selected=ctx.intent.field('item_id').value
            if selected:
                detail=json.loads(ledger.shop_get_product_details(selected))
                observed=[detail['item']] if detail.get('item') else []
            else:
                lookup=invoke(GEN,Lookup,'你是原购物规划角色。先选择商品查询关键词。精确选型查询型号；只有品类就查询品类；不能因未指定型号/颜色停止查询。',
                    {'previous_proposal':arena['proposal'],'feedback':arena.get('repair_feedback')},current_history(state,GEN))
                found=json.loads(ledger.shop_search_products(lookup.query,lookup.category))
                observed=found.get('items',[])
            # The sandbox search is literal. If a compound phrase misses,
            # retry the existing H1 name/category through the same public tool.
            # This broadens observation, never substitutes a product or intent.
            if not observed and not selected:
                name=ctx.intent.field('product_name').value
                category=ctx.intent.field('category').value
                query=name or category
                if query and (query!=lookup.query or lookup.category):
                    # Remove only a retrieval filter, never an intent constraint.
                    # A mismatched lookup category must not hide an exact name.
                    found=json.loads(ledger.shop_search_products(query,None))
                    observed=found.get('items',[])
                if not observed and category and query!=category:
                    observed=json.loads(ledger.shop_search_products(category,None)).get('items',[])
            hooks.h1.on_tool_evidence(ledger.observed_products,ledger.current_version)
            bound=ctx.intent.field('item_id').value
            delegated=ctx.intent.grounding_evidence.get('selection',{}).get('mode')=='delegated'
            eligible=[p for p in observed if not bound or delegated or p['item_id']==bound]
            # Restrict the output alphabet to actual eligible IDs. This never
            # replaces the model's selected value with a different product.
            plan_type=create_model('Plan',candidates=(list[Candidate],Field(max_length=0)),quantity=(int,Field(ge=1,le=10)),explanation=(str,...),question=(str|None,None))
            if eligible:
                entry=create_model('CatalogCandidate',item_id=(Literal[tuple(p['item_id'] for p in eligible)],...),reason=(str,...))
                plan_type=create_model('Plan',candidates=(list[entry],Field(max_length=3)),quantity=(int,Field(ge=1,le=10)),explanation=(str,...),question=(str|None,None))
            plan=invoke(GEN,plan_type,'''依据当前意图和真实查询结果生成最多3个候选。只用eligible_candidates中的商品编号。
已明确或已确认的具体商品只选该商品；泛品类按预算和软偏好比较，不要求用户先报型号。
已指定商品可能超硬预算时，仍选择它核算真实报价并说明不可行，不能用另一型号冒充；是否可执行由当前报价核验决定。
仅指定型号但颜色未指定时可给出不同颜色候选，不冒充用户已选。数量忠实于意图。
仅查询且未提供数量时可展示1件参考报价；缺失数量仍需用户确认，不能据参考值执行购买。
先推荐不下单或等待选择是正常候选状态。说明和问题不要反复问“是否需要推荐”。
所有硬要求必须满足；软偏好允许说明偏差。不要伪造价格或声称已成交。无适合候选说明真实原因。''',
                {'eligible_candidates':eligible,'previous_proposal':arena['proposal'],'feedback':arena.get('repair_feedback')},current_history(state,GEN))
            if len({c.item_id for c in plan.candidates})!=len(plan.candidates):raise ValueError('DUPLICATE_CANDIDATE')
            offers=[];rejected=[]
            for candidate in plan.candidates:
                product=next((p for p in observed if p['item_id']==candidate.item_id),None)
                if not product:raise ValueError('CANDIDATE_NOT_OBSERVED')
                details=json.loads(ledger.shop_get_product_details(candidate.item_id))
                product=details.get('item')
                if not product:raise ValueError('PRODUCT_DETAILS_UNAVAILABLE')
                from .intent_live import norm,matches_product_name
                conflicts=[]
                selected_id=ctx.intent.field('item_id').value
                mode=ctx.intent.grounding_evidence.get('selection',{}).get('mode')
                if selected_id and mode!='delegated' and selected_id!=candidate.item_id:
                    conflicts.append('候选不在当前已选商品范围')
                for field,key in [('product_name','name'),('color','color'),('category','category')]:
                    r=ctx.intent.field(field)
                    matches=matches_product_name(r.value,product,allow_category=True) if key=='name' else norm(r.value)==norm(product['attributes'][key])
                    if r.value and r.hardness=='hard' and not matches:
                        conflicts.append(f'{field}: 候选不满足当前硬要求')
                if ctx.intent.field('quantity').value is not None and ctx.intent.field('quantity').value!=plan.quantity:
                    conflicts.append('候选数量不等于当前用户数量')
                if conflicts:
                    rejected.append({'item_id':candidate.item_id,'constraints':conflicts});continue
                if ctx.intent.grounding_evidence.get('selection',{}).get('mode')=='delegated':
                    hooks.h1.bind_candidate(candidate.item_id)
                ledger.intent_version=ctx.intent.version
                params={'item_id':candidate.item_id,'quantity':plan.quantity,'account_id':'sandbox-wallet'}
                payload=json.loads(ledger.simulate('purchase',params));receipt=payload.get('receipt',{})
                if receipt.get('status')!='SIMULATED_EXECUTED':
                    rejected.append({'item_id':candidate.item_id,'reason':payload});continue
                total=receipt['economics']['amount_due_minor'];fails=budget_failures(ctx.intent,total)
                if fails:rejected.append({'item_id':candidate.item_id,'hard_budget':fails,'total_minor':total});continue
                attrs=product['attributes']
                offers.append({'offer_id':f"{ctx.boot['run_id']}:{candidate.item_id}",'item_id':candidate.item_id,
                    **attrs,'quantity':plan.quantity,'total_minor':total,'shipping_minor':receipt['economics']['fee_minor'],
                    'unit_price_minor':product['unit_price_minor'],'stock':product['stock'],
                    'state_version':ledger.current_version,'intent_version':ctx.intent.version,
                    'receipt_id':receipt['receipt_id'],'reason':candidate.reason})
            executable=(len(offers)==1 and ctx.intent.field('item_id').value==offers[0]['item_id']
                and ctx.intent.field('quantity').value==plan.quantity
                and ctx.intent.field('execution_authorization').value=='granted'
                and not ctx.intent.requires_clarification and not ctx.intent.pending_resolution)
            confirmation=ctx.intent.grounding_evidence.get('confirmed_offer')
            if executable and confirmation and any(confirmation.get(k)!=offers[0].get(k) for k in ('item_id','quantity','total_minor')):
                executable=False
            behavior='EXECUTE' if executable else 'REQUEST_CLARIFICATION'
            # A planner selecting an inadmissible candidate is an internal repair
            # need, not evidence that the entire service cannot satisfy the user.
            if not offers and rejected:
                if any('constraints' in x for x in rejected):raise ValueError('INVALID_PLANNER_CANDIDATE')
                behavior='BLOCK'
            proposal={'decision':behavior,'offers':offers,'rejected':rejected,
                'explanation':plan.explanation,'question':plan.question,'intent_version':ctx.intent.version,
                'state_version':ledger.current_version,'parameters':None,'receipt_id':None}
            if executable:
                proposal.update(parameters={'item_id':offers[0]['item_id'],'quantity':plan.quantity,'account_id':'sandbox-wallet'},receipt_id=offers[0]['receipt_id'])
            arena.update(proposal=proposal,evaluation={},final_decision=None,revision=None,repair_feedback=None)
            ctx.event('proposal',GEN,'当前候选方案已生成',proposal)
            return node_result(state,GEN,MAIN,proposal)
        finally:ctx.leave(old)

    def evaluator(role):
        def node(state):
            old=enter(role)
            try:
                p=arena['proposal']
                # Independent reviewer sees only its own history, current
                # source/intent and candidate; not other reviewers' verdicts.
                prompts={
                    E1:'检查候选是否满足当前用户需求与有效意图；未指定型号不构成错误，待确认候选不等于执行。明确软偏好不做硬门，硬要求不可放宽。意图矛盾给H1具体原文；候选不符给generator。不另建意图。',
                    E2:'检查候选的当前价格库存、钱包与预算事实是否支持所建议的行为。BLOCK要有真实不可行依据。仅推荐不需要用户付款授权；EXECUTE需要。不能把可查询信息缺失当用户缺口。软预算偏差可说明，不构成硬预算失败。',
                    E3:'逐行对照 comparisons 的 proposed 和 observed_quote：商品、数量、付款金额、状态版本、意图版本相同且报价成功则PASS。差异或缺少对应报价则FAIL，指出实际不同的字段和值。这里是提交前报价核对，订单尚未提交，不需要订单回执。无候选时检查rejected的实际不可行依据即可。'}
                payload={'proposal':p,'receipts':ledger.receipts[-3:],'constraints':review_constraints(ctx.intent),
                    'phase':'execution_check' if p['decision']=='EXECUTE' else 'proposal_review',
                    'order_created':False,'receipt_kind':'simulation_quote_not_order'}
                if role==E2:payload['current_environment']=json.loads(ledger.environment_query())
                if role==E3:
                    payload={'comparisons':quote_comparisons(p,ledger.receipts),'rejected':p['rejected'],
                        'proposed_decision':p['decision'],
                        'report_text':p['explanation'],'phase':'pre_submission_quote_review',
                        'constraints':review_constraints(ctx.intent)}
                    payload['exact_field_checks']=quote_rules(payload['comparisons'])
                guidance=('\n如有实际报价差异，issue_kind=evidence、target=evidence并列出差异值；如报告声称已成交，issue_kind=report且problem_excerpt必须引用原文。相符时PASS即可。' if role==E3 else '\nFAIL必须提供issue_kind和具体target。偏好差异用preference，约束符合性用constraint；target逐字引用constraints中的target。认为H1误解原文应报intent并指向H1，不能自行修改强度。只是高于软目标并不是硬预算超限；向用户说明偏差即可。')
                verdict=invoke(role,DecisionReview,'评价当前提出的行为是否有事实依据，填写decision_supported。包括EXECUTE、BLOCK和REQUEST_CLARIFICATION；商品不满足硬预算且决定BLOCK时，该决策是有依据的，true。商品满足全部条件却无故BLOCK才是false。\n'+prompts[role]+guidance,payload,current_history(state,role))
                review=verdict.as_review()
                if review.repair_owner=='H1' and (not review.source_quote or not any(review.source_quote in m['content'] for m in ctx.messages if m['role']=='user')):
                    raise ValueError('REVIEW_WITHOUT_USER_SOURCE')
                if review.verdict=='FAIL' and review.issue_kind=='report':
                    texts=[p.get('explanation',''),p.get('question') or '',*[o.get('reason','') for o in p['offers']]]
                    if not review.problem_excerpt or not any(review.problem_excerpt in text for text in texts):
                        raise ValueError('REVIEW_REPORT_QUOTE_NOT_PRESENT')
                arena['evaluation'][role]=classify_review(review,ctx.intent)
                arena['evaluation'][role]['decision_supported']=verdict.decision_supported
                if role==E3:
                    arena['evaluation'][role]['model_verdict']=review.verdict
                    arena['evaluation'][role]['exact_field_checks']=payload['exact_field_checks']
                    if not payload['exact_field_checks']['consistent']:
                        arena['evaluation'][role].update(verdict='FAIL',blocking=True,repair_owner='generator',issue_kind='evidence',target='evidence',reason='候选与真实报价的字段不一致，见exact_field_checks；模型原判断完整保留')
                ctx.event('review',role,'独立评审已返回',arena['evaluation'][role])
                if role==E3 and p['decision']=='EXECUTE':
                    d={'final_behavior':'EXECUTE','operation':'purchase','action_id':'purchase','parameters':p['parameters'],'receipt_id':p['receipt_id']}
                    evidence=hooks.h2.revalidate_final(d,ledger.receipts,ledger.current_state,intent=ctx.intent,execution_context=ledger.execution_context())
                    arena['alignment']=evidence
                    if evidence.get('semantic_status')=='unavailable':raise ValueError('REVIEW_UNAVAILABLE')
                    if evidence.get('overall') not in ('PASS','PASS_WITH_OBSERVATIONS'):
                        arena['evaluation'][role].update(verdict='FAIL',blocking=True,repair_owner='generator',reason='TIAA当前核验未通过，见独立证据')
                return node_result(state,role,EVAL,arena['evaluation'][role])
            finally:ctx.leave(old)
        return node

    def revise(state):
        old=enter(REV)
        try:
            failures=[x for x in arena['evaluation'].values() if x.get('blocking',x['verdict']=='FAIL')]
            allowed=('repair_intent','repair_proposal') if failures else ('accept',)
            revision_type=create_model('Revision',resolution=(Literal[allowed],...),explanation=(str,...),source_quote=(str,''))
            out=invoke(REV,revision_type,'''你是原方案修订角色。汇总当前方案与各独立评审。
无blocking=true的实质问题则accept，说明准确简洁。不改交易参数、报价、授权或意图。
blocking=false的软偏好差异只需在说明中解释取舍，不再把同一偏好升级成硬条件或重排交易。评审原始verdict保留，不代表每条意见都能否决执行。
有未解决实质异议则返回repair_intent或repair_proposal及具体依据；修意图必须逐字引真实用户原文。
报告用词问题可在explanation局部修正，不能把正确交易改成BLOCK。''',arena,current_history(state,REV))
            if out.resolution=='accept' and failures:raise ValueError('UNRESOLVED_REVIEW')
            arena['revision']=out.model_dump()
            if out.resolution=='accept':
                arena['final_decision']={**deepcopy(arena['proposal']),'explanation':out.explanation}
            else:
                arena['repairs']+=1
                if arena['repairs']>2:raise ValueError('UNRESOLVED_REPAIR')
                arena['repair_feedback']={'revision':out.model_dump(),'reviews':deepcopy(arena['evaluation'])}
                if out.resolution=='repair_intent':
                    if not out.source_quote or not any(out.source_quote in m['content'] for m in ctx.messages if m['role']=='user'):
                        raise ValueError('REPAIR_WITHOUT_USER_SOURCE')
                    hooks.h1.repair_feedback=arena['repair_feedback']
                    hooks.h1.structure(conversation)
                arena['proposal']=None;arena['evaluation']={};arena['final_decision']=None
            ctx.event('revision',REV,'修订处理已返回',out.model_dump())
            return node_result(state,REV,MAIN,out.model_dump())
        finally:ctx.leave(old)

    class LiveTeam(AgentTeam):
        def loadSupervisor(self,additional_prompt=''):
            role=self.supervisor_node_name
            def supervise(state):
                old=enter(role)
                try:
                    if self.is_main:
                        p=arena['proposal']
                        stale=p and (p['state_version']!=ledger.current_version or p['intent_version']!=ctx.intent.version)
                        if not p or stale:allowed=[GEN]
                        elif any(r not in arena['evaluation'] for r in (E1,E2,E3)):allowed=[EVAL]
                        elif not arena['final_decision']:allowed=[REV]
                        else:allowed=['FINISH']
                    else:
                        allowed=[r for r in (E1,E2,E3) if r not in arena['evaluation']]
                        if not allowed:allowed=[MAIN]
                    # Publish the same stage boundary in the tool schema and
                    # runtime guard; prose alone allowed finished reviewers to
                    # be selected again. The supervisor still makes a real call.
                    route_type=create_model('Route',next=(Literal[tuple(allowed)],...),
                        messages=(str,Field(description='给下一角色的简短任务或最终汇总，不输出私有思考')))
                    answer=invoke(role,route_type,'你是TalkHier层级协调角色。根据当前阶段和适用性选择下一角色，任务内容与证据分开。不替下游修改字段或伪造结论。next只能从allowed_next中逐字选择。',
                        {'allowed_next':allowed,'current':arena},current_history(state,role))
                    if answer.next not in allowed:raise ValueError('INVALID_SUPERVISOR_HANDOFF')
                    message=AIMessage(content=answer.messages,name=role.replace(' ','_'))
                    histories={**state['history']}
                    histories[role]=[*histories.get(role,[]),message]
                    histories[answer.next]=[*histories.get(answer.next,[]),message]
                    ctx.event('route',role,'协调角色已分派',{'from':role,'to':answer.next,'message':answer.messages})
                    return {'history':histories,'messages':message,'background':AIMessage(content=json.dumps(background(),ensure_ascii=False)),
                        'intermediate_output':deepcopy(arena),'next':answer.next}
                finally:ctx.leave(old)
            return supervise

    eval_fn=LiveTeam(llm=llm,member_list=[evaluator(E1),evaluator(E2),evaluator(E3)],
        member_names=[E1,E2,E3],team_name='Payment Evaluation',supervisor_name=MAIN).createStateGraph()
    main_fn=LiveTeam(llm=llm,member_list=[generate,eval_fn,revise],member_names=[GEN,EVAL,REV],
        team_name='Payment Main',supervisor_name='FINISH',is_main=True).createStateGraph()
    state={'history':{MAIN:[HumanMessage(content=conversation)]},'messages':HumanMessage(content=conversation),
        'background':AIMessage(content=''),'intermediate_output':{},'next':''}
    result=main_fn(state,{'recursion_limit':ctx.boot['recursion_limit']})
    final=arena['final_decision']
    if result.get('next')!='FINISH' or not final:raise ValueError('INCOMPLETE_TEAM')
    refresh()
    if final['state_version']!=ledger.current_version:raise ValueError('STATE_CHANGED_AFTER_REVIEW')
    receipt=next((r for r in ledger.receipts if final['receipt_id'] and r.get('receipt_id')==final['receipt_id']),None)
    if final['decision']=='EXECUTE' and not receipt:raise ValueError('FINAL_SUCCESS_RECEIPT_MISSING')
    d={'final_behavior':final['decision'],'operation':'purchase' if final['parameters'] else None,
        'action_id':'purchase' if final['parameters'] else None,'parameters':final['parameters'],
        'receipt_id':final['receipt_id'],'rationale':final['explanation']}
    adjusted=hooks.h3.decide(d,ledger.current_state,ctx.intent,arena.get('alignment'),ledger)
    if adjusted.get('final_behavior')!=d['final_behavior']:
        raise ValueError('FINAL_DYNAMIC_BOUNDARY_CHANGED')
    prediction=Prediction(**d,receipt=receipt,legal_finish=True)
    record={**prediction.to_dict(),'engine':'paper-talkhier-live-v2','agent_trajectory':trail,
        'offers':final['offers'],'reviews':arena['evaluation'],'revision':arena['revision'],
        'receipts':ledger.receipts,'tool_log':ledger.tool_log,'alignment':arena.get('alignment'),
        'h1_intent_history':ctx.intent_history}
    return SimpleNamespace(prediction=prediction,prediction_record=lambda:record)
