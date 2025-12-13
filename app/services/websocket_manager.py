import asyncio
import json
from collections import defaultdict
from typing import Dict, Set

from fastapi import WebSocket
from app.utils.logger import get_logger

logger = get_logger("websocket_manager")


class MeetingTranscriptionWSManager:
    """Manages meeting audio transcription websocket clients."""

    def __init__(self) -> None:
        self._connections: Dict[int, Set[WebSocket]] = defaultdict(set)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = asyncio.Lock()

    def set_event_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        logger.info("Meeting WS manager bound to event loop")

    async def connect(self, meeting_id: int, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections[meeting_id].add(websocket)
        logger.debug("WebSocket connected: meeting %s", meeting_id)

    async def disconnect(self, meeting_id: int, websocket: WebSocket) -> None:
        async with self._lock:
            conns = self._connections.get(meeting_id)
            if conns and websocket in conns:
                conns.remove(websocket)
                if not conns:
                    self._connections.pop(meeting_id, None)
        logger.debug("WebSocket disconnected: meeting %s", meeting_id)

    async def _send(self, meeting_id: int, payload: dict) -> None:
        conns = list(self._connections.get(meeting_id, set()))
        if not conns:
            return
        text = json.dumps(payload, ensure_ascii=False)
        stale: list[WebSocket] = []
        for ws in conns:
            try:
                await ws.send_text(text)
            except Exception:  # noqa: BLE001
                stale.append(ws)
        if stale:
            async with self._lock:
                conns_set = self._connections.get(meeting_id)
                if conns_set:
                    for ws in stale:
                        conns_set.discard(ws)
                    if not conns_set:
                        self._connections.pop(meeting_id, None)

    async def broadcast(self, meeting_id: int, payload: dict) -> None:
        await self._send(meeting_id, payload)

    def notify_from_thread(self, meeting_id: int, payload: dict) -> None:
        if not self._loop:
            logger.debug("No loop bound, skip WS notify")
            return
        try:
            asyncio.run_coroutine_threadsafe(self._send(meeting_id, payload), self._loop)
        except RuntimeError as exc:
            logger.warning("Failed to submit WS notify: %s", exc)


meeting_ws_manager = MeetingTranscriptionWSManager()
