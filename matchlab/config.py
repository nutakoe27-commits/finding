from dataclasses import dataclass
from pathlib import Path
import os
from urllib.parse import urlsplit

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    qwen_base_url: str = ""
    qwen_model: str = ""
    qwen_api_key: str = ""
    qwen_timeout: float = 90
    qwen_enable_thinking: bool | None = None
    qwen_max_tokens: int = 1024
    bookmaker_ingest_token: str = ""
    public_origin: str = ""
    ai_daily_limit: int = 0
    vapid_private_key: str = ""
    vapid_public_key: str = ""
    vapid_subject: str = ""
    wigolo_mcp_url: str = ""
    wigolo_mcp_token_file: str = ""

    @property
    def database(self) -> Path:
        return self.data_dir / "matchlab.sqlite3"

    @property
    def report(self) -> Path:
        return self.data_dir / "backtest.json"

    @property
    def qwen_configured(self) -> bool:
        return bool(self.qwen_base_url and self.qwen_model)

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(ROOT / ".env", override=False)
        thinking = os.getenv("QWEN_ENABLE_THINKING", "").strip().lower()
        if thinking not in ("", "true", "false"):
            raise ValueError("QWEN_ENABLE_THINKING должен быть true или false.")
        max_tokens = int(os.getenv("QWEN_MAX_TOKENS", "1024"))
        if max_tokens < 1:
            raise ValueError("QWEN_MAX_TOKENS должен быть положительным целым числом.")
        public_origin=os.getenv('PUBLIC_ORIGIN','https://prognozich.online').rstrip('/')
        parsed=urlsplit(public_origin)
        if (parsed.scheme not in ('http','https') or not parsed.hostname or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password
            or (parsed.scheme=='http' and parsed.hostname not in ('127.0.0.1','localhost','::1'))):
            raise ValueError('PUBLIC_ORIGIN должен быть HTTPS origin без пути. HTTP допускается только для локальной разработки.')
        origin_port=parsed.port
        if origin_port==0:raise ValueError('Некорректный порт PUBLIC_ORIGIN.')
        origin_host=parsed.hostname.encode('idna').decode().lower()
        if ':' in origin_host:origin_host='['+origin_host+']'
        default_port=443 if parsed.scheme=='https' else 80
        public_origin=parsed.scheme+'://'+origin_host+(f':{origin_port}' if origin_port and origin_port!=default_port else '')
        daily_limit=int(os.getenv('AI_DAILY_LIMIT','0'))
        if not 0<=daily_limit<=100000:raise ValueError('AI_DAILY_LIMIT должен быть от 0 до 100000.')
        return cls(
            data_dir=Path(os.getenv("MATCHLAB_DATA_DIR", str(ROOT / ".local"))),
            qwen_base_url=os.getenv("QWEN_BASE_URL", "").rstrip("/"),
            qwen_model=os.getenv("QWEN_MODEL", ""),
            qwen_api_key=os.getenv("QWEN_API_KEY", ""),
            qwen_timeout=float(os.getenv("QWEN_TIMEOUT_SECONDS", "90")),
            qwen_enable_thinking=None if not thinking else thinking == "true",
            qwen_max_tokens=max_tokens,
            bookmaker_ingest_token=os.getenv("BOOKMAKER_INGEST_TOKEN", ""),
            public_origin=public_origin,
            ai_daily_limit=daily_limit,
            vapid_private_key=os.getenv("WEBPUSH_PRIVATE_KEY_FILE", ""),
            vapid_public_key=os.getenv("WEBPUSH_PUBLIC_KEY", ""),
            vapid_subject=os.getenv("WEBPUSH_SUBJECT", ""),
            wigolo_mcp_url=os.getenv("WIGOLO_MCP_URL", "").strip(),
            wigolo_mcp_token_file=os.getenv("WIGOLO_MCP_TOKEN_FILE", "").strip(),
        )
