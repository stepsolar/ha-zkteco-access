"""Client asyncio nativo per i controller ZKTeco C3 / inBio (protocollo "PullSDK").

Nessuna DLL: parla direttamente il protocollo binario TCP dei pannelli, quindi
funziona su Home Assistant OS senza il PC con ZKAccess.

Frame::

    0xAA | ver | cmd | len_lo | len_hi | payload | crc_lo | crc_hi | 0x55

CRC-16/ARC (poly 0xA001, init 0) calcolato su ``ver .. payload``.
In modalità sessione il payload inizia con session_id (LE16) e request_nr (LE16).

Comandi (codici verificati sul PullSDK plcommpro.dll):

    0x01 connect (senza sessione)   0x02 disconnect      0x03 SetDeviceParam
    0x04 GetDeviceParam             0x05 ControlDevice   0x06 struttura tabelle
    0x07 SetDeviceData              0x08 GetDeviceData   0x09 DeleteDeviceData
    0x0A GetDeviceDataCount         0x0B RT log binario  0x76 connect con sessione
    0x79 RT log chiave/valore

Le tabelle (user, userauthorize, timezone, holiday, transaction...) sono
codificate in binario: [indice tabella][n campi][indici campi...] seguiti dai
record; ogni valore è [lunghezza][byte] (interi little-endian di lunghezza
minima, 0 => lunghezza 0; stringhe ASCII).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

_LOGGER = logging.getLogger(__name__)

DEFAULT_PORT = 4370
FRAME_START = 0xAA
FRAME_END = 0x55
PROTOCOL_VERSION = 0x01
REPLY_OK = 0xC8
REPLY_ERROR = 0xC9
INITIAL_SESSION = 0xFEFE
INITIAL_REQUEST = 0xFEFE  # -258 come fa il PullSDK
MAX_PAYLOAD = 0xFFFF

CMD_CONNECT = 0x01
CMD_DISCONNECT = 0x02
CMD_SET_PARAM = 0x03
CMD_GET_PARAM = 0x04
CMD_CONTROL = 0x05
CMD_TABLE_CFG = 0x06
CMD_SET_DATA = 0x07
CMD_GET_DATA = 0x08
CMD_DELETE_DATA = 0x09
CMD_DATA_COUNT = 0x0A
CMD_RTLOG_BIN = 0x0B
CMD_CONNECT_SESSION = 0x76
CMD_RTLOG_KV = 0x79

# ControlDevice
OP_OUTPUT = 1
OP_CANCEL_ALARM = 2
OP_RESTART = 3
OP_NORMAL_OPEN = 4
ADDR_DOOR = 1
ADDR_AUX = 2

EVENT_STATUS = 255

ERROR_CODES = {
    -1: "comando non supportato",
    -2: "errore biometrico",
    -3: "parametri di connessione errati",
    -4: "memoria piena",
    -5: "dato già presente",
    -6: "dato non trovato",
    -7: "driver disconnesso",
    -10: "errore di trasmissione",
    -12: "comando vuoto",
    -13: "comando non disponibile",
    -14: "password di comunicazione errata",
}


class C3Error(Exception):
    """Errore generico pannello."""


class C3ConnectionError(C3Error):
    """Problema di rete / frame / timeout."""


class C3AuthError(C3Error):
    """Password di comunicazione rifiutata."""


class C3CommandError(C3Error):
    def __init__(self, command: int, code: int) -> None:
        self.command = command
        self.code = code
        super().__init__(f"comando 0x{command:02x}: errore {code} ({ERROR_CODES.get(code, 'sconosciuto')})")


# ---------------------------------------------------------------- utilità

def crc16(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc & 0xFFFF


def build_frame(command: int, payload: bytes = b"") -> bytes:
    body = bytes([PROTOCOL_VERSION, command, len(payload) & 0xFF, len(payload) >> 8]) + payload
    crc = crc16(body)
    return bytes([FRAME_START]) + body + bytes([crc & 0xFF, crc >> 8, FRAME_END])


def parse_frame(header: bytes, rest: bytes) -> tuple[int, bytes]:
    if len(header) != 5 or header[0] != FRAME_START:
        raise C3ConnectionError(f"intestazione frame non valida {header.hex()}")
    size = header[3] | header[4] << 8
    if len(rest) != size + 3 or rest[-1] != FRAME_END:
        raise C3ConnectionError("fine frame non valida")
    payload = rest[:size]
    if crc16(header[1:] + payload) != (rest[size] | rest[size + 1] << 8):
        raise C3ConnectionError("CRC frame errato")
    return header[2], payload


def parse_kv(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in text.replace("\t", ",").replace("\r", ",").replace("\n", ",").split(","):
        key, sep, value = part.partition("=")
        if sep and key.strip():
            out[key.strip()] = value.strip().rstrip("\x00")
    return out


def encode_datetime(value: datetime) -> int:
    """Formato orario dei pannelli (secondi in un calendario 12x31 dal 2000)."""
    return (
        ((value.year - 2000) * 12 * 31 + (value.month - 1) * 31 + (value.day - 1)) * 86400
        + value.hour * 3600 + value.minute * 60 + value.second
    )


def decode_datetime(value: int) -> datetime | None:
    second = value % 60
    value //= 60
    minute = value % 60
    value //= 60
    hour = value % 24
    value //= 24
    day = value % 31 + 1
    value //= 31
    month = value % 12 + 1
    year = value // 12 + 2000
    try:
        return datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None


def encode_tz_interval(start: str, end: str) -> int:
    """'08:00','18:30' -> intero fascia oraria (inizio<<16 | fine) in HHMM."""
    def hhmm(t: str) -> int:
        t = (t or "00:00").strip()
        h, _, m = t.partition(":")
        return int(h or 0) * 100 + int(m or 0)
    return (hhmm(start) << 16) | hhmm(end)


def decode_tz_interval(value: int) -> tuple[str, str]:
    s, e = value >> 16, value & 0xFFFF
    return f"{s // 100:02d}:{s % 100:02d}", f"{e // 100:02d}:{e % 100:02d}"


# --------------------------------------------------------- tabelle dati

@dataclass
class TableField:
    name: str
    type: str  # i, s, S, b, B
    index: int


@dataclass
class TableCfg:
    name: str
    index: int
    fields: list[TableField] = field(default_factory=list)

    def field(self, name: str) -> TableField:
        for f in self.fields:
            if f.name.lower() == name.lower():
                return f
        raise KeyError(f"campo '{name}' non presente nella tabella {self.name}")

    def has(self, name: str) -> bool:
        return any(f.name.lower() == name.lower() for f in self.fields)


def parse_table_cfg(body: bytes) -> dict[str, TableCfg]:
    tables: dict[str, TableCfg] = {}
    for line in body.split(b"\n"):
        text = line.decode("ascii", "ignore").strip().strip("\x00")
        if "=" not in text:
            continue
        parts = [p for p in text.split(",") if "=" in p]
        name, _, idx = parts[0].partition("=")
        try:
            cfg = TableCfg(name.strip(), int(idx))
        except ValueError:
            continue
        for p in parts[1:]:
            fname, _, tdef = p.partition("=")
            tdef = tdef.strip()
            if len(tdef) >= 2 and tdef[1:].isdigit():
                cfg.fields.append(TableField(fname.strip(), tdef[0], int(tdef[1:])))
        tables[cfg.name.lower()] = cfg
    return tables


def _encode_value(ftype: str, value: Any) -> bytes:
    if ftype == "i":
        v = int(value or 0) & 0xFFFFFFFF
        if v == 0:
            return b"\x00"
        n = 4 if v > 0xFFFFFF else 3 if v > 0xFFFF else 2 if v > 0xFF else 1
        return bytes([n]) + v.to_bytes(n, "little")
    raw = value if isinstance(value, (bytes, bytearray)) else str(value if value is not None else "").encode("ascii", "ignore")
    if ftype in ("s", "b"):
        raw = raw[:255]
        return bytes([len(raw)]) + raw
    raw = raw[:0xFFFF]
    return len(raw).to_bytes(2, "little") + raw


def encode_records(cfg: TableCfg, records: list[dict[str, Any]]) -> bytes:
    """Codifica record per SetDeviceData / DeleteDeviceData."""
    names: list[str] = []
    for rec in records:
        for k in rec:
            if k not in names:
                names.append(k)
    flds = sorted((cfg.field(n) for n in names), key=lambda f: f.index)
    out = bytearray([cfg.index, len(flds)]) + bytes(f.index for f in flds)
    for rec in records:
        lower = {k.lower(): v for k, v in rec.items()}
        for f in flds:
            out += _encode_value(f.type, lower.get(f.name.lower(), 0 if f.type == "i" else ""))
    return bytes(out)


def decode_records(cfg: TableCfg, body: bytes) -> list[dict[str, Any]]:
    if not body:
        return []
    if body[0] != cfg.index:
        raise C3ConnectionError(f"tabella attesa {cfg.index}, ricevuta {body[0]}")
    count = body[1]
    idxs = list(body[2:2 + count])
    by_idx = {f.index: f for f in cfg.fields}
    data = body[2 + count:]
    pos = 0
    rows: list[dict[str, Any]] = []
    while pos < len(data):
        row: dict[str, Any] = {}
        for i in idxs:
            f = by_idx.get(i)
            ftype = f.type if f else "i"
            if ftype in ("S", "B"):
                size = int.from_bytes(data[pos:pos + 2], "little")
                pos += 2
            else:
                size = data[pos]
                pos += 1
            raw = data[pos:pos + size]
            pos += size
            name = f.name if f else f"field{i}"
            if ftype == "i":
                row[name] = int.from_bytes(raw, "little") if raw else 0
            elif ftype in ("s", "S"):
                row[name] = raw.split(b"\x00", 1)[0].decode("latin-1")
            else:
                row[name] = raw.hex()
        rows.append(row)
    return rows


# --------------------------------------------------------------- RT log

@dataclass
class PanelEvent:
    time: datetime | None
    door: int
    event: int
    card: int = 0
    pin: str = ""
    verify: int = 0
    inout: int = 2


@dataclass
class PanelStatus:
    time: datetime | None
    sensors: list[int]  # per porta: 0 nessun sensore, 1 chiusa, 2 aperta
    alarms: list[int]   # per porta: bit1 allarme, bit2 apertura troppo lunga
    relay: int | None = None


@dataclass
class RtLog:
    events: list[PanelEvent] = field(default_factory=list)
    status: PanelStatus | None = None


def _ptime(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def parse_rtlog_kv(text: str) -> RtLog:
    log = RtLog()
    for line in text.replace("\r", "\n").split("\n"):
        kv = parse_kv(line)
        if not kv:
            continue
        if "sensor" in kv:
            try:
                sensor = int(kv.get("sensor") or "0", 16)
            except ValueError:
                sensor = 0
            try:
                alarm = bytes.fromhex(kv.get("alarm", "") or "")
            except ValueError:
                alarm = b""
            relay = None
            with contextlib.suppress(ValueError):
                relay = int(kv["relay"], 16) if kv.get("relay") else None
            log.status = PanelStatus(
                _ptime(kv.get("time")),
                [(sensor >> (2 * i)) & 3 for i in range(4)],
                list(alarm[:4].ljust(4, b"\x00")),
                relay,
            )
        elif "event" in kv:
            def num(k: str, d: int = 0, kv: dict = kv) -> int:
                try:
                    return int(kv.get(k, d))
                except ValueError:
                    return d
            log.events.append(PanelEvent(
                _ptime(kv.get("time")), num("eventaddr"), num("event"), num("cardno"),
                kv.get("pin", "") if kv.get("pin", "0") != "0" else "", num("verifytype"), num("inoutstatus", 2),
            ))
    return log


def parse_rtlog_binary(data: bytes) -> RtLog:
    log = RtLog()
    if not data:
        return log
    if len(data) % 16 == 0 and all(
        decode_datetime(int.from_bytes(data[o + 12:o + 16], "little")) for o in range(0, len(data), 16)
    ):
        size = 16
    elif len(data) % 36 == 0:
        size = 36
    elif len(data) % 16 == 0:
        size = 16
    else:
        raise ValueError("formato RT log binario sconosciuto")
    for off in range(0, len(data), size):
        rec = data[off:off + size]
        if size == 36:
            if rec[30] == EVENT_STATUS:
                continue
            pin = rec[4:28].split(b"\x00", 1)[0].decode("ascii", "ignore")
            log.events.append(PanelEvent(
                decode_datetime(int.from_bytes(rec[32:36], "little")), rec[29], rec[30],
                int.from_bytes(rec[0:4], "little"), "" if pin in ("", "0") else pin, rec[28], rec[31],
            ))
            continue
        when = decode_datetime(int.from_bytes(rec[12:16], "little"))
        if rec[10] == EVENT_STATUS:
            # byte 0-3 allarmi per porta, byte 4-7 stato sensore porta
            log.status = PanelStatus(when, [b & 0x0F for b in rec[4:8]], list(rec[0:4]))
        else:
            pin = int.from_bytes(rec[4:8], "little")
            log.events.append(PanelEvent(
                when, rec[9], rec[10], int.from_bytes(rec[0:4], "little"),
                str(pin) if pin else "", rec[8], rec[11],
            ))
    return log


# --------------------------------------------------------------- client

class C3Client:
    """Una connessione TCP / sessione verso un pannello. Richieste serializzate."""

    def __init__(self, host: str, port: int = DEFAULT_PORT, password: str = "", timeout: float = 4.0) -> None:
        self.host = host
        self.port = int(port or DEFAULT_PORT)
        self.password = password or ""
        self.timeout = timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._session: int | None = None
        self._req = INITIAL_REQUEST
        self._lock = asyncio.Lock()
        self._rtlog_kv = False
        self._tables: dict[str, TableCfg] | None = None
        self.session_less = False

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def _open(self) -> None:
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), self.timeout)
        except (OSError, asyncio.TimeoutError) as err:
            raise C3ConnectionError(f"impossibile connettersi a {self.host}:{self.port}: {err}") from err

    async def _close(self) -> None:
        writer, self._writer, self._reader = self._writer, None, None
        self._session = None
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), 2)

    async def _exchange(self, command: int, data: bytes = b"") -> bytes:
        if self._writer is None or self._reader is None:
            raise C3ConnectionError("non connesso")
        if self._session is not None:
            payload = self._session.to_bytes(2, "little") + self._req.to_bytes(2, "little") + data
            self._req = (self._req + 1) & 0xFFFF
        else:
            payload = data
        if len(payload) > MAX_PAYLOAD:
            raise C3Error("payload troppo grande")
        frame = build_frame(command, payload)
        _LOGGER.debug("%s TX %s", self.host, frame.hex())
        try:
            self._writer.write(frame)
            await asyncio.wait_for(self._writer.drain(), self.timeout)
            header = await asyncio.wait_for(self._reader.readexactly(5), self.timeout)
            if header[0] != FRAME_START:
                raise C3ConnectionError(f"intestazione non valida {header.hex()}")
            size = header[3] | header[4] << 8
            rest = await asyncio.wait_for(self._reader.readexactly(size + 3), self.timeout)
        except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError) as err:
            raise C3ConnectionError(f"{type(err).__name__}: {err}") from err
        _LOGGER.debug("%s RX %s", self.host, (header + rest).hex())
        reply, body = parse_frame(header, rest)
        if self._session is not None and command != CMD_CONNECT_SESSION:
            if len(body) < 4:
                raise C3ConnectionError("risposta senza intestazione di sessione")
            if int.from_bytes(body[0:2], "little") != self._session:
                raise C3ConnectionError("risposta di un'altra sessione")
            body = body[4:]
        if reply == REPLY_ERROR:
            code = body[-1] if body else 0
            raise C3CommandError(command, code - 256 if code > 127 else code)
        if reply != REPLY_OK:
            raise C3ConnectionError(f"codice risposta inatteso 0x{reply:02x}")
        return body

    async def connect(self) -> None:
        async with self._lock:
            await self._connect_locked()

    async def _connect_locked(self) -> None:
        await self._close()
        pwd = self.password.encode("ascii", "ignore")
        await self._open()
        self._session, self._req = INITIAL_SESSION, INITIAL_REQUEST
        body: bytes | None
        try:
            body = await self._exchange(CMD_CONNECT_SESSION, pwd)
        except C3CommandError as err:
            await self._close()
            if err.code == -14:
                raise C3AuthError("password di comunicazione errata") from err
            body = None
        except C3Error:
            await self._close()
            body = None
        if body is not None and len(body) >= 2:
            self._session = int.from_bytes(body[0:2], "little")
            self.session_less = False
            return
        await self._close()
        await self._open()
        self._session = None
        try:
            await self._exchange(CMD_CONNECT, pwd)
        except C3CommandError as err:
            await self._close()
            if err.code == -14:
                raise C3AuthError("password di comunicazione errata") from err
            raise
        except C3Error:
            await self._close()
            raise
        self.session_less = True

    async def disconnect(self) -> None:
        async with self._lock:
            if self.connected:
                with contextlib.suppress(C3Error):
                    saved, self.timeout = self.timeout, min(self.timeout, 1.5)
                    try:
                        await self._exchange(CMD_DISCONNECT)
                    finally:
                        self.timeout = saved
            await self._close()

    async def request(self, command: int, data: bytes = b"") -> bytes:
        async with self._lock:
            if not self.connected:
                await self._connect_locked()
            try:
                return await self._exchange(command, data)
            except C3ConnectionError:
                await self._close()
                raise

    # -------------------------------------------------------- parametri
    async def get_params(self, names: Iterable[str]) -> dict[str, str]:
        names = list(names)
        out: dict[str, str] = {}
        for i in range(0, len(names), 8):
            body = await self.request(CMD_GET_PARAM, ",".join(names[i:i + 8]).encode())
            out.update(parse_kv(body.decode("ascii", "ignore")))
        return out

    async def set_params(self, params: dict[str, Any]) -> None:
        items = list(params.items())
        for i in range(0, len(items), 8):
            text = ",".join(f"{k}={v}" for k, v in items[i:i + 8])
            await self.request(CMD_SET_PARAM, text.encode("ascii", "ignore"))

    async def set_time(self, value: datetime | None = None) -> None:
        await self.set_params({"DateTime": encode_datetime(value or datetime.now())})

    async def info(self) -> dict[str, str]:
        return await self.get_params([
            "~SerialNumber", "FirmVer", "~DeviceName", "LockCount", "ReaderCount",
            "AuxInCount", "AuxOutCount", "IPAddress", "NetMask", "GATEIPAddress",
            "DateTime", "~MaxUserCount", "MachineType", "AntiPassback", "InterLock",
        ])

    # ---------------------------------------------------------- comandi
    async def control(self, op: int, p1: int = 0, p2: int = 0, p3: int = 0, p4: int = 0) -> None:
        await self.request(CMD_CONTROL, bytes([op, p1 & 0xFF, p2 & 0xFF, p3 & 0xFF, p4 & 0xFF]))

    async def open_door(self, door: int, seconds: int = 5) -> None:
        await self.control(OP_OUTPUT, door, ADDR_DOOR, max(1, min(254, int(seconds))))

    async def close_door(self, door: int) -> None:
        await self.control(OP_OUTPUT, door, ADDR_DOOR, 0)

    async def hold_door_open(self, door: int) -> None:
        await self.control(OP_OUTPUT, door, ADDR_DOOR, 255)

    async def set_aux(self, aux: int, seconds: int) -> None:
        await self.control(OP_OUTPUT, aux, ADDR_AUX, max(0, min(255, int(seconds))))

    async def cancel_alarm(self) -> None:
        await self.control(OP_CANCEL_ALARM)

    async def restart(self) -> None:
        await self.control(OP_RESTART)

    async def normal_open(self, door: int, enable: bool) -> None:
        await self.control(OP_NORMAL_OPEN, door, 1 if enable else 0)

    async def rtlog(self) -> RtLog:
        if not self._rtlog_kv:
            body = await self.request(CMD_RTLOG_BIN)
            try:
                return parse_rtlog_binary(body)
            except ValueError:
                self._rtlog_kv = True
        body = await self.request(CMD_RTLOG_KV)
        return parse_rtlog_kv(body.decode("ascii", "ignore"))

    # ----------------------------------------------------------- tabelle
    async def tables(self, refresh: bool = False) -> dict[str, TableCfg]:
        if self._tables is None or refresh:
            self._tables = parse_table_cfg(await self.request(CMD_TABLE_CFG))
        return self._tables

    async def table(self, name: str) -> TableCfg:
        t = (await self.tables()).get(name.lower())
        if t is None:
            raise C3Error(f"tabella '{name}' non supportata dal pannello")
        return t

    async def get_data(self, name: str, fields: list[str] | None = None) -> list[dict[str, Any]]:
        cfg = await self.table(name)
        flds = sorted((cfg.field(f) for f in fields), key=lambda f: f.index) if fields else cfg.fields
        req = bytes([cfg.index, len(flds)]) + bytes(f.index for f in flds) + b"\x00\x00"
        return decode_records(cfg, await self.request(CMD_GET_DATA, req))

    async def set_data(self, name: str, records: list[dict[str, Any]], batch: int = 20) -> None:
        cfg = await self.table(name)
        for i in range(0, len(records), batch):
            await self.request(CMD_SET_DATA, encode_records(cfg, records[i:i + batch]))

    async def delete_data(self, name: str, conditions: list[dict[str, Any]], batch: int = 20) -> None:
        """conditions: es. [{"Pin": "101"}]; [{}] o [] non cancella nulla."""
        cfg = await self.table(name)
        conditions = [c for c in conditions if c]
        for i in range(0, len(conditions), batch):
            try:
                await self.request(CMD_DELETE_DATA, encode_records(cfg, conditions[i:i + batch]))
            except C3CommandError as err:
                if err.code != -6:  # "dato non trovato" non è un errore
                    raise
