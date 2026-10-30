"""One task per process, JSONL events on stdout, diagnostics on stderr."""
import json
import sys
import threading
import traceback
from contextlib import redirect_stdout

def main():
    boot=json.loads(sys.stdin.readline())
    stream=sys.stdout;lock=threading.Lock()
    def emit(value):
        with lock:
            stream.write(json.dumps(value,ensure_ascii=False,default=str)+'\n');stream.flush()
    try:
        with redirect_stdout(sys.stderr):
            from .integration import run_live
            result=run_live(boot,emit)
        emit({'type':'result',**result})
    except Exception as error:
        traceback.print_exc(file=sys.stderr)
        emit({'type':'result','status':'error','content':'运行遇到问题，本次未提交新订单。请查看任务记录后重试。',
              'result':{'failure':type(error).__name__,'detail':str(error)[:600]}})

if __name__=='__main__':main()
