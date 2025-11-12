import os, json
from dataclasses import dataclass
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(dotenv_path=Path(__file__).resolve().parents[2] / ".env")

@dataclass
class LLMConfig:
    provider: str = os.getenv("LLM_PROVIDER", "deepseek").lower()
    api_key: str = os.getenv("LLM_API_KEY") or os.getenv("DEEPSEEK_API_KEY")
    base_url: str = os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1")
    model: str = os.getenv("LLM_MODEL", "deepseek-chat")
    timeout: int = int(os.getenv("LLM_TIMEOUT", "60"))
    extra_headers_json: str = os.getenv("LLM_EXTRA_HEADERS", "{}")

    def extra_headers(self):
        try:
            return json.loads(self.extra_headers_json)
        except Exception:
            return {}
