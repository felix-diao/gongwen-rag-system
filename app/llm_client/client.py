import time, requests
from typing import List, Dict, Optional
from .config import LLMConfig

class LLMClient:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self.session = requests.Session()
        # 默认禁用 requests 对系统代理环境变量的继承，避免 127.0.0.1:7890 等失效代理拖挂
        self.session.trust_env = bool(self.cfg.use_env_proxy)
        if self.cfg.proxy_url:
            self.session.proxies.update({
                "http": self.cfg.proxy_url,
                "https": self.cfg.proxy_url,
            })

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
             retries: int = 2,
             user_id: Optional[str] = None) -> str:
        url = f"{self.cfg.base_url.rstrip('/')}/chat/completions"
        model_name = model or self.cfg.model
        body = {
            "model": model_name,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        # 估算请求字符数（用于 API 不返回 usage 时的回退）
        request_chars = sum(len(m.get("content", "")) for m in messages)
        for attempt in range(retries + 1):
            t_start = time.time()
            try:
                resp = self.session.post(url, headers=self._headers(), json=body, timeout=self.cfg.timeout)
                duration_ms = int((time.time() - t_start) * 1000)
                resp.raise_for_status()
                data = resp.json()
                text = data["choices"][0]["message"]["content"].strip()
                usage = data.get("usage", {})
                prompt_tokens = usage.get("prompt_tokens", 0)
                completion_tokens = usage.get("completion_tokens", 0)
                total_tokens = usage.get("total_tokens", prompt_tokens + completion_tokens)

                # 记录 token 消耗
                try:
                    from app.services.token_tracker import token_tracker
                    token_tracker.record(
                        user_id=user_id,
                        api_category="llm",
                        api_endpoint=url,
                        model=model_name,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        total_tokens=total_tokens,
                        request_chars=request_chars,
                        duration_ms=duration_ms,
                    )
                except Exception:
                    pass  # 记录失败不影响主流程

                return text
            except Exception as e:
                duration_ms = int((time.time() - t_start) * 1000)
                last_err = e
                if attempt < retries:
                    time.sleep(1.5 ** attempt)
                    continue
                # 最后一次重试失败，记录错误
                try:
                    from app.services.token_tracker import token_tracker
                    token_tracker.record(
                        user_id=user_id,
                        api_category="llm",
                        api_endpoint=url,
                        model=model_name,
                        request_chars=request_chars,
                        duration_ms=duration_ms,
                        status="error",
                        error_msg=str(last_err)[:500],
                    )
                except Exception:
                    pass
        hint = ""
        if "proxy" in str(last_err).lower():
            hint = "；可检查 LLM_USE_ENV_PROXY / LLM_PROXY_URL 配置"
        return f"（调用失败：{last_err}{hint}）"
