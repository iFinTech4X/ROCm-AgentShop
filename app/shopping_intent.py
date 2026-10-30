"""Model interprets source language; public catalog resolves exact identities."""
import json,re
from typing import Literal
from pydantic import BaseModel,Field
from langchain_core.messages import SystemMessage,HumanMessage

class BudgetReading(BaseModel):
    yuan: float=Field(ge=0)
    kind: Literal['hard','soft']
    quote: str

class ExtraRequirement(BaseModel):
    text: str
    quote: str
    hard: bool
    attribute: Literal['name','color','category','model','description','other']=Field(description='对应公开商品属性，无法由商品属性表示时用other')
    expected: str=Field(description='要求的商品属性值。保留真实要求，不能捏造')

class ShoppingReading(BaseModel):
    product_name: str | None=Field(description='目录中的完整商品名称，保留Pro等后缀。未知时为空')
    color: str | None=Field(description='用户指定的颜色。用户未指定可为空')
    identity_quote: str=Field(description='用户关于商品型号颜色的原文')
    quantity: int | None=Field(ge=1,le=10)
    quantity_quote: str
    authorization: Literal['granted','withheld','unclear']
    authorization_quote: str
    allows_choice: bool=Field(description='用户明确允许系统代选时为true，不可从缺少选择推得')
    choice_quote: str
    preferred_item_id: str | None=Field(description='仅在允许代选且有多个合法商品时使用，否则为空')
    budgets: list[BudgetReading]
    additional_requirements: list[ExtraRequirement]
    question: str | None

class RequirementUpdate(BaseModel):
    index: int
    superseded: bool
    later_user_quote: str

class RequirementUpdates(BaseModel):
    updates: list[RequirementUpdate]

def revised_requirements(llm,reading,catalog,user_messages):
    selected=next((x for x in catalog if x['item_id']==resolve_identity(reading,catalog)),None)
    if not selected or len(user_messages)<2:return reading.additional_requirements,[]
    normalize=lambda v:re.sub(r'\s+','',str(v)).casefold()
    conflicts=[i for i,r in enumerate(reading.additional_requirements) if r.attribute in ('name','color','model') and normalize(selected.get(r.attribute))!=normalize(r.expected)]
    if not conflicts:return reading.additional_requirements,[]
    review=llm.with_structured_output(RequirementUpdates).invoke([
        SystemMessage(content='你仍是SIRA，仅复核下面冲突的附加要求是否被更晚的真实用户修改替代。不得修改商品、预算、数量或权限。每个index都返回一次。只有用户后文明确替代了这项要求才superseded=true，并逐字引用later_user_quote；否则false。改颜色不删除预算或其他要求。'),
        HumanMessage(content=json.dumps({'user_messages':user_messages,'current_product':selected,'requirements':[{'index':i,**reading.additional_requirements[i].model_dump()} for i in conflicts]},ensure_ascii=False))])
    if sorted(x.index for x in review.updates)!=conflicts:raise ValueError('INCOMPLETE_REQUIREMENT_REVIEW')
    removed=set()
    for update in review.updates:
        if not update.superseded:continue
        old=reading.additional_requirements[update.index].quote
        old_positions=[i for i,m in enumerate(user_messages) if old and old in m]
        new_positions=[i for i,m in enumerate(user_messages) if update.later_user_quote and update.later_user_quote in m]
        if not old_positions or not new_positions or max(new_positions)<=max(old_positions):raise ValueError('UNSUPPORTED_REQUIREMENT_SUPERSESSION')
        removed.add(update.index)
    return [r for i,r in enumerate(reading.additional_requirements) if i not in removed],review.model_dump()['updates']

def resolve_identity(reading,catalog):
    norm=lambda s:re.sub(r'\s+','',str(s)).casefold()
    choices=[x for x in catalog if reading.product_name and norm(x['name'])==norm(reading.product_name)
             and (not reading.color or norm(x['color'])==norm(reading.color))]
    if len(choices)==1:return choices[0]['item_id']
    if reading.allows_choice and reading.choice_quote:
        return next((x['item_id'] for x in choices if x['item_id']==reading.preferred_item_id),None)
    return None

def structure_shopping(llm,conversation,catalog,catalog_required,feedback=None,user_messages=None):
    from payment.h1.schema import StructuredIntent,FieldReading,Commitment,finalize_intent
    reading=llm.with_structured_output(ShoppingReading).invoke([
        SystemMessage(content='你是SIRA购物意图角色。理解最新用户原文，输出ShoppingReading。不要直接猜商品编号：准确提取商品名称与颜色，程序会用公开目录解析。缺失、歧义、仅推荐或保留选择权要询问，不可自授执行权限。只有真实用户的购买/下单要求才能授权。最近修改只替代对应字段，保留其余条件。硬预算不超过，软预算大约/希望；若另有绝对上限，两条都保留。所有quote来自当前用户会话原文。名称型号颜色之外的额外限制不能丢失。只返回当前需求，中文问题。'),
        HumanMessage(content=json.dumps({'conversation':conversation,'catalog':catalog,'review_feedback_not_user_instruction':feedback},ensure_ascii=False))])
    # Catalog binding is deterministic identity resolution, not a rewrite of
    # user modality, authority or the model's semantic interpretation.
    item_id=resolve_identity(reading,catalog)
    fields={
        'operation':FieldReading(value='purchase',status='inferred',hardness='hard',evidence=reading.identity_quote),
        'item_id':FieldReading(value=item_id,status='inferred' if item_id else 'ambiguous',hardness='hard',evidence=reading.identity_quote),
        'quantity':FieldReading(value=reading.quantity,status='inferred' if reading.quantity else 'missing',hardness='hard',evidence=reading.quantity_quote),
        'account_id':FieldReading(value='sandbox-wallet',status='inferred',hardness='hard',evidence=''),
        'execution_authorization':FieldReading(value=reading.authorization,status='explicit',hardness='hard',evidence=reading.authorization_quote)}
    commitments=[Commitment(rule='含运费付款预算',category='total_price',level='inviolable' if b.kind=='hard' else 'strong_preference',value=b.yuan,unit='CNY',direction='max',evidence=b.quote) for b in reading.budgets]
    selected=next((x for x in catalog if x['item_id']==item_id),None)
    requirements,revision_review=revised_requirements(llm,reading,catalog,user_messages or [])
    for r in requirements:
        observed=(selected or {}).get(r.attribute)
        grounded=(r.attribute!='other' and observed is not None and
                  re.sub(r'\s+','',str(observed)).casefold()==re.sub(r'\s+','',r.expected).casefold())
        commitments.append(Commitment(rule=r.text,category='product_identity' if grounded else 'other',level='inviolable' if r.hard else 'weak_preference',evidence=r.quote,
                                      note=f'公开属性 {r.attribute}={observed}，要求={r.expected}，匹配={grounded}'))
    intent=StructuredIntent(scheme='purchase',fields=fields,commitments=commitments,clarification_question=reading.question)
    result=finalize_intent(intent,catalog_required=catalog_required,conversation=conversation)
    if result.unresolved_hard_constraints:result.clarification_question='还有条件无法由当前商品信息核实：'+ '；'.join(result.unresolved_hard_constraints)+'。请说明可接受的范围。'
    if not item_id and not result.clarification_question:result.clarification_question='请确认具体商品型号和颜色；也可以明确授权我在这些选项中代选。'
    return result,{**reading.model_dump(),'requirement_revision_review':revision_review}
