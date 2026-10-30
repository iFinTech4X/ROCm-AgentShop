import os
from pathlib import Path
from dataclasses import dataclass
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / '.env')

@dataclass(frozen=True)
class Settings:
    db: Path = Path(os.getenv('TAPAY_DB', str(ROOT / 'data/tapay.sqlite3')))
    model_url: str = os.getenv('TAPAY_MODEL_BASE_URL','http://127.0.0.1:8081/v1').rstrip('/')
    model_name: str = os.getenv('TAPAY_MODEL_NAME','Qwen3-8B')
    api_key: str = os.getenv('TAPAY_MODEL_API_KEY','local')
    timeout: int = int(os.getenv('TAPAY_HTTP_TIMEOUT','120'))
    run_timeout: int = int(os.getenv('TAPAY_RUN_TIMEOUT','900'))
    recursion_limit: int = int(os.getenv('TAPAY_RECURSION_LIMIT','60'))
    port: int = int(os.getenv('TAPAY_PORT','8000'))
    device_label: str = os.getenv('TAPAY_DEVICE_LABEL','本地模型服务')
    root_path: str = os.getenv('TAPAY_ROOT_PATH','').rstrip('/')

settings = Settings()
