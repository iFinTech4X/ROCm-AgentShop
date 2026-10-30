"""Editable, fictional goods for a real interactive sandbox, not benchmark rows."""
from copy import deepcopy
from datetime import datetime, timezone

def now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')

def products():
    rows = [
        ('A01','听山 Air','耳机','白色',299,'无线入耳','主动降噪，通勤轻巧',18),
        ('A02','听山 Air','耳机','黑色',299,'无线入耳','主动降噪，通勤轻巧',12),
        ('A03','听山 Air Pro','耳机','白色',449,'无线入耳','自适应降噪，双设备连接',9),
        ('A04','听山 Studio','耳机','银灰色',599,'无线头戴','长续航，柔软耳罩',6),
        ('A05','拾音 Lite','耳机','蓝色',169,'无线入耳','轻量佩戴，基础通话',22),
        ('K01','墨键 K68','键盘','白色',259,'68键机械','红轴，热插拔，有线连接',16),
        ('K02','墨键 K68','键盘','黑色',259,'68键机械','红轴，热插拔，有线连接',14),
        ('K03','墨键 K68 Pro','键盘','白色',359,'68键机械','无线三模，热插拔，静音轴',8),
        ('K04','墨键 K98','键盘','灰色',329,'98键机械','数字区，红轴，有线连接',11),
        ('K05','轻敲 Slim','键盘','白色',199,'薄膜全尺寸','安静敲击，无线连接',20),
        ('H01','暖禾 Mini','电热水壶','白色',189,'0.8升','304不锈钢内胆，自动断电',15),
        ('H02','暖禾 Plus','电热水壶','白色',269,'1.5升','304不锈钢内胆，保温功能',12),
        ('H03','清风 Desk','风扇','绿色',129,'桌面款','USB供电，三档风速',24),
        ('H04','清风 Desk Pro','风扇','白色',199,'桌面款','充电电池，五档风速',10),
        ('H05','明序 Light','台灯','白色',229,'阅读款','无级调光，暖冷光可调',13),
        ('D01','山野随行杯','保温杯','绿色',89,'400毫升','不锈钢内胆，随行杯盖',30),
        ('D02','山野随行杯','保温杯','白色',89,'400毫升','不锈钢内胆，随行杯盖',26),
        ('D03','山野随行杯 Pro','保温杯','黑色',129,'600毫升','带提手，长效保温',18),
        ('D04','织野帆布袋','帆布袋','米色',59,'标准款','棉帆布，内置小口袋',35),
        ('D05','织野帆布袋','帆布袋','黑色',59,'标准款','棉帆布，内置小口袋',25),
    ]
    return [{'item_id':i,'unit_price_minor':price*100,'stock':stock,'quote_version':1,
             'attributes':{'name':name,'category':cat,'color':color,'model':model,'description':desc}}
            for i,name,cat,color,price,model,desc,stock in rows]

def initial_state():
    return {'version':1,'updated_at':now(),'currency':'CNY','items':products(),
            'shipping_minor':800,'free_shipping_min_minor':19900,
            'account':{'account_id':'sandbox-wallet','owner_id':'demo-user','authorized':True,
                       'authorization_version':1,'balance_minor':1000000,'limit_minor':500000,
                       'allowed_actions':['purchase']}}

def public_world(state, messages):
    # Compatible shape for the original business calculator. No future schedule.
    return {'domain':'Shopping','currency':state['currency'],'start_at':state['updated_at'],
            'policy':{'stages':['start','finish'],'max_simulations':20,
                      'shipping_minor':state['shipping_minor'],
                      'shipping_tiers':[{'min_subtotal_minor':state['free_shipping_min_minor'],'shipping_minor':0}],
                      'action_space':{'action_types':['purchase'],'quantities':list(range(1,11))}},
            'accounts':[deepcopy(state['account'])],'items':deepcopy(state['items']),
            'initial_messages':[{'message_id':m['id'],'role':'user','content':m['content']} for m in messages if m['role']=='user'],
            'events':[]}
