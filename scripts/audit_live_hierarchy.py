"""Check observed business outcomes and evidence, not merely HTTP/model success."""
import json,sys
from pathlib import Path
root=Path(sys.argv[1]);rows=[]
expected={'generic_230':'clarification','yes_no_selection':'clarification','confirm_offer':'completed',
          'normal':'completed','soft_budget':'completed','hard_budget':'blocked',
          'revision_before':'clarification','revision_after':'completed','price_change':'blocked',
          'stock_change':'blocked','authorization_expiry':'blocked','cancel':'cancelled',
          'multiple':'completed','next_task_recommend':'clarification'}
for name,status in expected.items():
    p=root/(name+'.json')
    if not p.exists():rows.append(dict(name=name,passed=False,issues=['未观察到该路径']));continue
    d=json.loads(p.read_text(encoding='utf-8'));session=d['session'];run=session['runs'][-1];r=run.get('result') or {}
    orders=[o for o in session['orders'] if o['run_id']==run['id']];events=[e for e in d['events'] if e['run_id']==run['id']]
    issues=[]
    if run['status']!=status:issues.append(f"状态{run['status']}，期望{status}")
    if len(orders)!=(1 if status=='completed' else 0):issues.append('订单数量不符合预期')
    if name in ('generic_230','yes_no_selection','revision_before','next_task_recommend') and not r.get('offers'):issues.append('没有真实候选')
    if name=='generic_230':
        prices=[c for c in r.get('current_intent',{}).get('commitments',[]) if c['category']=='total_price']
        if not prices or any(c['level']=='inviolable' for c in prices):issues.append('左右预算未保留为软目标')
        if not any(e['kind']=='tool_end' and e['payload'].get('tool')=='shop_search_products' for e in events):issues.append('无真实搜索')
    if name in ('normal','soft_budget','revision_after') and orders:
        if orders[0]['item_id']!='A01' or orders[0]['quantity']!=1 or orders[0]['total_minor']!=29900:issues.append('成交商品/数量/金额错误')
    if name=='multiple' and orders:
        if orders[0]['item_id']!='D02' or orders[0]['quantity']!=2 or orders[0]['total_minor']!=18600:issues.append('多件商品/数量/运费错误')
    if name=='next_task_recommend':
        if r.get('current_intent',{}).get('fields',{}).get('execution_authorization',{}).get('value')=='granted':issues.append('下一任务错误继承下单授权')
    if name=='confirm_offer' and orders:
        prior=json.loads((root/'yes_no_selection.json').read_text(encoding='utf-8'))['session']['runs'][-1]['result']
        selected=prior['offers'][0]
        if any(orders[0][key]!=selected[key] for key in ('item_id','quantity','total_minor')):issues.append('订单不匹配本次确认的候选')
        prices=[c for c in r.get('current_intent',{}).get('commitments',[]) if c['category']=='total_price']
        if not any(c['value']==230 and c['level']!='inviolable' for c in prices):issues.append('确认后丢失或硬化原软预算')
    if name in ('price_change','stock_change','authorization_expiry'):
        if not any(e['kind']=='dynamic' and e['payload'].get('relevant') for e in events):issues.append('没有观察到相关变化触发H3')
    if name!='cancel' and run['status']!='error':
        roles={e['payload']['role_key'] for e in events if e['kind']=='agent_start'}
        for role in ['Payment Main Supervisor','Payment Evaluation Supervisor','Payment Revisor','Request/Intent Consistency Evaluator','Environment/Feasibility Evaluator','Transaction/Outcome Evaluator']:
            if role not in roles:issues.append('缺少实际角色:'+role)
    rows.append(dict(name=name,run_id=run['id'],status=run['status'],orders=len(orders),offers=len(r.get('offers',[])),
                     model_returns=sum(e['kind']=='model_end' for e in events),passed=not issues,issues=issues))
report={'tests':rows,'passed':sum(r['passed'] for r in rows),'total':len(rows),'scope':'功能小批；非准确率估计，全部尝试分别保留'}
(root/'functional-audit.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(report,ensure_ascii=False,indent=2))
