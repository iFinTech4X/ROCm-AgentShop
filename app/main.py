import asyncio
import json
import secrets
from contextlib import asynccontextmanager
from typing import Literal
import httpx
from fastapi import FastAPI,Request,HTTPException
from fastapi.responses import JSONResponse,StreamingResponse,FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel,Field,ConfigDict
from .config import settings,ROOT
from .store import Store,Conflict,NotFound
from .manager import Manager

def create_app(config=settings,enable_workers=True):
    store=Store(config.db);manager=Manager(store,config)
    @asynccontextmanager
    async def lifespan(app):
        if enable_workers:await manager.start()
        yield
        if enable_workers:await manager.close()
    # AUP's proxy strips its external prefix. Browser URLs are relative; keep
    # ASGI paths unprefixed so StaticFiles can resolve its mounted asset path.
    app=FastAPI(title='TAPay 智购',lifespan=lifespan)
    app.state.store=store;app.state.manager=manager
    @app.exception_handler(Conflict)
    async def conflict(request,e):return JSONResponse({'detail':str(e)},status_code=409)
    @app.exception_handler(NotFound)
    async def missing(request,e):return JSONResponse({'detail':str(e)},status_code=404)
    @app.exception_handler(ValueError)
    async def invalid(request,e):return JSONResponse({'detail':str(e)},status_code=422)
    @app.middleware('http')
    async def same_origin(request,call_next):
        origin=request.headers.get('origin')
        if request.method in ('POST','PUT','PATCH','DELETE') and origin:
            from urllib.parse import urlsplit
            origin_host=urlsplit(origin).netloc
            expected=request.headers.get('x-forwarded-host',request.headers.get('host','')).split(',')[0].strip()
            if origin_host!=expected:return JSONResponse({'detail':'不允许跨站修改'},status_code=403)
        response=await call_next(request);response.headers['X-Content-Type-Options']='nosniff'
        if response.status_code>=400 or request.url.path=='/':response.headers['Cache-Control']='no-store'
        return response
    @app.get('/api/health')
    async def health():
        available=False
        try:
            async with httpx.AsyncClient(timeout=3,trust_env=False) as c:
                r=await c.get(config.model_url+'/models',headers={'Authorization':f'Bearer {config.api_key}'})
                available=r.is_success
        except httpx.HTTPError:pass
        return {'status':'ok','model_available':available,'model':config.model_name,'device':config.device_label,'sandbox':True}
    @app.get('/api/sessions')
    def sessions():return store.list_sessions()
    @app.post('/api/sessions')
    def new():return store.create_session()
    @app.get('/api/sessions/{sid}')
    def session(sid:str):return store.session(sid)
    class Message(BaseModel):
        model_config=ConfigDict(extra='forbid')
        content:str=Field(min_length=1,max_length=5000)
        request_key:str=Field(min_length=8,max_length=100)
    @app.post('/api/sessions/{sid}/messages')
    async def message(sid:str,body:Message):
        rid=store.submit(sid,body.content,body.request_key)
        if enable_workers:await manager.enqueue(rid)
        return {'run_id':rid}
    @app.post('/api/sessions/{sid}/cancel')
    def cancel(sid:str):store.cancel(sid);return {'ok':True}
    class EnvironmentChange(BaseModel):
        model_config=ConfigDict(extra='forbid')
        kind:Literal['price','stock','revoke','restore','expire']
        item_id:str|None=None
        value:int|None=Field(default=None,ge=0,le=1000000)
    @app.post('/api/sessions/{sid}/environment')
    def environment(sid:str,body:EnvironmentChange):return store.change_environment(sid,body.model_dump())
    @app.get('/api/sessions/{sid}/events')
    async def events(sid:str,request:Request,after:int=0):
        store.session(sid)
        try:after=max(after,int(request.headers.get('last-event-id','0')))
        except ValueError:pass
        async def stream():
            cursor=after;idle=0
            yield 'retry: 1500\n: connected\n\n'
            while not await request.is_disconnected():
                batch=store.events(sid,cursor)
                for event in batch:
                    cursor=event['id'];yield f"id: {cursor}\ndata: {json.dumps(event,ensure_ascii=False)}\n\n"
                idle+=1
                if idle%15==0:yield ': heartbeat\n\n'
                await asyncio.sleep(.6)
        return StreamingResponse(stream(),media_type='text/event-stream',headers={'Cache-Control':'no-cache, no-transform','X-Accel-Buffering':'no'})
    @app.get('/api/sessions/{sid}/events/history')
    def event_history(sid:str,after:int=0):
        return store.events(sid,after)
    @app.get('/api/sessions/{sid}/export')
    def export(sid:str):
        events=[];cursor=0
        while batch:=store.events(sid,cursor):events.extend(batch);cursor=batch[-1]['id']
        return JSONResponse({'session':store.session(sid),'events':events},headers={'Content-Disposition':f'attachment; filename="{sid}.json"'})
    def authorize(rid,request):
        expected=manager.tokens.get(rid)
        if not expected or not secrets.compare_digest(expected,request.headers.get('x-worker-token','')):raise HTTPException(403,'Worker credential invalid')
    @app.get('/api/_worker/{rid}/checkpoint')
    def checkpoint(rid:str,request:Request):authorize(rid,request);return store.checkpoint(rid)
    @app.post('/api/_worker/{rid}/quote')
    async def quote(rid:str,request:Request):
        authorize(rid,request);body=await request.json();return store.record_quote(rid,body['receipt'],body['intent'])
    @app.post('/api/_worker/{rid}/commit')
    async def commit(rid:str,request:Request):
        authorize(rid,request);body=await request.json();return store.commit(rid,body['receipt_id'],body['intent'],body['alignment'])
    dist=ROOT/'frontend/dist'
    if dist.exists():
        app.mount('/assets',StaticFiles(directory=dist/'assets'),name='assets')
        @app.get('/')
        def index():return FileResponse(dist/'index.html')
    else:
        @app.get('/')
        def index():return {'message':'请先构建 frontend：npm install && npm run build'}
    return app

app=create_app()
