"""Client MQTT 3.1.1 minimale (asyncio, nessuna dipendenza) con riconnessione automatica."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import struct
from typing import Awaitable, Callable

_LOGGER = logging.getLogger("zkteco.mqtt")


def _str(s: str | bytes) -> bytes:
    b = s.encode() if isinstance(s, str) else s
    return struct.pack("!H", len(b)) + b


def _varlen(n: int) -> bytes:
    out = bytearray()
    while True:
        byte = n % 128
        n //= 128
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def topic_matches(sub: str, topic: str) -> bool:
    s, t = sub.split("/"), topic.split("/")
    for i, part in enumerate(s):
        if part == "#":
            return True
        if i >= len(t) or (part != "+" and part != t[i]):
            return False
    return len(s) == len(t)


class MQTTClient:
    def __init__(self, host: str, port: int = 1883, username: str = "", password: str = "",
                 client_id: str = "zkteco_access", will: tuple[str, str] | None = None, keepalive: int = 60) -> None:
        self.host, self.port = host, int(port)
        self.username, self.password = username or "", password or ""
        self.client_id, self.will, self.keepalive = client_id, will, keepalive
        self.on_message: Callable[[str, bytes], Awaitable[None] | None] | None = None
        self.on_connect: Callable[[], Awaitable[None] | None] | None = None
        self.connected = False
        self._subs: list[str] = []
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task | None = None
        self._mid = 0
        self._wlock = asyncio.Lock()

    def start(self) -> None:
        self._task = asyncio.ensure_future(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task
        if self._writer:
            with contextlib.suppress(Exception):
                self._writer.write(b"\xe0\x00")
                self._writer.close()

    async def _loop(self) -> None:
        delay = 2
        while True:
            try:
                await self._session()
                delay = 2
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                if self.connected:
                    _LOGGER.warning("MQTT disconnesso: %s", err)
                else:
                    _LOGGER.debug("MQTT connessione fallita: %s", err)
            self.connected = False
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

    async def _session(self) -> None:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), 10)
        self._writer = writer
        flags = 0x02
        payload = _str(self.client_id)
        if self.will:
            flags |= 0x04 | 0x20 | (1 << 3)  # will, retain, qos1
            payload += _str(self.will[0]) + _str(self.will[1])
        # ordine nel payload: client id, will topic, will msg, username, password
        if self.username:
            flags |= 0x80
            payload += _str(self.username)
        if self.password:
            flags |= 0x40
            payload += _str(self.password)
        var = _str("MQTT") + bytes([4, flags]) + struct.pack("!H", self.keepalive)
        await self._send(0x10, var + payload)
        ptype, data = await asyncio.wait_for(self._read(reader), 10)
        if ptype >> 4 != 2 or len(data) < 2 or data[1] != 0:
            raise ConnectionError(f"CONNACK rifiutato (codice {data[1] if len(data) > 1 else '?'})")
        self.connected = True
        _LOGGER.info("Connesso al broker MQTT %s:%s", self.host, self.port)
        if self._subs:
            await self._subscribe(self._subs)
        if self.on_connect:
            res = self.on_connect()
            if asyncio.iscoroutine(res):
                asyncio.ensure_future(res)
        ping = asyncio.ensure_future(self._pinger())
        try:
            while True:
                ptype, data = await self._read(reader)
                kind = ptype >> 4
                if kind == 3:  # PUBLISH
                    qos = (ptype >> 1) & 3
                    tlen = struct.unpack("!H", data[:2])[0]
                    topic = data[2:2 + tlen].decode("utf-8", "replace")
                    pos = 2 + tlen
                    if qos:
                        mid = data[pos:pos + 2]
                        pos += 2
                        await self._send(0x40, mid)
                    if self.on_message:
                        res = self.on_message(topic, data[pos:])
                        if asyncio.iscoroutine(res):
                            asyncio.ensure_future(res)
        finally:
            ping.cancel()
            writer.close()

    async def _pinger(self) -> None:
        while True:
            await asyncio.sleep(self.keepalive / 2)
            await self._send(0xC0, b"")

    @staticmethod
    async def _read(reader: asyncio.StreamReader) -> tuple[int, bytes]:
        head = (await reader.readexactly(1))[0]
        mult, length = 1, 0
        while True:
            b = (await reader.readexactly(1))[0]
            length += (b & 0x7F) * mult
            if not b & 0x80:
                break
            mult *= 128
        return head, await reader.readexactly(length) if length else b""

    async def _send(self, header: int, body: bytes) -> None:
        if not self._writer:
            raise ConnectionError("non connesso")
        async with self._wlock:
            self._writer.write(bytes([header]) + _varlen(len(body)) + body)
            await self._writer.drain()

    def _next_mid(self) -> int:
        self._mid = self._mid % 65535 + 1
        return self._mid

    async def _subscribe(self, topics: list[str]) -> None:
        body = struct.pack("!H", self._next_mid()) + b"".join(_str(t) + b"\x00" for t in topics)
        await self._send(0x82, body)

    async def subscribe(self, topic: str) -> None:
        if topic not in self._subs:
            self._subs.append(topic)
        if self.connected:
            await self._subscribe([topic])

    async def publish(self, topic: str, payload: str | bytes, retain: bool = False) -> None:
        if not self.connected:
            return
        data = payload.encode() if isinstance(payload, str) else payload
        try:
            await self._send(0x30 | (1 if retain else 0), _str(topic) + data)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("publish fallito: %s", err)
