from dataclasses import dataclass
from pathlib import Path
import os

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
        return cls(
            data_dir=Path(os.getenv("MATCHLAB_DATA_DIR", str(ROOT / ".local"))),
            qwen_base_url=os.getenv("QWEN_BASE_URL", "").rstrip("/"),
            qwen_model=os.getenv("QWEN_MODEL", ""),
            qwen_api_key=os.getenv("QWEN_API_KEY", ""),
            qwen_timeout=float(os.getenv("QWEN_TIMEOUT_SECONDS", "90")),
            qwen_enable_thinking=None if not thinking else thinking == "true",
            qwen_max_tokens=max_tokens,
        )
