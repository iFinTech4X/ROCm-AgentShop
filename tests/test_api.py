from dataclasses import replace
from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app

def test_rpc_is_private_and_changes_are_same_origin(tmp_path):
    app=create_app(replace(Settings(),db=tmp_path/'test.sqlite'),enable_workers=False)
    with TestClient(app) as c:
        s=c.post('/api/sessions').json();sid=s['id']
        r=c.post(f'/api/sessions/{sid}/messages',json={'content':'白色耳机','request_key':'valid-key'}).json()['run_id']
        assert c.get(f'/api/_worker/{r}/checkpoint').status_code==403
        assert c.post(f'/api/sessions/{sid}/cancel',headers={'Origin':'https://evil.example'}).status_code==403
        assert c.post(f'/api/sessions/{sid}/environment',json={'kind':'price','item_id':'A01','value':34900}).status_code==200
        exported=c.get(f'/api/sessions/{sid}/export').json()
        assert len(exported['events'])==3
        assert exported['session']['state']['items'][0]['unit_price_minor']==34900

def test_static_assets_behind_stripping_proxy(tmp_path):
    from app.config import ROOT
    app=create_app(replace(Settings(),db=tmp_path/'proxy.sqlite',root_path='/user/demo/proxy/8000'),enable_workers=False)
    with TestClient(app) as c:
        for p in (ROOT/'frontend/dist/assets').glob('*'):
            assert c.get('/assets/'+p.name).status_code==200
