"""Live IO for the paper's FieldReading/Commitment and evidence update contract.

Unknown identity is a planning state, not permission to invent a selection.
Only the H1 adapter maintains the current version; other roles propose changes.
"""
import json
import re
from copy import deepcopy
from typing import Literal, get_args
from pydantic import BaseModel, ConfigDict, Field, create_model
from langchain_core.messages import SystemMessage, HumanMessage
from payment.h1.schema import FieldReading, Commitment, StructuredIntent, finalize_intent, compute_diff


class TextReading(BaseModel):
    value: str | None = None
    status: Literal['explicit','inferred','missing','ambiguous','invalid','unsupported'] = 'missing'
    hardness: Literal['hard','soft'] = 'soft'
    evidence: str = ''


class QuantityReading(BaseModel):
    value: int | None = Field(default=None, ge=1, le=10)
    status: Literal['explicit','inferred','missing','ambiguous','invalid','unsupported'] = 'missing'
    hardness: Literal['hard','soft'] = 'soft'
    evidence: str = ''


class ProductCategoryReading(TextReading):
    value: str | None = Field(default=None,description='商品的自然语言品类，如耳机、键盘、保温杯。不是预算/费用类型，不能填写total_price等Commitment分类；确实未说明且不能推断时null。')


class LiveIntentDraft(BaseModel):
    model_config=ConfigDict(populate_by_name=True)
    product_name: TextReading = Field(description='具体商品型号，如听山 Air；只有耳机/杯子等品类时value=null，品类放category。用户最新确认具体商品时更新此字段，不沿用旧泛品类。')
    category: ProductCategoryReading = Field(alias='product_category',description='商品品类的原文解释，与commitments[].category完全分开。')
    color: TextReading
    quantity: QuantityReading
    execution_authorization: Literal['granted','withheld','unclear']
    authorization_evidence: str
    selection_mode: Literal['open','selected','delegated','ambiguous']
    selection_evidence: str
    confirmed_offer_id: str | None = Field(default=None, description='仅指向上轮展示且用户此次确认的 offer_id，否则 null')
    confirmation_evidence: str = ''
    commitments: list[Commitment] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    question: str | None = None


PROMPT = '''你是 TAPay SIRA，唯一维护当前用户意图。按原 FieldReading/Commitment 语义解释真实会话。
把商品种类、已指定型号、颜色、数量的来源、状态、强度分别保留。未说型号/颜色不等于真实歧义，不猜一个型号；可查询并形成候选。
product_name只放具体型号，不放耳机等品类。用户后来确认某个候选时，更新型号/颜色等选型字段，保留仍然有效的预算与其他要求。
“买个耳机，230左右”是开放的耳机购买需求，数量1，价格为软目标；不是询问用户是否需要推荐。仅在用户意思冲突或确需用户信息时 question 才非空。
硬度按用户措辞，不按字段名。必须/不超过是硬约束；大约/左右/最好是软偏好。commitments 保留四档 inviolable/strong_preference/aspirational_target/weak_preference。
预算 category=total_price，unit=CNY，方向 max/min/exact/target；around用软 target，硬上下界分别记录，不自创上下浮动范围。每项只记录一次。
型号颜色等保留在各字段，不重复写为 other。额外真实要求不得遗漏。报价、库存、商品介绍不属于用户要求。
selection_mode：只说品类且未选具体商品是 open；明确选定型号/某个候选是 selected；明确允许你代选是 delegated；真实指代有多个解释是 ambiguous。
允许查询/推荐不等于允许付款。execution_authorization：明确要求购买、下单或条件内代办可granted；仅推荐/先别买/等确认为withheld；不明为unclear。所有授权引用必须来自真实user消息。
“符合条件才下单”是保留所列条件的授权；“等我确认后才下单”则保留了用户的后续确认。二者须区分，不得丢掉条件。
用户最新修改只替代对应要求，保留其他有效条件。若用户确认先前实际展示的候选，confirmed_offer_id 引用该候选，confirmation_evidence逐字引用户本轮确认语；若多个候选且“是的”没选哪个，不能伪造选中。
确认商品不把原软预算/偏好改成硬条件。可解析“是的/就第二个”的上下文，但不能把助手内容本身当授权。
返回的evidence必须是某条真实user消息中的连续原文。审阅异议是待核对意见，不是用户新要求。
'''


def norm(value):
    return re.sub(r'\s+', '', str(value or '')).casefold()


def repair_citations(llm,draft,users):
    slots=[(f'{k}.evidence',getattr(draft,k),'evidence',getattr(draft,k).value is not None)
           for k in ('product_name','category','color','quantity')]
    slots += [(f'commitments[{i}].evidence',c,'evidence',True) for i,c in enumerate(draft.commitments)]
    slots += [('authorization_evidence',draft,'authorization_evidence',draft.execution_authorization=='granted'),
              ('selection_evidence',draft,'selection_evidence',draft.selection_mode=='delegated'),
              ('confirmation_evidence',draft,'confirmation_evidence',bool(draft.confirmed_offer_id))]
    invalid=[(target,obj,key) for target,obj,key,required in slots
             if (getattr(obj,key) and not any(getattr(obj,key) in u for u in users)) or (required and not getattr(obj,key))]
    if not invalid:return
    # The model chooses source-message indices; the program copies the selected
    # original text verbatim. It cannot add punctuation or merge user turns.
    keys={target.replace('.','_').replace('[','_').replace(']',''):(target,obj,key)
          for target,obj,key in invalid}
    patch_type=create_model('SourcePatch',**{
        slot:(Literal[tuple(range(len(users)))],Field(description=f'支持 {target} 对应字段值的真实用户消息下标'))
        for slot,(target,obj,key) in keys.items()})
    patch=llm.with_structured_output(patch_type).invoke([SystemMessage(content='只修复H1来源指向。为每个字段选择支持其当前值的真实用户消息下标。接口不能改商品、数量、预算、强度、授权或生成新的引文；后端将逐字复制选中的原消息。最新颜色修改应引用最新用户消息，保留的型号引用原先消息。'),HumanMessage(content=json.dumps({'user_messages':list(enumerate(users)),'current_draft':draft.model_dump(),'source_slots':{slot:target for slot,(target,_,_) in keys.items()}},ensure_ascii=False))])
    for slot,index in patch.model_dump().items():
        _,obj,key=keys[slot];setattr(obj,key,users[index])


def matches_product_name(value,product,allow_category=False):
    """Exact public identity aliases, without editing the source reading.

    Color and identifier qualifiers do not denote another model. Pro/model
    suffixes remain significant; no fuzzy matching or partial-name truncation.
    """
    attrs=product.get('attributes',product);name=attrs['name'];color=attrs['color'];sku=product['item_id']
    bases={name,color+name,name+color}
    bases |= {base+attrs['category'] for base in tuple(bases)}
    aliases={norm(sku)}|{norm(base+suffix) for base in bases for suffix in ('',f'（{sku}）',f'({sku})')}
    if allow_category:aliases.add(norm(attrs['category']))
    return norm(value) in aliases


def repair_product_category(llm,draft,users):
    reserved=set(get_args(Commitment.model_fields['category'].annotation))
    if draft.category.value not in reserved:return
    schema=create_model('ProductCategoryPatch',value=(str|None,...),evidence=(str,...))
    patch=llm.with_structured_output(schema).invoke([SystemMessage(content='你仍是H1，只修复被误填成付款约束类型的商品品类。按真实用户原文解释商品是什么品类，如耳机、键盘、保温杯；不能填total_price等技术分类。确实无法判断时null。evidence逐字引用一条用户消息。不得改变商品型号、数量、价格、授权、软硬性质或其余字段。'),HumanMessage(content=json.dumps({'user_messages':users,'product_name':draft.product_name.model_dump(),'invalid_product_category':draft.category.model_dump()},ensure_ascii=False))])
    if patch.value in reserved or (patch.value is not None and (not patch.evidence or not any(patch.evidence in u for u in users))):
        raise ValueError('H1_INVALID_PRODUCT_CATEGORY')
    draft.category.value=patch.value;draft.category.evidence=patch.evidence
    draft.category.status='inferred' if patch.value is not None else 'missing'


def update_authorization(llm,draft,users):
    if len(users)<2:return
    schema=create_model('AuthorizationUpdate',value=(Literal['granted','withheld','unclear'],...),
        source_message_index=(Literal[tuple(range(len(users)))+(None,)],...))
    patch=llm.with_structured_output(schema).invoke([SystemMessage(content='你仍是H1，只核对当前下单授权。按真实用户消息的先后顺序处理：后来的明确允许/撤回替代之前的对应权限；仅修改颜色或询问不自动授予下单权限。没有更改权限则保留仍有效的先前授权。返回当前权限与直接支持它的用户消息下标；无法确认时unclear。此接口不能改商品选择、预算、数量或任何软硬要求。'),HumanMessage(content=json.dumps({'user_messages':list(enumerate(users)),'draft_authorization':draft.execution_authorization,'draft_source':draft.authorization_evidence},ensure_ascii=False))])
    if patch.value=='granted' and patch.source_message_index is None:raise ValueError('H1_AUTHORIZATION_WITHOUT_SOURCE')
    draft.execution_authorization=patch.value
    draft.authorization_evidence='' if patch.source_message_index is None else users[patch.source_message_index]



def build_intent(llm, messages, prior=None, offers=None, feedback=None):
    users=[m['content'] for m in messages if m['role']=='user']
    draft=llm.with_structured_output(LiveIntentDraft).invoke([
        SystemMessage(content=PROMPT), HumanMessage(content=json.dumps({
            # IDs and display timestamps are transport metadata, never shopper
            # utterances. Preserve exact text and roles, with no text rewriting.
            'conversation':[{'role':m['role'],'content':m['content']} for m in messages],
            'previous_intent':prior,'previous_offers':offers or [],
            'review_feedback_not_user_instruction':feedback},ensure_ascii=False))])
    repair_product_category(llm,draft,users)
    update_authorization(llm,draft,users)
    repair_citations(llm,draft,users)
    for c in draft.commitments:
        # Wire-format synonyms only: no change to amount, strength or bounds.
        if c.category=='total_price':
            if c.unit in ('元','人民币','RMB'):c.unit='CNY'
            if c.direction=='around' and c.level!='inviolable':c.direction='target'
    for r in [draft.product_name,draft.category,draft.color,draft.quantity]:
        if r.evidence and not any(r.evidence in s for s in users):raise ValueError('H1_UNGROUNDED_SOURCE')
        if r.value is not None and not r.evidence:raise ValueError('H1_VALUE_WITHOUT_SOURCE')
    for c in draft.commitments:
        if not c.evidence or not any(c.evidence in s for s in users):raise ValueError('H1_UNGROUNDED_COMMITMENT')
        if c.category=='total_price' and (c.unit!='CNY' or c.direction not in ('max','min','exact','target')):
            raise ValueError('H1_INVALID_BUDGET_REPRESENTATION')
        if c.level=='inviolable' and c.direction=='target':raise ValueError('H1_HARD_TARGET_WITHOUT_BOUND')
    if draft.authorization_evidence and not any(draft.authorization_evidence in s for s in users):
        raise ValueError('H1_UNGROUNDED_AUTHORIZATION')
    if draft.execution_authorization=='granted' and not draft.authorization_evidence:
        raise ValueError('H1_AUTHORIZATION_WITHOUT_SOURCE')
    if draft.selection_mode=='delegated' and (not draft.selection_evidence or not any(draft.selection_evidence in s for s in users)):
        raise ValueError('H1_DELEGATION_WITHOUT_SOURCE')
    fields={k:FieldReading(**getattr(draft,k).model_dump()) for k in ('product_name','category','color','quantity')}
    fields.update(operation=FieldReading(value='purchase',status='inferred',evidence=draft.category.evidence or draft.product_name.evidence),
        item_id=FieldReading(status='missing'),
        account_id=FieldReading(value='sandbox-wallet',status='inferred'),
        execution_authorization=FieldReading(value=draft.execution_authorization,status='explicit' if draft.authorization_evidence else 'missing',hardness='hard',evidence=draft.authorization_evidence))
    evidence={'selection':{'mode':draft.selection_mode,'source':draft.selection_evidence}}
    if draft.confirmed_offer_id:
        offer=next((o for o in offers or [] if o['offer_id']==draft.confirmed_offer_id),None)
        if not offer or not draft.confirmation_evidence or draft.confirmation_evidence not in users[-1]:
            raise ValueError('H1_INVALID_OFFER_CONFIRMATION')
        # A modified color/model no longer confirms the old product. Let H1
        # correct only the binding pointer; every interpreted value stays fixed.
        bare=len(offers or [])>1 and draft.confirmation_evidence.strip(' 。！!') in ('是的','好','好的','可以','嗯','确认')
        compatible=[o for o in offers or []
                    if (not draft.product_name.value or matches_product_name(draft.product_name.value,o,allow_category=True))
                    and (not draft.color.value or norm(draft.color.value)==norm(o['color']))]
        if bare:compatible=[]
        if offer not in compatible:
            schema=create_model('OfferBindingPatch',confirmed_offer_id=(Literal[tuple([o['offer_id'] for o in compatible]+[None])],...),confirmation_evidence=(str,...))
            patch=llm.with_structured_output(schema).invoke([SystemMessage(content='只修正候选确认指针。用户修改型号/颜色后不能确认冲突的旧方案；多个方案时泛泛“是的”不能任选。当前没有合适的已展示方案时返回null，后续重新查询。其他意图字段不变。'),HumanMessage(content=json.dumps({'latest_user':users[-1],'current_fields':{k:v.model_dump() for k,v in fields.items()},'compatible_offers':compatible},ensure_ascii=False))])
            draft.confirmed_offer_id=patch.confirmed_offer_id;draft.confirmation_evidence=patch.confirmation_evidence
            offer=next((o for o in compatible if o['offer_id']==patch.confirmed_offer_id),None)
            if offer and (not patch.confirmation_evidence or patch.confirmation_evidence not in users[-1]):raise ValueError('H1_INVALID_OFFER_CONFIRMATION')
        if offer:
            fields['item_id']=FieldReading(value=offer['item_id'],status='inferred',hardness=draft.product_name.hardness,
                evidence=draft.confirmation_evidence,confirmed=True)
            evidence['confirmed_offer']={**deepcopy(offer),'user_quote':draft.confirmation_evidence}
            evidence['selection']['mode']='selected'
    intent=StructuredIntent(scheme='purchase',fields=fields,commitments=draft.commitments,
        conflicts=draft.conflicts,clarification_question=draft.question,grounding_evidence=evidence)
    previous=StructuredIntent.model_validate(prior) if prior else None
    intent.version=previous.version+1 if previous else 1
    intent.supersedes=previous.version if previous else None
    intent=finalize_intent(intent,previous=previous,catalog_required={'purchase':['item_id','quantity','account_id']},
        conversation='\n'.join(users))
    if previous:intent.diff_from_previous=compute_diff(previous,intent)
    defer_identity(intent)
    return intent


def defer_identity(intent):
    if intent.field('item_id').value is None:
        intent.pending_resolution=list(dict.fromkeys([*intent.pending_resolution,'item_id']))
        intent.missing_required=[x for x in intent.missing_required if x!='item_id']
        # The execution gate still requires a bound item; only planning is open.
        if not intent.missing_required and not intent.conflicts and not intent.unresolved_hard_constraints:
            intent.requires_clarification=False
            intent.clarification_question=None
    return intent


def ground_observed(intent, products, state_version, chosen_id=None):
    """Public identity evidence; candidate IDs never imply user confirmation."""
    current=intent.model_copy(deep=True)
    selection=current.grounding_evidence.get('selection',{})
    explicit=current.field('item_id').value
    name=current.field('product_name');color=current.field('color');category=current.field('category')
    if explicit:
        matches=[p for p in products if p['item_id']==explicit]
    elif name.value:
        matches=[p for p in products if matches_product_name(name.value,p)]
        if color.value:
            # Resolve a user's selected variant; this does not turn a soft
            # color preference into a hard condition on other proposals.
            matches=[p for p in matches if norm(p['attributes']['color'])==norm(color.value)]
    else:matches=[]
    if selection.get('mode')=='delegated' and chosen_id:
        matches=[p for p in products if p['item_id']==chosen_id]
        for field,key in [(name,'name'),(color,'color'),(category,'category')]:
            if field.value and field.hardness=='hard':
                matches=[p for p in matches if matches_product_name(field.value,p,allow_category=True)] if key=='name' else [p for p in matches if norm(p['attributes'][key])==norm(field.value)]
    if len(matches)!=1:return None
    p=matches[0]
    old=current.field('item_id')
    source=old.evidence or name.evidence or selection.get('source','')
    if not source:return None
    current.fields['item_id']=FieldReading(value=p['item_id'],status='inferred',hardness=old.hardness if explicit else name.hardness,
        evidence=source,confirmed=old.confirmed)
    current.grounding_evidence['item_id']={'source':'observed_shop_tool','state_version':state_version,
        'product':deepcopy(p),'user_quote':source,'selection_mode':selection.get('mode')}
    current.pending_resolution=[x for x in current.pending_resolution if x!='item_id']
    current.missing_required=[x for x in current.missing_required if x!='item_id']
    if not current.missing_required and not current.conflicts and not current.unresolved_hard_constraints:
        current.requires_clarification=False;current.clarification_question=None
    if current.compact()==intent.compact():return None
    current.version=intent.version+1;current.supersedes=intent.version
    current.diff_from_previous=compute_diff(intent,current)
    return current
