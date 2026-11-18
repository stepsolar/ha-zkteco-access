"""Gestione pannelli: polling realtime, comandi, sincronizzazione utenti/tessere."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable

import c3
from events_it import (AUX_EVENTS, INOUT, LOCK_RELEASED, NORMAL_OPEN_END,
                       NORMAL_OPEN_START, VERIFY, describe)
from store import Store

_LOGGER = logging.getLogger("zkteco.manager")

DAYS = ("sun", "mon", "tue", "wed", "thu", "fri", "sat", "hol1", "hol2", "hol3")
TZ_FIELD = {"sun": "Sun", "mon": "Mon", "tue": "Tue", "wed": "Wed", "thu": "Thu", "fri": "Fri",
            "sat": "Sat", "hol1": "Hol1", "hol2": "Hol2", "hol3": "Hol3"}

DOOR_PARAMS = ("Drivertime", "Detectortime", "SensorType", "VerifyType", "Intertime",
               "KeepOpenTimeZone", "CloseAndLock", "SupperPassWord", "ForcePassWord", "ValidTZ")


@dataclass
class DoorState:
    sensor: int = 0          # 0 sconosciuto/assente, 1 chiusa, 2 aperta
    alarm: int = 0
    unlocked_until: float = 0
    normal_open: bool = False
    last_event: dict | None = None

    def as_dict(self) -> dict:
        return {"sensor": self.sensor, "alarm": self.alarm, "unlocked": self.unlocked_until > time.time() or self.normal_open,
                "normal_open": self.normal_open, "last_event": self.last_event}


@dataclass
class PanelRuntime:
    id: int
    client: c3.C3Client
    online: bool = False
    error: str = ""
    doors: dict[int, DoorState] = field(default_factory=dict)
    aux_in: dict[int, int] = field(default_factory=dict)   # 1 = aperto/attivo, 0 = chiuso
    aux_out: dict[int, int] = field(default_factory=dict)
    task: asyncio.Task | None = None
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    info: dict = field(default_factory=dict)


def ymd(value: str | None) -> int:
    if not value:
        return 0
    try:
        return int(datetime.strptime(value[:10], "%Y-%m-%d").strftime("%Y%m%d"))
    except ValueError:
        return 0


def panel_to_iso(value: int) -> str:
    if not value:
        return ""
    s = str(value)
    if len(s) == 8:
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    d = c3.decode_datetime(value)
    return d.strftime("%Y-%m-%d") if d else ""


class Manager:
    def __init__(self, store: Store, options: dict) -> None:
        self.store = store
        self.options = options
        self.panels: dict[int, PanelRuntime] = {}
        self._listeners: list[Callable[[str, dict], Any]] = []
        self._learners: list[tuple[asyncio.Future, int | None]] = []
        self.poll_interval = float(options.get("poll_interval", 1.0))
        self.sync_status: dict[str, Any] = {"running": False, "message": "", "progress": 0}

    # ------------------------------------------------------------ eventi
    def subscribe(self, cb: Callable[[str, dict], Any]) -> Callable[[], None]:
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb) if cb in self._listeners else None

    def emit(self, kind: str, data: dict) -> None:
        for cb in list(self._listeners):
            try:
                res = cb(kind, data)
                if asyncio.iscoroutine(res):
                    asyncio.ensure_future(res)
            except Exception:  # pragma: no cover
                _LOGGER.exception("listener error")

    # ------------------------------------------------------------ avvio
    async def start(self) -> None:
        for p in self.store.panels():
            self._start_panel(p)

    async def stop(self) -> None:
        for rt in list(self.panels.values()):
            await self._stop_panel(rt.id)

    def _start_panel(self, p: dict) -> None:
        if not p["enabled"]:
            return
        client = c3.C3Client(p["host"], p["port"], p["password"], timeout=float(self.options.get("timeout", 4)))
        rt = PanelRuntime(p["id"], client)
        self.panels[p["id"]] = rt
        rt.task = asyncio.ensure_future(self._run(rt))

    async def _stop_panel(self, pid: int) -> None:
        rt = self.panels.pop(pid, None)
        if rt:
            if rt.task:
                rt.task.cancel()
                try:
                    await rt.task
                except (asyncio.CancelledError, Exception):
                    pass
            await rt.client.disconnect()

    async def reload_panel(self, pid: int) -> None:
        await self._stop_panel(pid)
        p = self.store.panel(pid)
        if p:
            self._start_panel(p)
        self.emit("panels", {})

    async def reload_all(self) -> None:
        await self.stop()
        await self.start()
        self.emit("panels", {})

    def rt(self, pid: int) -> PanelRuntime:
        rt = self.panels.get(int(pid))
        if not rt:
            raise RuntimeError("Dispositivo disattivato o inesistente")
        return rt

    # --------------------------------------------------------- polling
    async def _run(self, rt: PanelRuntime) -> None:
        backoff = 3.0
        while True:
            try:
                if not rt.client.connected:
                    await rt.client.connect()
                    await self._on_connect(rt)
                    backoff = 3.0
                log = await rt.client.rtlog()
                self._process_rtlog(rt, log)
                if self.store.queued(rt.id):
                    await self._flush_queue(rt)
                try:
                    await asyncio.wait_for(rt.wake.wait(), self.poll_interval)
                except asyncio.TimeoutError:
                    pass
                rt.wake.clear()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                was = rt.online
                rt.online, rt.error = False, str(err)
                if was:
                    _LOGGER.warning("Pannello %s offline: %s", rt.id, err)
                    self.emit("panel", self.panel_state(rt.id))
                await rt.client.disconnect()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _on_connect(self, rt: PanelRuntime) -> None:
        info: dict = {}
        try:
            info = await rt.client.info()
        except c3.C3Error as err:
            _LOGGER.debug("info pannello %s: %s", rt.id, err)
        rt.info = info
        kw: dict[str, Any] = {"last_seen": time.time()}
        if info.get("~SerialNumber"):
            kw["serial"] = info["~SerialNumber"]
        if info.get("FirmVer"):
            kw["firmware"] = info["FirmVer"]
        for key, col in (("LockCount", "locks"), ("AuxInCount", "aux_in"), ("AuxOutCount", "aux_out")):
            if str(info.get(key, "")).isdigit():
                kw[col] = int(info[key])
        if info.get("~DeviceName"):
            kw["model"] = info["~DeviceName"]
        self.store.update_panel_info(rt.id, **kw)
        p = self.store.panel(rt.id)
        before = self.store.q1("SELECT COUNT(*) n FROM doors WHERE panel_id=?", (rt.id,))["n"]
        self.store.ensure_ports(rt.id, p["locks"], p["aux_in"], p["aux_out"])
        if self.store.q1("SELECT COUNT(*) n FROM doors WHERE panel_id=?", (rt.id,))["n"] != before or kw.keys() & {"serial", "firmware", "model"}:
            self.emit("panels", {})
        if self.options.get("sync_time_on_connect", True):
            try:
                await rt.client.set_time()
            except c3.C3Error as err:
                _LOGGER.debug("set time %s: %s", rt.id, err)
        rt.online, rt.error = True, ""
        _LOGGER.info("Connesso a %s (%s) fw %s", p["name"], p["host"], p.get("firmware"))
        self.emit("panel", self.panel_state(rt.id))

    def _process_rtlog(self, rt: PanelRuntime, log: c3.RtLog) -> None:
        changed = False
        if log.status:
            for i, s in enumerate(log.status.sensors, start=1):
                ds = rt.doors.setdefault(i, DoorState())
                a = log.status.alarms[i - 1] if i - 1 < len(log.status.alarms) else 0
                if ds.sensor != s or ds.alarm != a:
                    ds.sensor, ds.alarm = s, a
                    changed = True
        for ev in log.events:
            self._handle_event(rt, ev)
            changed = True
        now = time.time()
        for ds in rt.doors.values():
            if ds.unlocked_until and ds.unlocked_until <= now:
                ds.unlocked_until = 0
                changed = True
        if changed:
            self.emit("panel", self.panel_state(rt.id))

    def _handle_event(self, rt: PanelRuntime, ev: c3.PanelEvent) -> None:
        p = self.store.panel(rt.id) or {"name": "?"}
        desc, cat = describe(ev.event)
        is_aux = ev.event in AUX_EVENTS
        point = ""
        if is_aux:
            kind = "in" if ev.event in (220, 221) else "out"
            a = self.store.q1("SELECT name FROM aux WHERE panel_id=? AND kind=? AND aux_no=?", (rt.id, kind, ev.door))
            point = a["name"] if a else f"Aux {ev.door}"
            if kind == "in":
                rt.aux_in[ev.door] = 1 if ev.event == 220 else 0
            else:
                rt.aux_out[ev.door] = 1 if ev.event == 12 else 0
        else:
            d = self.store.q1("SELECT name, open_seconds FROM doors WHERE panel_id=? AND door_no=?", (rt.id, ev.door))
            point = d["name"] if d else (f"Porta {ev.door}" if ev.door else p["name"])
        user = None
        card = str(ev.card) if ev.card else ""
        if ev.pin:
            user = self.store.user(pin=ev.pin)
        if not user and card:
            user = self.store.user(card=card)
        uname = ""
        if user:
            uname = user["name"] or f"Utente {user['pin']}"
            if user["grp"]:
                uname = f"{uname} ({user['grp']})" if user["name"] else f"{user['grp']} #{user['pin']}"
        rec = {
            "ts": time.time(), "panel_id": rt.id, "panel": p["name"], "door_no": ev.door, "door": point,
            "event": ev.event, "description": desc, "card": card, "pin": ev.pin or (user["pin"] if user else ""),
            "user": uname, "inout": ev.inout, "verify": ev.verify, "category": cat,
            "panel_time": ev.time.isoformat(sep=" ") if ev.time else "",
            "verify_text": VERIFY.get(ev.verify, ""), "inout_text": INOUT.get(ev.inout, ""), "aux": is_aux,
        }
        rec["id"] = self.store.add_event(rec)
        if not is_aux and ev.door:
            ds = rt.doors.setdefault(ev.door, DoorState())
            ds.last_event = {k: rec[k] for k in ("ts", "description", "user", "card", "category")}
            if ev.event in LOCK_RELEASED:
                d = self.store.q1("SELECT open_seconds FROM doors WHERE panel_id=? AND door_no=?", (rt.id, ev.door))
                ds.unlocked_until = time.time() + (d["open_seconds"] if d else 5)
            if ev.event in NORMAL_OPEN_START:
                ds.normal_open = True
            if ev.event in NORMAL_OPEN_END:
                ds.normal_open = False
                ds.unlocked_until = 0
            if ev.event == 201:
                ds.sensor = 1
            elif ev.event == 200:
                ds.sensor = 2
        self.emit("event", rec)
        if card and ev.event not in (200, 201):
            for fut, door_filter in list(self._learners):
                if not fut.done() and (door_filter is None or door_filter == rt.id):
                    fut.set_result({"card": card, "panel": p["name"], "door": point, "event": desc,
                                    "known_user": user["pin"] if user else None})

    # ------------------------------------------------------------ stato
    def panel_state(self, pid: int) -> dict:
        p = self.store.panel(pid) or {}
        rt = self.panels.get(pid)
        doors = {}
        for n in range(1, (p.get("locks") or 4) + 1):
            doors[n] = (rt.doors[n] if rt and n in rt.doors else DoorState()).as_dict()
        return {
            "id": pid, "name": p.get("name"), "host": p.get("host"), "enabled": bool(p.get("enabled")),
            "online": bool(rt and rt.online), "error": rt.error if rt else "disattivato",
            "doors": doors, "aux_in": dict(rt.aux_in) if rt else {}, "aux_out": dict(rt.aux_out) if rt else {},
            "queue": len(self.store.queued(pid)), "info": rt.info if rt else {},
        }

    def full_state(self) -> dict:
        return {"panels": [self.panel_state(p["id"]) for p in self.store.panels()], "sync": self.sync_status}

    # --------------------------------------------------------- comandi
    async def open_door(self, door_id: int, seconds: int | None = None) -> None:
        d = self.store.door(door_id)
        if not d:
            raise ValueError("Porta inesistente")
        rt = self.rt(d["panel_id"])
        sec = int(seconds or d["open_seconds"] or 5)
        await rt.client.open_door(d["door_no"], sec)
        ds = rt.doors.setdefault(d["door_no"], DoorState())
        ds.unlocked_until = time.time() + sec
        rt.wake.set()
        self.emit("panel", self.panel_state(rt.id))

    async def close_door(self, door_id: int) -> None:
        d = self.store.door(door_id)
        rt = self.rt(d["panel_id"])
        await rt.client.close_door(d["door_no"])
        ds = rt.doors.setdefault(d["door_no"], DoorState())
        ds.unlocked_until = 0
        rt.wake.set()
        self.emit("panel", self.panel_state(rt.id))

    async def normal_open(self, door_id: int, enable: bool) -> None:
        d = self.store.door(door_id)
        rt = self.rt(d["panel_id"])
        if enable:
            await rt.client.hold_door_open(d["door_no"])
        else:
            await rt.client.normal_open(d["door_no"], False)
            await rt.client.close_door(d["door_no"])
        ds = rt.doors.setdefault(d["door_no"], DoorState())
        ds.normal_open = enable
        if not enable:
            ds.unlocked_until = 0
        rt.wake.set()
        self.emit("panel", self.panel_state(rt.id))

    async def aux_output(self, aux_id: int, seconds: int) -> None:
        a = self.store.q1("SELECT * FROM aux WHERE id=? AND kind='out'", (aux_id,))
        if not a:
            raise ValueError("Uscita ausiliaria inesistente")
        rt = self.rt(a["panel_id"])
        await rt.client.set_aux(a["aux_no"], seconds)
        rt.aux_out[a["aux_no"]] = 1 if seconds else 0
        rt.wake.set()

    async def panel_action(self, pid: int, action: str) -> Any:
        rt = self.rt(pid)
        if action == "cancel_alarm":
            await rt.client.cancel_alarm()
            for ds in rt.doors.values():
                ds.alarm = 0
        elif action == "restart":
            await rt.client.restart()
        elif action == "sync_time":
            await rt.client.set_time()
        elif action == "info":
            rt.info = await rt.client.info()
            return rt.info
        elif action == "reconnect":
            await rt.client.disconnect()
        else:
            raise ValueError("Azione sconosciuta")
        rt.wake.set()
        self.emit("panel", self.panel_state(pid))
        return True

    async def test_connection(self, host: str, port: int, password: str) -> dict:
        cl = c3.C3Client(host, port, password, timeout=4)
        try:
            await cl.connect()
            info = await cl.info()
            info["_session_less"] = cl.session_less
            return info
        finally:
            await cl.disconnect()

    # ------------------------------------------------- parametri porta
    async def get_door_params(self, door_id: int) -> dict:
        d = self.store.door(door_id)
        rt = self.rt(d["panel_id"])
        n = d["door_no"]
        vals = await rt.client.get_params([f"Door{n}{k}" for k in DOOR_PARAMS])
        return {k: vals.get(f"Door{n}{k}") for k in DOOR_PARAMS}

    async def set_door_params(self, door_id: int, params: dict) -> None:
        d = self.store.door(door_id)
        rt = self.rt(d["panel_id"])
        n = d["door_no"]
        clean = {f"Door{n}{k}": int(v) if str(v).lstrip("-").isdigit() else v
                 for k, v in params.items() if k in DOOR_PARAMS and v not in (None, "")}
        if clean:
            await rt.client.set_params(clean)
        if "Drivertime" in params and str(params["Drivertime"]).isdigit():
            self.store.x("UPDATE doors SET open_seconds=? WHERE id=?", (int(params["Drivertime"]), door_id))

    async def get_panel_params(self, pid: int, names: list[str]) -> dict:
        return await self.rt(pid).client.get_params(names)

    async def set_panel_params(self, pid: int, params: dict) -> None:
        await self.rt(pid).client.set_params(params)

    # ------------------------------------------------------- lettura tessera
    async def learn_card(self, timeout: float = 60, panel_id: int | None = None) -> dict | None:
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        item = (fut, panel_id)
        self._learners.append(item)
        self.emit("learn", {"waiting": True, "panel_id": panel_id})
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            if item in self._learners:
                self._learners.remove(item)
            self.emit("learn", {"waiting": bool(self._learners)})

    # =================================================== SINCRONIZZAZIONE
    def desired(self, pid: int) -> dict[str, dict]:
        """pin -> {"user": {...}, "auth": [{"tz":..,"mask":..}]} per un pannello."""
        doors = {d["id"]: d for d in self.store.q("SELECT * FROM doors WHERE panel_id=?", (pid,))}
        out: dict[str, dict] = {}
        for u in self.store.q("SELECT * FROM users WHERE enabled=1"):
            if not u["card"] and not u["password"]:
                continue
            masks: dict[int, int] = {}
            for door_id, tz in self.store.user_door_ids(u["id"]).items():
                d = doors.get(door_id)
                if d:
                    masks[tz] = masks.get(tz, 0) | (1 << (d["door_no"] - 1))
            if masks:
                out[u["pin"]] = {"user": u, "auth": [{"tz": tz, "mask": m} for tz, m in sorted(masks.items())]}
        return out

    @staticmethod
    def _user_record(cfg: c3.TableCfg, u: dict) -> dict:
        rec = {"Pin": u["pin"], "CardNo": u["card"] or 0, "Password": u["password"] or "",
               "Group": 0, "StartTime": ymd(u["start_date"]), "EndTime": ymd(u["end_date"]),
               "Name": (u["name"] or "")[:24], "SuperAuthorize": 1 if u.get("super") else 0}
        return {k: v for k, v in rec.items() if cfg.has(k)}

    async def _push_pins(self, rt: PanelRuntime, pins: list[str]) -> None:
        desired = self.desired(rt.id)
        ucfg = await rt.client.table("user")
        present = [p for p in pins if p in desired]
        absent = [p for p in pins if p not in desired]
        if absent:
            await rt.client.delete_data("userauthorize", [{"Pin": p} for p in absent])
            await rt.client.delete_data("user", [{"Pin": p} for p in absent])
        if present:
            await rt.client.delete_data("userauthorize", [{"Pin": p} for p in present])
            await rt.client.set_data("user", [self._user_record(ucfg, desired[p]["user"]) for p in present])
            auth = [{"Pin": p, "AuthorizeTimezoneId": a["tz"], "AuthorizeDoorId": a["mask"]}
                    for p in present for a in desired[p]["auth"]]
            await rt.client.set_data("userauthorize", auth)
        for p in pins:
            self.store.dequeue(rt.id, p)

    async def _flush_queue(self, rt: PanelRuntime) -> None:
        pins = self.store.queued(rt.id)
        if pins:
            await self._push_pins(rt, pins)
            self.emit("panel", self.panel_state(rt.id))

    async def push_user(self, pin: str, old_pin: str | None = None) -> dict:
        """Invia/aggiorna/cancella un utente su tutti i pannelli. Ritorna esito per pannello."""
        pins = [pin] + ([old_pin] if old_pin and old_pin != pin else [])
        result = {}
        for p in self.store.panels():
            if not p["enabled"]:
                continue
            for x in pins:
                self.store.queue(p["id"], x)
            rt = self.panels.get(p["id"])
            if rt and rt.online:
                try:
                    await self._push_pins(rt, pins)
                    result[p["name"]] = "ok"
                except Exception as err:  # noqa: BLE001
                    result[p["name"]] = f"in coda ({err})"
                    rt.wake.set()
            else:
                result[p["name"]] = "in coda (offline)"
        self.emit("users", {"pin": pin})
        for p in self.store.panels():
            if p["id"] in self.panels:
                self.emit("panel", self.panel_state(p["id"]))
        return result

    async def full_sync(self, pid: int, remove_unknown: bool = True) -> dict:
        rt = self.rt(pid)
        desired = self.desired(pid)
        on_panel = await rt.client.get_data("user", ["Pin"])
        panel_pins = {str(r.get("Pin")) for r in on_panel}
        extra = sorted(panel_pins - set(desired)) if remove_unknown else []
        if extra:
            await rt.client.delete_data("userauthorize", [{"Pin": p} for p in extra])
            await rt.client.delete_data("user", [{"Pin": p} for p in extra])
        await self.push_timezones(pid)
        pins = sorted(desired)
        for i in range(0, len(pins), 40):
            await self._push_pins(rt, pins[i:i + 40])
            self.sync_status.update(message=f"{self.store.panel(pid)['name']}: {min(i + 40, len(pins))}/{len(pins)}")
            self.emit("sync", self.sync_status)
        for p in list(self.store.queued(pid)):
            self.store.dequeue(pid, p)
        return {"inviati": len(pins), "rimossi": len(extra)}

    async def full_sync_all(self, remove_unknown: bool = True) -> dict:
        if self.sync_status.get("running"):
            raise RuntimeError("Sincronizzazione già in corso")
        self.sync_status = {"running": True, "message": "Avvio…", "progress": 0}
        self.emit("sync", self.sync_status)
        res = {}
        panels = [p for p in self.store.panels() if p["enabled"]]
        try:
            for i, p in enumerate(panels):
                try:
                    res[p["name"]] = await self.full_sync(p["id"], remove_unknown)
                except Exception as err:  # noqa: BLE001
                    res[p["name"]] = f"errore: {err}"
                self.sync_status["progress"] = int((i + 1) * 100 / max(1, len(panels)))
                self.emit("sync", self.sync_status)
        finally:
            self.sync_status = {"running": False, "message": "Completata", "progress": 100, "result": res}
            self.emit("sync", self.sync_status)
        return res

    async def push_timezones(self, pid: int) -> None:
        rt = self.rt(pid)
        try:
            cfg = await rt.client.table("timezone")
        except c3.C3Error:
            return
        recs = []
        for tz in self.store.q("SELECT * FROM timezones"):
            data = json.loads(tz["data"])
            rec: dict[str, Any] = {"TimezoneId": tz["id"]}
            for d in DAYS:
                for i in range(3):
                    iv = (data.get(d) or [])[i] if i < len(data.get(d) or []) else ["00:00", "00:00"]
                    rec[f"{TZ_FIELD[d]}Time{i + 1}"] = c3.encode_tz_interval(iv[0], iv[1])
            recs.append({k: v for k, v in rec.items() if cfg.has(k)})
        if recs:
            await rt.client.delete_data("timezone", [{"TimezoneId": r["TimezoneId"]} for r in recs])
            await rt.client.set_data("timezone", recs, batch=4)
        try:
            hcfg = await rt.client.table("holiday")
            hol = [{"Holiday": ymd(h["day"]), "HolidayType": h["type"], "Loop": 2 if h["loop"] else 1}
                   for h in self.store.q("SELECT * FROM holidays")]
            existing = await rt.client.get_data("holiday")
            if existing:
                await rt.client.delete_data("holiday", [{"Holiday": r["Holiday"]} for r in existing if r.get("Holiday")])
            if hol:
                await rt.client.set_data("holiday", [{k: v for k, v in h.items() if hcfg.has(k)} for h in hol])
        except c3.C3Error as err:
            _LOGGER.debug("holiday %s: %s", pid, err)

    async def read_panel_users(self, pid: int) -> list[dict]:
        rt = self.rt(pid)
        users = await rt.client.get_data("user")
        try:
            auth = await rt.client.get_data("userauthorize")
        except c3.C3Error:
            auth = []
        by_pin: dict[str, list] = {}
        for a in auth:
            by_pin.setdefault(str(a.get("Pin")), []).append(a)
        out = []
        for u in users:
            pin = str(u.get("Pin"))
            mask = 0
            for a in by_pin.get(pin, []):
                mask |= int(a.get("AuthorizeDoorId") or 0)
            out.append({"pin": pin, "card": str(u.get("CardNo") or ""), "name": u.get("Name", ""),
                        "start": panel_to_iso(u.get("StartTime", 0)), "end": panel_to_iso(u.get("EndTime", 0)),
                        "mask": mask, "doors": [n for n in range(1, 5) if mask & (1 << (n - 1))]})
        return out

    async def import_from_panels(self) -> dict:
        """Crea/aggiorna utenti del database leggendo tessere e permessi dai pannelli (porte dirette)."""
        added = updated = 0
        errors = {}
        for p in self.store.panels():
            if p["id"] not in self.panels:
                continue
            try:
                rows = await self.read_panel_users(p["id"])
            except Exception as err:  # noqa: BLE001
                errors[p["name"]] = str(err)
                continue
            doors = {d["door_no"]: d["id"] for d in self.store.q("SELECT * FROM doors WHERE panel_id=?", (p["id"],))}
            for r in rows:
                u = self.store.user(pin=r["pin"])
                door_ids = [doors[n] for n in r["doors"] if n in doors]
                if u is None:
                    self.store.save_user({"pin": r["pin"], "card": r["card"] if r["card"] != "0" else "",
                                          "name": r["name"], "start_date": r["start"], "end_date": r["end"],
                                          "doors": door_ids})
                    added += 1
                else:
                    existing = set(self.store.user_door_ids(u["id"]))
                    new = [d for d in door_ids if d not in existing]
                    if new:
                        u["doors"] = u["doors"] + new
                        self.store.save_user(u)
                        updated += 1
        self.emit("users", {})
        return {"aggiunti": added, "aggiornati": updated, "errori": errors}

    # --------------------------------------------------------- utilità
    def kick(self) -> None:
        for rt in self.panels.values():
            rt.wake.set()

    def users_due_expiry(self) -> list[dict]:
        today = date.today().isoformat()
        return self.store.q("SELECT * FROM users WHERE enabled=1 AND end_date<>'' AND end_date < ?", (today,))
