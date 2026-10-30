"""Actual resident-model checks. Every attempt is retained; no score filtering."""
import json,time,uuid,urllib.request,sys,os
from pathlib import Path
BASE=os.environ.get('VALIDATION_BASE','http://127.0.0.1:8002')
ROOT=Path(__file__).resolve().parents[1]/'artifacts'/sys.argv[1]
ROOT.mkdir(parents=True,exist_ok=False)
rows=[]
def api(path,body=None):
    req=urllib.request.Request(BASE+'/api/'+path,data=json.dumps(body,ensure_ascii=False).encode() if body is not None else None,headers={'Content-Type':'application/json'})
    return json.load(urllib.request.urlopen(req,timeout=30))
def check(name,text,sid=None,change=None):
    sid=sid or api('sessions',{})['id'];rid=api(f'sessions/{sid}/messages',dict(content=text,request_key=uuid.uuid4().hex))['run_id']
    start=time.time();changed=False
    print('START',name,sid,rid,flush=True)
    while time.time()-start<1000:
        data=api(f'sessions/{sid}/export');r=next(x for x in data['session']['runs'] if x['id']==rid)
        trigger='intent' if change and change['kind']=='cancel' else 'quote'
        if change and not changed and any(e['kind']==trigger and e['run_id']==rid for e in data['events']):
            api(f'sessions/{sid}/cancel',{}) if change['kind']=='cancel' else api(f'sessions/{sid}/environment',change)
            changed=True
        if r['status'] not in ('running','queued'):break
        time.sleep(1)
    data=api(f'sessions/{sid}/export');(ROOT/f'{name}.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    r=next(x for x in data['session']['runs'] if x['id']==rid);result=r['result'] or {}
    row=dict(name=name,sid=sid,rid=rid,status=r['status'],seconds=round(time.time()-start,2),offers=len(result.get('offers',[])),orders=len(data['session']['orders']),failure=result.get('failure'),changed=changed)
    rows.append(row);(ROOT/'summary.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8');print('RESULT',json.dumps(row,ensure_ascii=False),flush=True)
    return sid,data,result

sid,data,r=check('generic_230','帮我买个耳机，价格230左右。')
if r.get('offers'):
    sid,data,r=check('yes_no_selection','是的',sid)
    if not data['session']['orders'] and r.get('offers'):
        o=r['offers'][0]
        check('confirm_offer',f"确认购买{o['color']}{o['name']}（{o['item_id']}）{o['quantity']}件，按含运费{o['total_minor']/100:.2f}元这份报价，可以下单。",sid)
check('normal','帮我买一副白色听山 Air，含运费不超过320元，符合就下单。')
check('soft_budget','帮我买一副白色听山 Air，预算大约280元，超出20元以内也可以，直接下单。')
check('hard_budget','帮我买一副白色听山 Air，含运费绝对不能超过280元，符合才下单。')
sid,data,r=check('revision_before','先看看黑色听山 Air，预算约320元，先不要下单。')
check('revision_after','改成白色的，其他不变，可以下单。',sid)
for name,change in [('price_change',dict(kind='price',item_id='A01',value=34900)),('stock_change',dict(kind='stock',item_id='A01',value=0)),('authorization_expiry',dict(kind='expire')),('cancel',dict(kind='cancel'))]:
    check(name,'帮我买一副白色听山 Air，含运费不超过320元，符合就下单。',change=change)
sid,data,r=check('multiple','买2个白色山野随行杯，含运费不超过200元，可以下单。')
check('next_task_recommend','再看看白色轻敲 Slim 键盘，预算大约200元，先推荐，不要下单。',sid)
print('ALL_DONE',flush=True)
