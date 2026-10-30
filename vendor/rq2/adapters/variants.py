"""Explicit experimental replacements; the original src tree stays frozen."""
import copy,hashlib,inspect,json,textwrap
from langchain_core.messages import HumanMessage,SystemMessage
from langchain_core.runnables import RunnableLambda
from payment.h1.schema import finalize_intent
from multiagent.llm import StructuredResponseError

def canonical_json(value):return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'))
def relation(value):
    return {'max':'upper_bound','le':'upper_bound','lt':'upper_bound','min':'lower_bound','ge':'lower_bound','gt':'lower_bound','exact':'eq','ne':'not_equal','in':'membership'}.get(value,value if value in ('eq','upper_bound','lower_bound','not_equal','membership','contains') else 'unspecified')

class FixedPolicy:
    def __init__(self,document):self.document=document
    def lookup(self,name,op):
        cat=self.document['alias_map'].get(name,'other');rel=relation(op)
        key=cat+'|'+rel
        if key in self.document['selected_level_by_key']:return self.document['selected_level_by_key'][key]
        for rule in self.document['fallback_rules']:
            if cat in rule['categories'] and rel in rule['relations']:return rule['level']
        return self.document['default_fallback_level']
    def project(self,intent,catalog_required,clock):
        value=intent.model_copy(deep=True)
        metadata={k:copy.deepcopy(getattr(value,k)) for k in ('version','supersedes','diff_from_previous')}
        # Field schemas encode their comparison semantics. Do not infer these
        # from modality words or copy adaptive field hardness.
        field_relations={'max_total_usd':'upper_bound','total_price':'upper_bound',
            'total_fee':'upper_bound','max_fee':'upper_bound','amount_limit':'upper_bound',
            'gas_cap':'upper_bound','slippage_cap':'upper_bound','slippage':'upper_bound',
            'min_output':'lower_bound','minimum_received':'lower_bound',
            'deadline_at':'upper_bound','time_window_seconds':'upper_bound'}
        for name,f in value.fields.items():
            level=self.lookup(name,field_relations.get(name,'eq'))
            f.hardness='hard' if level=='inviolable' else 'soft'
        for c in value.commitments:
            c.level=self.lookup(c.category,c.direction)
        # Rebuild published derived state rather than leaving adaptive strength
        # in the plain lists or the unresolved-hard flags.
        value.requires_clarification=False;value.requires_block=False
        value.clarification_question=None
        value.hard_constraints=[c.rule for c in value.commitments if c.level=='inviolable']
        value.soft_preferences=[c.rule for c in value.commitments if c.level!='inviolable']
        value=finalize_intent(value,catalog_required=catalog_required,conversation='',conversation_start_at=clock)
        for k,v in metadata.items():setattr(value,k,v)
        for name,f in value.fields.items():f.confirmed=intent.field(name).confirmed
        return value

def fixed_h1(base,policy):
    class FixedH1(base):
        def _project(self,value):
            if value is None:return None
            value=policy.project(value,self.catalog_required,self.environment_snapshot.get('conversation_start_at'))
            self._current=value
            return value
        def _call(self,*a,**kw):return self._project(super()._call(*a,**kw))
        def structure(self,*a,**kw):return self._project(super().structure(*a,**kw))
        def on_user_event(self,*a,**kw):return self._project(super().on_user_event(*a,**kw))
        def on_tool_evidence(self,*a,**kw):return self._project(super().on_tool_evidence(*a,**kw))
        def on_authorization_evidence(self,*a,**kw):return self._project(super().on_authorization_evidence(*a,**kw))
    return FixedH1

def instant_h3(base):
    class InstantH3(base):
        def __init__(self):super().__init__(risk_enter=.6,risk_exit=.6,min_hold_events=1)
        def _update_zone(self,current_facts,chain_exhausted=False):
            self.event_count+=1
            risk=(current_facts or {}).get('risk_score')
            if risk is None:return None
            old=self.zone;self.zone='FORBIDDEN' if risk>=self.risk_enter else 'AUTO'
            return None if old==self.zone else old+'->'+self.zone
        def decide(self,decision,environment_snapshot,**kw):
            facts=(environment_snapshot or {}).get('facts',environment_snapshot or {})
            risk=facts.get('risk_score')
            if risk is not None:self.zone='FORBIDDEN' if risk>=self.risk_enter else 'AUTO'
            return super().decide(decision,environment_snapshot,**kw)
    return InstantH3

def proposal_key(proposal):
    return canonical_json({k:proposal.get(k) for k in ('action_id','operation','parameters')})

class AuthorContexts:
    def __init__(self):self.contexts={};self.checks=[]
    def register(self,proposal,name,messages):
        self.contexts[proposal_key(proposal)]={'author':name,'messages':copy.deepcopy(messages)}
    def require(self,proposal):
        record=self.contexts.get(proposal_key(proposal))
        if record is None:
            raise StructuredResponseError('PARSE_FAILURE','author-selfcheck: no captured author context for current candidate; independent fallback forbidden')
        self.checks.append({'proposal_sha256':hashlib.sha256(proposal_key(proposal).encode()).hexdigest(),'author':record['author'],'message_count':len(record['messages'])})
        return record

def author_factory(base,registry):
    """Capture the actual formatted member prompt, including its tool dialogue.

    The factory and returned node forward original arguments/results unchanged.
    No hidden supervisor/evaluator memory is added to a candidate author's view.
    """
    class CaptureFactory(base):
        def loadMember(self,name,member_tools,member_prompt,sup_name):
            import multiagent.agent_team as team
            from payment.prompts import GENERATOR,REVISOR
            captured=[];raw_artifact=[]
            def create(*a,**kw):
                modifier=kw['state_modifier']
                def remember(prompt):
                    captured[:]=copy.deepcopy(prompt.to_messages())
                    return prompt
                kw['state_modifier']=modifier|RunnableLambda(remember)
                agent=team.create_react_agent(*a,**kw)
                class ObservedAgent:
                    def stream(inner,*args,**kwargs):
                        for chunk in agent.stream(*args,**kwargs):
                            raw_artifact[:]=[team._parse_intermediate_output(chunk.get('intermediate_output'),None)]
                            yield chunk
                return ObservedAgent()
            namespace=dict(vars(team));namespace['create_react_agent']=create
            source=textwrap.dedent(inspect.getsource(base.loadMember))
            exec(compile(source,'<rq2-author-context-capture>','exec'),namespace)
            node=namespace['loadMember'](self,name,member_tools,member_prompt,sup_name)
            def invoke(state,config=None):
                captured.clear();raw_artifact.clear()
                output=node(state)
                artifact=output.get('intermediate_output')
                raw=raw_artifact[-1] if raw_artifact else None
                if name in (GENERATOR,REVISOR) and isinstance(raw,dict) and isinstance(raw.get('proposal'),dict) and isinstance(artifact,dict) and artifact.get('proposal')==raw['proposal']:
                    if not captured:raise RuntimeError('author prompt capture missing')
                    registry.register(artifact['proposal'],name,captured+[output['messages']])
                return output
            return invoke
    return CaptureFactory

def selfcheck_h2(base,registry):
    class AuthorCheck(base):
        def _semantic_layer(self,proposal,receipts,rule_report,intent=None):
            context=registry.require(proposal)
            original=self.llm
            class AuthorLLM:
                callbacks=getattr(original,'callbacks',None)
                def with_structured_output(inner,schema,**kw):
                    chain=original.with_structured_output(schema,**kw)
                    def adapt(messages):
                        system=messages[0].content.replace('You are an independent verifier.','You are reviewing the candidate you authored. Use your observed author context below.')
                        return [SystemMessage(content=system),*copy.deepcopy(context['messages']),messages[1]]
                    return RunnableLambda(adapt)|chain
            self.llm=AuthorLLM()
            try:return super()._semantic_layer(proposal,receipts,rule_report,intent)
            finally:self.llm=original
    return AuthorCheck
