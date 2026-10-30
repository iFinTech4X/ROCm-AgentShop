"""One resident model, one isolated orchestration process per task."""
import asyncio
import json
import os
import secrets
import sys
import time
from .config import ROOT

class Manager:
    def __init__(self,store,settings):
        self.store=store;self.settings=settings;self.queue=asyncio.Queue();self.tokens={};self.task=None;self.process=None
    async def start(self):
        self.store.interrupt_previous();self.task=asyncio.create_task(self.consume())
    async def close(self):
        if self.process and self.process.returncode is None:self.process.terminate()
        if self.task:
            self.task.cancel()
            try:await self.task
            except asyncio.CancelledError:pass
    async def enqueue(self,rid):await self.queue.put(rid)
    async def consume(self):
        while True:
            rid=await self.queue.get()
            try:await self.execute(rid)
            except asyncio.CancelledError:raise
            except Exception as e:self.store.finish(rid,'error',{'failure':type(e).__name__},'服务未完成本次任务，记录已保留。')
            finally:self.queue.task_done()
    async def execute(self,rid):
        s=self.store.run(rid);sid=s['session_id']
        if not self.store.start(rid):
            self.store.finish(rid,'cancelled',{},'任务已停止。');return
        token=secrets.token_urlsafe(32);self.tokens[rid]=token
        boot={'run_id':rid,'backend_url':f'http://127.0.0.1:{self.settings.port}','token':token,
              'recursion_limit':self.settings.recursion_limit,
              'model':{'name':self.settings.model_name,'url':self.settings.model_url,'api_key':self.settings.api_key,'timeout':self.settings.timeout}}
        process=await asyncio.create_subprocess_exec(sys.executable,'-u','-m','app.worker',cwd=str(ROOT),
                      stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,
                      env={**os.environ,'PYTHONIOENCODING':'utf-8'},limit=4*1024*1024)
        self.process=process;process.stdin.write((json.dumps(boot,ensure_ascii=False)+'\n').encode());await process.stdin.drain();process.stdin.close()
        result=None;errors=bytearray();started=time.monotonic();reason=None
        async def stderr():
            while chunk:=await process.stderr.read(8192):
                errors.extend(chunk)
                if len(errors)>24000:del errors[:-24000]
        async def stdout():
            nonlocal result
            while line:=await process.stdout.readline():
                try:item=json.loads(line)
                except (ValueError,UnicodeError):continue
                if item.get('type')=='event':self.store.event(sid,rid,item['kind'],item['role'],item['title'],item.get('payload'))
                elif item.get('type')=='result':result=item
        readers=[asyncio.create_task(stdout()),asyncio.create_task(stderr())]
        try:
            while process.returncode is None:
                if self.store.run(rid)['cancelled']:reason='CANCELLED';process.terminate();break
                if time.monotonic()-started>self.settings.run_timeout:reason='TIMEOUT';process.terminate();break
                await asyncio.sleep(.3)
            await process.wait();await asyncio.gather(*readers)
            orders=self.store.session(sid)['orders'];order=next((o for o in orders if o['run_id']==rid),None)
            if order:
                self.store.finish(rid,'completed',(result or {}).get('result',{'order':order}),
                    (result or {}).get('content') or f"沙盒订单 {order['id']} 已生成，实付 ¥{order['total_minor']/100:.2f}。")
            elif reason:self.store.finish(rid,'cancelled' if reason=='CANCELLED' else 'error',{'failure':reason},'任务已停止，本次没有生成订单。')
            elif result:self.store.finish(rid,result['status'],result.get('result',{}),result.get('content',''))
            else:self.store.finish(rid,'error',{'failure':'WORKER_EXIT','exit_code':process.returncode},'执行进程提前退出，本次没有生成订单。')
            if errors and not order and (not result or result.get('status')=='error'):
                detail=errors.decode('utf-8','replace').replace(token,'[redacted]').replace(self.settings.api_key,'[redacted]' if self.settings.api_key!='local' else 'local')
                self.store.event(sid,rid,'diagnostic','服务','运行诊断',{'detail':detail[-10000:]})
        finally:
            self.tokens.pop(rid,None)
            if process.returncode is None:process.terminate();await process.wait()
            for t in readers:
                if not t.done():t.cancel()
            self.process=None
