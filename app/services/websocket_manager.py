"""会议级 WebSocket 广播管理器。

这个模块只做一件事：维护按 `meeting_id` 分组的 WebSocket 连接池，并向该会议的所有连接广播状态事件。

典型使用场景：
1. 音频上传完成后通知前端刷新列表。
2. 火山妙记后台轮询线程完成后通知前端纪要已生成。
3. API 和后台线程共享同一套广播实现，避免各处重复维护连接池。
"""

import asyncio
import json
from collections import defaultdict
from typing import Dict, Set

from fastapi import WebSocket

from app.utils.logger import get_logger

logger = get_logger("meeting_ws_manager")


class MeetingTranscriptionWSManager:
    """会议级别 WebSocket 广播管理器。

    关键约束：
    1. 连接池按 meeting_id 分组，而不是按用户分组。
    2. 后台线程不能直接 `await`，因此需要在应用启动时绑定主事件循环。
    3. 发送失败的连接会被自动剔除，避免脏连接长期滞留。
    """

    def __init__(self) -> None:
        self._connections: Dict[int, Set[WebSocket]] = defaultdict(set)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = asyncio.Lock()

    # 步骤说明：应用启动时绑定主事件循环，供线程内安全投递协程。
    def set_event_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    # 步骤说明：接收连接并按 meeting_id 加入连接池。
    async def connect(self, meeting_id: int, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections[meeting_id].add(websocket)

    # 步骤说明：断开连接并回收空集合，避免连接池泄漏。
    async def disconnect(self, meeting_id: int, websocket: WebSocket) -> None:
        async with self._lock:
            conns = self._connections.get(meeting_id)
            if not conns:
                return
            conns.discard(websocket)
            if not conns:
                self._connections.pop(meeting_id, None)

    # 步骤说明：序列化消息并广播给 meeting 下所有连接，失败连接自动剔除。
    async def _send(self, meeting_id: int, payload: dict) -> None:
        conns = list(self._connections.get(meeting_id, set()))
        if not conns:
            return
        text = json.dumps(payload, ensure_ascii=False)
        stale: list[WebSocket] = []
        for ws in conns:
            try:
                await ws.send_text(text)
            except Exception as exc:  # noqa: BLE001
                logger.warning("meeting ws send failed meeting_id=%s error=%s", meeting_id, exc)
                stale.append(ws)
        if stale:
            async with self._lock:
                current = self._connections.get(meeting_id)
                if not current:
                    return
                for ws in stale:
                    current.discard(ws)
                if not current:
                    self._connections.pop(meeting_id, None)

    # 步骤说明：提供协程态广播入口（供 async 上下文调用）。
    async def broadcast(self, meeting_id: int, payload: dict) -> None:
        await self._send(meeting_id, payload)

    # 步骤说明：提供线程态广播入口（供后台线程投递状态更新）。
    def notify_from_thread(self, meeting_id: int, payload: dict) -> None:
        # 后台线程只有在主循环已绑定后才能安全投递广播任务。
        if not self._loop:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._send(meeting_id, payload), self._loop)
        except RuntimeError as exc:
            logger.warning("meeting ws thread notify failed: %s", exc)


meeting_ws_manager = MeetingTranscriptionWSManager()
