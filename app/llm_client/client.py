import time, requests
from typing import List, Dict, Optional
from .config import LLMConfig

class LLMClient:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self.session = requests.Session()

    def _headers(self):
        headers = {"Content-Type": "application/json"}
        if self.cfg.api_key:
            headers["Authorization"] = f"Bearer {self.cfg.api_key}"
        headers.update(self.cfg.extra_headers())
        return headers

    def chat(self, messages: List[Dict[str, str]],
             model: Optional[str] = None,
             temperature: float = 0.6,
             max_tokens: int = 1000,
             retries: int = 2) -> str:
        url = f"{self.cfg.base_url.rstrip('/')}/chat/completions"
        body = {
            "model": model or self.cfg.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        for attempt in range(retries + 1):
            try:
                resp = self.session.post(url, headers=self._headers(), json=body, timeout=self.cfg.timeout)
                resp.raise_for_status()
                return resp.json()["choices"][0]["message"]["content"].strip()
            except Exception as e:
                last_err = e
                time.sleep(1.5 ** attempt)
        return f"（调用失败：{last_err}）"
