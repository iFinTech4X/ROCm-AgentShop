from pathlib import Path
import json,hashlib
root=Path(__file__).resolve().parents[1]
d=json.loads((root/'vendor/SOURCE.json').read_text('utf-8'))
for row in d['files']:
    p=root/'vendor/rq2'/row['path']
    assert hashlib.sha256(p.read_bytes()).hexdigest()==row['sha256'],str(p)
print(f"Original source unchanged: {len(d['files'])} files")
