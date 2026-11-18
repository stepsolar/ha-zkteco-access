"""Simulatore di un pannello C3-400 (per test e sviluppo senza hardware).

Avvio manuale:  python tests/sim_panel.py 4370
"""
from __future__ import annotations

import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "zkteco_access" / "app"))
import c3  # noqa: E402

# risposta reale "struttura tabelle" di un C3-400 (fixture del progetto zkaccess-c3)
_REF = pathlib.Path(__file__).resolve().parent / "fixtures" / "c3_table_cfg.hex"
if _REF.exists():
    _full = bytes.fromhex(_REF.read_text().strip())
    CFG_BODY = c3.parse_frame(_full[:5], _full[5:])[1][4:]
else:  # struttura minima
    CFG_BODY = (b"user=1,CardNo=i1,Pin=i2,Password=s3,Group=i4,StartTime=i5,EndTime=i6,SuperAuthorize=i7\n"
                b"userauthorize=2,Pin=i1,AuthorizeTimezoneId=i2,AuthorizeDoorId=i3\n"
                b"holiday=3,Holiday=i1,HolidayType=i2,Loop=i3\n"
                b"timezone=4,TimezoneId=i1,SunTime1=i2\n")


class SimPanel:
    def __init__(self, password: str = "", session: bool = True) -> None:
        self.password = password
        self.session_mode = session
        self.tables = c3.parse_table_cfg(CFG_BODY)
        self.data: dict[str, list[dict]] = {n: [] for n in self.tables}
        self.params = {"~SerialNumber": "SIM0001", "FirmVer": "AC Ver 4.3.4 Apr 27 2017", "LockCount": "4",
                       "ReaderCount": "4", "AuxInCount": "4", "AuxOutCount": "4", "IPAddress": "127.0.0.1"}
        for n in range(1, 5):
            self.params.update({f"Door{n}Drivertime": "5", f"Door{n}SensorType": "2", f"Door{n}Detectortime": "15"})
        self.controls: list[bytes] = []
        self.pending: list[bytes] = []
        self.sensors = [1, 1, 0, 0]
        self.alarms = [0, 0, 0, 0]
        self.server: asyncio.base_events.Server | None = None
        self.writers: set = set()

    def push_event(self, card: int, pin: int, door: int, event: int, verify: int = 4, inout: int = 0) -> None:
        from datetime import datetime
        t = c3.encode_datetime(datetime.now())
        self.pending.append(card.to_bytes(4, "little") + pin.to_bytes(4, "little") + bytes([verify, door, event, inout]) + t.to_bytes(4, "little"))

    async def start(self, port: int = 0) -> int:
        self.server = await asyncio.start_server(self._client, "127.0.0.1", port)
        return self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.server:
            self.server.close()
            for w in list(self.writers):
                w.close()
            await asyncio.wait_for(self.server.wait_closed(), 3)

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        session = None
        self.writers.add(writer)
        try:
            while True:
                hdr = await reader.readexactly(5)
                size = hdr[3] | hdr[4] << 8
                rest = await reader.readexactly(size + 3)
                cmd, body = c3.parse_frame(hdr, rest)
                if cmd == c3.CMD_CONNECT_SESSION or session is not None:
                    body = body[4:]
                code, out = self.handle(cmd, body)
                if cmd == c3.CMD_CONNECT_SESSION and code == c3.REPLY_OK:
                    session = 0x4AC7
                    out = session.to_bytes(2, "little") + b"\x01\x00"
                elif session is not None:
                    out = session.to_bytes(2, "little") + b"\x00\x00" + out
                writer.write(c3.build_frame(code, out))
                await writer.drain()
                if cmd == c3.CMD_DISCONNECT:
                    break
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self.writers.discard(writer)
            writer.close()

    def handle(self, cmd: int, body: bytes) -> tuple[int, bytes]:
        err = lambda code: (c3.REPLY_ERROR, bytes([code & 0xFF]))  # noqa: E731
        if cmd == c3.CMD_CONNECT_SESSION:
            if not self.session_mode:
                return err(-13)
            return (c3.REPLY_OK, b"") if body.decode() == self.password else err(-14)
        if cmd == c3.CMD_CONNECT:
            return (c3.REPLY_OK, b"") if body.decode() == self.password else err(-14)
        if cmd == c3.CMD_DISCONNECT:
            return c3.REPLY_OK, b""
        if cmd == c3.CMD_GET_PARAM:
            names = body.decode().split(",")
            return c3.REPLY_OK, ",".join(f"{n}={self.params[n]}" for n in names if n in self.params).encode()
        if cmd == c3.CMD_SET_PARAM:
            self.params.update(c3.parse_kv(body.decode()))
            return c3.REPLY_OK, b""
        if cmd == c3.CMD_CONTROL:
            self.controls.append(body)
            if body[0] == c3.OP_OUTPUT and body[2] == c3.ADDR_DOOR:
                self.push_event(0, 0, body[1], 8 if body[3] else 9, verify=200, inout=2)
            return c3.REPLY_OK, b""
        if cmd == c3.CMD_TABLE_CFG:
            return c3.REPLY_OK, CFG_BODY
        if cmd in (c3.CMD_GET_DATA, c3.CMD_SET_DATA, c3.CMD_DELETE_DATA):
            cfg = next(t for t in self.tables.values() if t.index == body[0])
            store = self.data[cfg.name.lower()]
            if cmd == c3.CMD_GET_DATA:
                n = body[1]
                idx = list(body[2:2 + n])
                flds = [f for f in cfg.fields if f.index in idx]
                rows = [{f.name: r.get(f.name, 0 if f.type == "i" else "") for f in flds} for r in store]
                enc = c3.encode_records(c3.TableCfg(cfg.name, cfg.index, flds), rows) if rows else bytes([cfg.index, len(flds)]) + bytes(idx)
                return c3.REPLY_OK, enc
            recs = c3.decode_records(cfg, body)
            if cmd == c3.CMD_SET_DATA:
                for r in recs:
                    key = ("Pin", "AuthorizeTimezoneId") if cfg.name == "userauthorize" else \
                          ("TimezoneId",) if cfg.name == "timezone" else ("Holiday",) if cfg.name == "holiday" else ("Pin",)
                    store[:] = [x for x in store if any(x.get(k) != r.get(k) for k in key)]
                    store.append(r)
                return c3.REPLY_OK, b""
            before = len(store)
            for r in recs:
                store[:] = [x for x in store if any(x.get(k) != v for k, v in r.items())]
            return (c3.REPLY_OK, b"") if len(store) != before or not recs else err(-6)
        if cmd == c3.CMD_RTLOG_BIN:
            status = bytes(self.alarms) + bytes(self.sensors) + bytes([0, 0, 255, 0]) + (0x21ADADA5).to_bytes(4, "little")
            out = b"".join(self.pending) + status
            self.pending.clear()
            return c3.REPLY_OK, out
        return err(-13)


if __name__ == "__main__":
    async def main() -> None:
        sim = SimPanel(password=sys.argv[2] if len(sys.argv) > 2 else "")
        port = await sim.start(int(sys.argv[1]) if len(sys.argv) > 1 else 4370)
        print("Simulatore C3 in ascolto su", port)
        while True:
            await asyncio.sleep(10)
            sim.push_event(1234567, 0, 1, 27)
    asyncio.run(main())
