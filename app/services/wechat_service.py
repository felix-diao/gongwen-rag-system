"""企业微信 API 服务封装。

提供：
- 获取/缓存 access_token
- code 换 userId（免登）
- userId 换用户详情（姓名）
- 生成 JS-SDK config 签名
"""

import time
import hashlib
import random
import string
from typing import Optional, Dict, Any

import requests

from app.config import settings
from app.utils.logger import get_logger

logger = get_logger("wechat_service")


class WechatService:
    """企业微信 API 服务。"""

    _access_token: Optional[str] = None
    _token_expires_at: float = 0

    # ------------------------------------------------------------------
    # access_token（带缓存）
    # ------------------------------------------------------------------
    def _get_access_token(self) -> str:
        """获取企业微信 access_token，带 2 小时缓存。"""
        now = time.time()
        if self._access_token and now < self._token_expires_at - 60:
            return self._access_token

        url = (
            "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
            f"?corpid={settings.WECHAT_CORP_ID}"
            f"&corpsecret={settings.WECHAT_SECRET}"
        )
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data: Dict[str, Any] = resp.json()

        if data.get("errcode") != 0:
            raise RuntimeError(f"获取 access_token 失败: {data}")

        self._access_token = data["access_token"]
        # 企业微信返回 expires_in 秒，通常 7200
        self._token_expires_at = now + data.get("expires_in", 7200)
        logger.info("企业微信 access_token 已刷新")
        return self._access_token

    # ------------------------------------------------------------------
    # 免登：code → userId
    # ------------------------------------------------------------------
    def get_user_id(self, code: str) -> str:
        """用免登 code 换取企业微信 userId。"""
        token = self._get_access_token()
        url = (
            "https://qyapi.weixin.qq.com/cgi-bin/user/getuserinfo"
            f"?access_token={token}&code={code}"
        )
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data: Dict[str, Any] = resp.json()

        if data.get("errcode") != 0:
            raise RuntimeError(f"getuserinfo 失败: {data}")

        user_id = data.get("UserId") or data.get("userid")
        if not user_id:
            raise RuntimeError(f"getuserinfo 未返回 userId: {data}")

        logger.info("企业微信免登成功 userId=%s", user_id)
        return user_id

    # ------------------------------------------------------------------
    # userId → 姓名
    # ------------------------------------------------------------------
    def get_user_name(self, user_id: str) -> str:
        """用 userId 换取用户姓名。"""
        token = self._get_access_token()
        url = (
            "https://qyapi.weixin.qq.com/cgi-bin/user/get"
            f"?access_token={token}&userid={user_id}"
        )
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data: Dict[str, Any] = resp.json()

        if data.get("errcode") != 0:
            logger.warning("获取用户详情失败 userId=%s error=%s", user_id, data)
            return user_id  # fallback 用 userId

        return data.get("name") or user_id

    # ------------------------------------------------------------------
    # JS-SDK config 签名
    # ------------------------------------------------------------------
    def get_js_config(self, url: str) -> Dict[str, Any]:
        """生成 wx.config 所需的参数。

        :param url: 当前页面 URL（不含 # 后面部分）
        """
        token = self._get_access_token()

        # 1. 获取 jsapi_ticket
        ticket_url = (
            "https://qyapi.weixin.qq.com/cgi-bin/get_jsapi_ticket"
            f"?access_token={token}"
        )
        ticket_resp = requests.get(ticket_url, timeout=10)
        ticket_resp.raise_for_status()
        ticket_data = ticket_resp.json()
        if ticket_data.get("errcode") != 0:
            raise RuntimeError(f"获取 jsapi_ticket 失败: {ticket_data}")
        jsapi_ticket = ticket_data["ticket"]

        # 2. 生成签名
        timestamp = int(time.time())
        nonce_str = "".join(random.choices(string.ascii_letters + string.digits, k=16))
        raw = f"jsapi_ticket={jsapi_ticket}&noncestr={nonce_str}&timestamp={timestamp}&url={url}"
        signature = hashlib.sha1(raw.encode()).hexdigest()

        return {
            "corpId": settings.WECHAT_CORP_ID,
            "agentId": settings.WECHAT_AGENT_ID,
            "timestamp": timestamp,
            "nonceStr": nonce_str,
            "signature": signature,
        }


wechat_service = WechatService()
