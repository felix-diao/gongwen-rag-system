import os, json
from dataclasses import dataclass
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(dotenv_path=Path(__file__).resolve().parents[2] / ".env")

@dataclass
class LLMConfig:
    provider: str = os.getenv("LLM_PROVIDER", "deepseek").lower()
    api_key: str = os.getenv("LLM_API_KEY")
    base_url: str = os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1")
    model: str = os.getenv("LLM_MODEL", "deepseek-chat")
    timeout: int = int(os.getenv("LLM_TIMEOUT", "180"))
    extra_headers_json: str = os.getenv("LLM_EXTRA_HEADERS", "{}")
    # 默认不继承系统 HTTP(S)_PROXY，避免本地失效代理导致 LLM 调用失败
    use_env_proxy: bool = os.getenv("LLM_USE_ENV_PROXY", "false").lower() in ("1", "true", "yes", "on")
    # 如需指定代理，请显式配置；留空则不使用固定代理
    proxy_url: str = os.getenv("LLM_PROXY_URL", "").strip()

    def extra_headers(self):
        try:
            return json.loads(self.extra_headers_json)
        except Exception:
            return {}
