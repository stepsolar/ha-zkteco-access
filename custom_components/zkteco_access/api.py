"""Client dell'API dell'add-on ZKTeco Access (REST + stream di eventi SSE)."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)


class AddonError(Exception):
    """Errore restituito dall'add-on o di rete."""


class AddonClient:
    """Tutte le operazioni passano dall'add-on: è l'unico a parlare con i controller."""

    def __init__(self, session: aiohttp.ClientSession, host: str, port: int, token: str = "") -> None:
        self._session = session
        self.base = f"http://{host}:{port}/"
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}

    async def request(self, method: str, path: str, body: Any = None, timeout: float = 20) -> Any:
        try:
            async with self._session.request(
                method, self.base + path, json=body, headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                text = await resp.text()
                data = json.loads(text) if text and resp.content_type == "application/json" else text
                if resp.status >= 400:
                    msg = data.get("error") if isinstance(data, dict) else str(data)
                    raise AddonError(msg or f"HTTP {resp.status}")
                return data
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise AddonError(f"add-on non raggiungibile: {err}") from err

    async def state(self) -> dict:
        return await self.request("GET", "api/state")

    async def open_door(self, door_id: int, seconds: int | None = None) -> None:
        await self.request("POST", f"api/doors/{door_id}/open", {"seconds": seconds} if seconds else {})

    async def close_door(self, door_id: int) -> None:
        await self.request("POST", f"api/doors/{door_id}/close", {})

    async def normal_open(self, door_id: int, enable: bool) -> None:
        await self.request("POST", f"api/doors/{door_id}/normal_open", {"enable": enable})

    async def aux_output(self, aux_id: int, seconds: int) -> None:
        await self.request("POST", f"api/aux/{aux_id}/output", {"seconds": seconds})

    async def panel_action(self, panel_id: int, action: str) -> Any:
        return await self.request("POST", f"api/panels/{panel_id}/action", {"action": action})

    async def command(self, data: dict, timeout: float = 30) -> Any:
        return await self.request("POST", "api/cmd", data, timeout=timeout)

    async def stream(self) -> AsyncIterator[dict]:
        """Messaggi in tempo reale dall'add-on (stato pannelli, eventi, modifiche di configurazione)."""
        async with self._session.get(
            self.base + "api/stream", headers=self._headers,
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=70),
        ) as resp:
            if resp.status != 200:
                raise AddonError(f"stream HTTP {resp.status}")
            async for raw in resp.content:
                line = raw.decode("utf-8", "replace").strip()
                if line.startswith("data:"):
                    try:
                        yield json.loads(line[5:].strip())
                    except ValueError:
                        continue
