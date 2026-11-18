"""ZKTeco Access – add-on Home Assistant. Server web (ingress) + API + bridge MQTT."""

from __future__ import annotations

import asyncio
import csv
import io
import ipaddress
import json
import logging
import mimetypes
import os
import re
import signal
import time
import urllib.parse
import urllib.request
from typing import Any, Awaitable, Callable

import zkaccess_import
from api import Api, ApiError
from events_it import CATEGORIES
from ha_bridge import VERSION, HABridge
from manager import Manager
from mqtt import MQTTClient
from store import Store

DATA_DIR = os.environ.get("ZK_DATA", "/data")
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
_LOGGER = logging.getLogger("zkteco")

HASSIO_NET = ipaddress.ip_network("172.30.32.0/23")
Handler = Callable[["Request"], Awaitable[Any]]


class Request:
    def __init__(self, method: str, path: str, query: dict, headers: dict, body: bytes, peer: str, params: dict | None = None):
        self.method, self.path, self.query, self.headers, self.body, self.peer = method, path, query, headers, body, peer
        self.params = params or {}

    def json(self) -> Any:
        if not self.body:
            return {}
        try:
            return json.loads(self.body.decode("utf-8"))
        except ValueError as err:
            raise ApiError("JSON non valido") from err

    def arg(self, key: str, default: str = "") -> str:
        return self.query.get(key, [default])[0]


class Response:
    def __init__(self, body: bytes | str = b"", status: int = 200, ctype: str = "application/json; charset=utf-8", headers: dict | None = None):
        self.body = body.encode() if isinstance(body, str) else body
        self.status, self.ctype, self.headers = status, ctype, headers or {}


def jresp(data: Any, status: int = 200) -> Response:
    return Response(json.dumps(data, ensure_ascii=False, default=str), status)


STATUS_TEXT = {200: "OK", 204: "No Content", 400: "Bad Request", 403: "Forbidden", 404: "Not Found", 413: "Too Large", 500: "Internal Server Error"}


class App:
    def __init__(self, options: dict) -> None:
        self.options = options
        os.makedirs(DATA_DIR, exist_ok=True)
        self.store = Store(os.path.join(DATA_DIR, "zkteco.db"))
        self.manager = Manager(self.store, options)
        self.api = Api(self.store, self.manager, DATA_DIR)
        self.mqtt: MQTTClient | None = None
        self.bridge: HABridge | None = None
        self.routes: list[tuple[str, re.Pattern, Handler]] = []
        self._sse: set[asyncio.Queue] = set()
        self.manager.subscribe(self._broadcast)
        self._register()

    # ----------------------------------------------------------- avvio
    async def start(self) -> None:
        if not self.store.panels() and self.options.get("import_path") and os.path.exists(self.options["import_path"]):
            _LOGGER.info("Primo avvio: importo %s", self.options["import_path"])
            try:
                _LOGGER.info("Import: %s", zkaccess_import.import_mdb(self.options["import_path"], self.store))
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Import fallito")
        await self.manager.start()
        await self._start_mqtt()
        asyncio.ensure_future(self._housekeeping())
        asyncio.ensure_future(self._announce())

    async def _supervisor(self, method: str, path: str, data: dict | None = None) -> dict:
        token = os.environ.get("SUPERVISOR_TOKEN")
        if not token:
            raise RuntimeError("non in esecuzione sotto il Supervisor")
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(f"http://supervisor{path}", data=body, method=method,
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: json.load(urllib.request.urlopen(req, timeout=10)))

    async def _announce(self) -> None:
        """Annuncia l'add-on all'integrazione ZKTeco Access (scoperta automatica in Home Assistant)."""
        if not os.environ.get("SUPERVISOR_TOKEN"):
            return
        import socket
        cfg = {"host": socket.gethostname(), "port": int(self.options.get("port", 8099))}
        for _ in range(5):
            try:
                await self._supervisor("POST", "/discovery", {"service": "zkteco_access", "config": cfg})
                _LOGGER.info("Annunciato a Home Assistant come %s:%s (integrazione ZKTeco Access)", cfg["host"], cfg["port"])
                return
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("discovery: %s", err)
                await asyncio.sleep(30)

    async def _start_mqtt(self) -> None:
        cfg = None
        if self.options.get("mqtt_host"):
            cfg = {"host": self.options["mqtt_host"], "port": self.options.get("mqtt_port", 1883),
                   "username": self.options.get("mqtt_user", ""), "password": self.options.get("mqtt_password", "")}
        elif os.environ.get("SUPERVISOR_TOKEN"):
            try:
                req = urllib.request.Request("http://supervisor/services/mqtt",
                                             headers={"Authorization": f"Bearer {os.environ['SUPERVISOR_TOKEN']}"})
                data = await asyncio.get_running_loop().run_in_executor(None, lambda: json.load(urllib.request.urlopen(req, timeout=10)))
                cfg = data.get("data")
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Servizio MQTT non disponibile (%s): entità Home Assistant disattivate. "
                                "Installa il broker Mosquitto o imposta mqtt_host.", err)
        if not cfg:
            return
        if not self.options.get("mqtt_enabled", False):
            await self._mqtt_cleanup(cfg)
            return
        base = self.options.get("mqtt_topic", "zkteco_access")
        self.mqtt = MQTTClient(cfg["host"], int(cfg.get("port", 1883)), cfg.get("username", ""), cfg.get("password", ""),
                               client_id="zkteco_access_addon", will=(f"{base}/status", "offline"))
        self.bridge = HABridge(self.store, self.manager, self.api, self.mqtt, base,
                               self.options.get("discovery_prefix", "homeassistant"))
        self.mqtt.start()

    async def _mqtt_cleanup(self, cfg: dict) -> None:
        """MQTT disattivato: rimuove da Home Assistant le entità create in precedenza (evita doppioni con l'integrazione)."""
        old = self.store.get_setting("ha_entities", [])
        if not old:
            return
        prefix = self.options.get("discovery_prefix", "homeassistant")
        cl = MQTTClient(cfg["host"], int(cfg.get("port", 1883)), cfg.get("username", ""), cfg.get("password", ""),
                        client_id="zkteco_access_cleanup")
        cl.start()
        for _ in range(50):
            if cl.connected:
                break
            await asyncio.sleep(0.2)
        if cl.connected:
            for ent in old:
                await cl.publish(f"{prefix}/{ent}/config", "", retain=True)
            self.store.set_setting("ha_entities", [])
            _LOGGER.info("MQTT disattivato: rimosse %d entità MQTT precedenti", len(old))
            await asyncio.sleep(1)
        await cl.stop()

    async def _housekeeping(self) -> None:
        while True:
            try:
                self.store.purge_events(int(self.store.get_setting("event_retention_days", 365)))
                if self.options.get("auto_disable_expired", True):
                    for u in self.manager.users_due_expiry():
                        _LOGGER.info("Tessera %s scaduta il %s: disabilitata", u["pin"], u["end_date"])
                        self.store.x("UPDATE users SET enabled=0 WHERE id=?", (u["id"],))
                        await self.manager.push_user(u["pin"])
            except Exception:  # noqa: BLE001
                _LOGGER.exception("housekeeping")
            await asyncio.sleep(3600)

    def _broadcast(self, kind: str, data: dict) -> None:
        msg = json.dumps({"type": kind, "data": data}, ensure_ascii=False, default=str)
        for q in list(self._sse):
            if q.qsize() < 500:
                q.put_nowait(msg)

    # ----------------------------------------------------------- server
    def route(self, method: str, pattern: str, handler: Handler) -> None:
        self.routes.append((method, re.compile("^" + pattern + "$"), handler))

    async def handle_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = (writer.get_extra_info("peername") or ("?",))[0]
        try:
            while True:
                line = await asyncio.wait_for(reader.readline(), 120)
                if not line:
                    break
                try:
                    method, target, _ = line.decode("latin-1").split(" ", 2)
                except ValueError:
                    break
                headers: dict[str, str] = {}
                while True:
                    h = await reader.readline()
                    if h in (b"\r\n", b"\n", b""):
                        break
                    k, _, v = h.decode("latin-1").partition(":")
                    headers[k.strip().lower()] = v.strip()
                length = int(headers.get("content-length", "0") or 0)
                if length > 200 * 1024 * 1024:
                    await self._write(writer, Response("file troppo grande", 413, "text/plain"))
                    break
                body = await reader.readexactly(length) if length else b""
                url = urllib.parse.urlsplit(target)
                req = Request(method.upper(), url.path, urllib.parse.parse_qs(url.query), headers, body, peer)
                if not self._allowed(req):
                    _LOGGER.warning("Richiesta da %s rifiutata (fuori dalla rete di Home Assistant e senza token)", peer)
                    await self._write(writer, Response('{"error":"accesso negato: imposta api_token"}', 403))
                    break
                if req.path.endswith("/api/stream"):
                    await self._sse_stream(writer)
                    break
                resp = await self._dispatch(req)
                await self._write(writer, resp)
                if headers.get("connection", "").lower() == "close":
                    break
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    def _allowed(self, req: Request) -> bool:
        # rete interna di Home Assistant (172.30.32.0/23): Supervisor/ingress, Home Assistant (integrazione)
        try:
            ip = ipaddress.ip_address(req.peer.removeprefix("::ffff:"))
            if ip.is_loopback or ip in HASSIO_NET:
                return True
        except ValueError:
            pass
        token = self.options.get("api_token") or ""
        if token:
            auth = req.headers.get("authorization", "")
            return auth == f"Bearer {token}" or req.arg("token") == token
        return bool(self.options.get("allow_direct_access", False))

    async def _write(self, writer: asyncio.StreamWriter, resp: Response) -> None:
        head = [f"HTTP/1.1 {resp.status} {STATUS_TEXT.get(resp.status, 'OK')}",
                f"Content-Type: {resp.ctype}", f"Content-Length: {len(resp.body)}", "Cache-Control: no-store"]
        head += [f"{k}: {v}" for k, v in resp.headers.items()]
        writer.write(("\r\n".join(head) + "\r\n\r\n").encode() + resp.body)
        await writer.drain()

    async def _sse_stream(self, writer: asyncio.StreamWriter) -> None:
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-cache\r\nX-Accel-Buffering: no\r\n\r\n")
        q: asyncio.Queue = asyncio.Queue()
        self._sse.add(q)
        try:
            writer.write(f"data: {json.dumps({'type': 'state', 'data': self.manager.full_state()}, default=str)}\n\n".encode())
            await writer.drain()
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), 20)
                    writer.write(f"data: {msg}\n\n".encode())
                except asyncio.TimeoutError:
                    writer.write(b": ping\n\n")
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self._sse.discard(q)

    async def _dispatch(self, req: Request) -> Response:
        path = req.path
        idx = path.find("/api/")
        if idx >= 0:
            path = path[idx:]
        else:
            return self._static(path)
        for method, rx, handler in self.routes:
            m = rx.match(path)
            if m and method == req.method:
                req.params = m.groupdict()
                try:
                    res = await handler(req)
                    return res if isinstance(res, Response) else jresp(res if res is not None else {"ok": True})
                except ApiError as err:
                    return jresp({"error": str(err)}, err.status)
                except (ValueError, KeyError, RuntimeError) as err:
                    return jresp({"error": str(err)}, 400)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.exception("Errore %s %s", req.method, path)
                    return jresp({"error": f"{type(err).__name__}: {err}"}, 500)
        return jresp({"error": "non trovato"}, 404)

    def _static(self, path: str) -> Response:
        name = path.rsplit("/", 1)[-1] or "index.html"
        if name not in os.listdir(WEB_DIR):
            name = "index.html"
        fp = os.path.join(WEB_DIR, name)
        with open(fp, "rb") as f:
            data = f.read()
        ctype = mimetypes.guess_type(fp)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        return Response(data, 200, ctype)

    # ------------------------------------------------------------ rotte
    def _register(self) -> None:
        s, m, api, r = self.store, self.manager, self.api, self.route

        async def state(req):
            return {**m.full_state(), "doors": s.doors(), "aux": s.aux_list(), "levels": s.levels(),
                    "timezones": s.q("SELECT id,name FROM timezones ORDER BY id"), "categories": CATEGORIES,
                    "version": VERSION, "mqtt": bool(self.mqtt and self.mqtt.connected), "mqtt_configured": bool(self.mqtt),
                    "user_count": s.q1("SELECT COUNT(*) n FROM users")["n"], "settings": api.settings()}
        r("GET", "/api/state", state)

        # pannelli
        async def panels(req):
            q = m.store.queue_count()
            return [{**p, "password": p["password"], "queue": q.get(p["id"], 0), **{"state": m.panel_state(p["id"])}} for p in s.panels()]
        r("GET", "/api/panels", panels)

        async def panel_save(req):
            d = req.json()
            if not d.get("name") or not d.get("host"):
                raise ApiError("Nome e indirizzo IP obbligatori")
            pid = s.save_panel(d)
            await m.reload_panel(pid)
            return {"id": pid}
        r("POST", "/api/panels", panel_save)

        async def panel_delete(req):
            pid = int(req.params["id"])
            await m._stop_panel(pid)
            s.x("DELETE FROM sync_queue WHERE panel_id=?", (pid,))
            s.x("DELETE FROM panels WHERE id=?", (pid,))
            m.emit("panels", {})
            return {"ok": True}
        r("DELETE", r"/api/panels/(?P<id>\d+)", panel_delete)

        async def panel_test(req):
            d = req.json()
            try:
                return {"ok": True, "info": await m.test_connection(d["host"], int(d.get("port") or 4370), d.get("password", ""))}
            except Exception as err:  # noqa: BLE001
                return {"ok": False, "error": str(err)}
        r("POST", "/api/panels/test", panel_test)

        async def panel_action(req):
            return {"result": await m.panel_action(int(req.params["id"]), req.json().get("action", ""))}
        r("POST", r"/api/panels/(?P<id>\d+)/action", panel_action)

        async def panel_users(req):
            return await m.read_panel_users(int(req.params["id"]))
        r("GET", r"/api/panels/(?P<id>\d+)/users", panel_users)

        async def panel_sync(req):
            return await m.full_sync(int(req.params["id"]), bool(req.json().get("remove_unknown", True)))
        r("POST", r"/api/panels/(?P<id>\d+)/sync", panel_sync)

        async def panel_params_get(req):
            names = [n for n in req.arg("names").split(",") if n]
            return await m.get_panel_params(int(req.params["id"]), names)
        r("GET", r"/api/panels/(?P<id>\d+)/params", panel_params_get)

        async def panel_params_set(req):
            await m.set_panel_params(int(req.params["id"]), req.json())
            return {"ok": True}
        r("POST", r"/api/panels/(?P<id>\d+)/params", panel_params_set)

        async def sync_all(req):
            asyncio.ensure_future(m.full_sync_all(bool(req.json().get("remove_unknown", True))))
            return {"started": True}
        r("POST", "/api/sync_all", sync_all)

        async def import_panels(req):
            return await m.import_from_panels()
        r("POST", "/api/import_from_panels", import_panels)

        # porte
        async def door_save(req):
            d = req.json()
            did = int(req.params["id"])
            if "name" in d:
                s.x("UPDATE doors SET name=? WHERE id=?", (d["name"].strip(), did))
            if "open_seconds" in d:
                s.x("UPDATE doors SET open_seconds=? WHERE id=?", (int(d["open_seconds"]), did))
            if "hidden" in d:
                s.x("UPDATE doors SET hidden=? WHERE id=?", (1 if d["hidden"] else 0, did))
            m.emit("panels", {})
            return s.door(did)
        r("POST", r"/api/doors/(?P<id>\d+)", door_save)

        async def door_cmd(req):
            did, cmd = int(req.params["id"]), req.params["cmd"]
            d = req.json()
            if cmd == "open":
                await m.open_door(did, d.get("seconds"))
            elif cmd == "close":
                await m.close_door(did)
            elif cmd == "normal_open":
                await m.normal_open(did, bool(d.get("enable", True)))
            return {"ok": True}
        r("POST", r"/api/doors/(?P<id>\d+)/(?P<cmd>open|close|normal_open)", door_cmd)

        async def door_params_get(req):
            return await m.get_door_params(int(req.params["id"]))
        r("GET", r"/api/doors/(?P<id>\d+)/params", door_params_get)

        async def door_params_set(req):
            await m.set_door_params(int(req.params["id"]), req.json())
            return {"ok": True}
        r("POST", r"/api/doors/(?P<id>\d+)/params", door_params_set)

        async def aux_save(req):
            d = req.json()
            aid = int(req.params["id"])
            if "name" in d:
                s.x("UPDATE aux SET name=? WHERE id=?", (d["name"].strip(), aid))
            if "hidden" in d:
                s.x("UPDATE aux SET hidden=? WHERE id=?", (1 if d["hidden"] else 0, aid))
            m.emit("panels", {})
            return {"ok": True}
        r("POST", r"/api/aux/(?P<id>\d+)", aux_save)

        async def aux_out(req):
            await m.aux_output(int(req.params["id"]), int(req.json().get("seconds", 5)))
            return {"ok": True}
        r("POST", r"/api/aux/(?P<id>\d+)/output", aux_out)

        # livelli
        async def levels(req):
            return s.levels()
        r("GET", "/api/levels", levels)

        async def level_save(req):
            d = req.json()
            if not (d.get("name") or "").strip():
                raise ApiError("Nome livello obbligatorio")
            old_users = []
            if d.get("id"):
                old_users = [x["pin"] for x in s.q("SELECT u.pin FROM users u JOIN user_levels ul ON ul.user_id=u.id WHERE ul.level_id=?", (int(d["id"]),))]
            lid = s.save_level(d)
            for pin in old_users:
                await m.push_user(pin)
            return {"id": lid, "aggiornati": len(old_users)}
        r("POST", "/api/levels", level_save)

        async def level_delete(req):
            lid = int(req.params["id"])
            pins = [x["pin"] for x in s.q("SELECT u.pin FROM users u JOIN user_levels ul ON ul.user_id=u.id WHERE ul.level_id=?", (lid,))]
            s.x("DELETE FROM levels WHERE id=?", (lid,))
            for pin in pins:
                await m.push_user(pin)
            return {"ok": True}
        r("DELETE", r"/api/levels/(?P<id>\d+)", level_delete)

        # utenti
        async def users(req):
            return s.users(req.arg("q"))
        r("GET", "/api/users", users)

        async def user_save(req):
            return await api.save_user(req.json())
        r("POST", "/api/users", user_save)

        async def user_delete(req):
            return await api.delete_user({"id": int(req.params["id"])})
        r("DELETE", r"/api/users/(?P<id>\d+)", user_delete)

        async def user_push(req):
            u = s.user(uid=int(req.params["id"]))
            return await m.push_user(u["pin"])
        r("POST", r"/api/users/(?P<id>\d+)/push", user_push)

        async def next_pin(req):
            return {"pin": s.next_pin()}
        r("GET", "/api/users/next_pin", next_pin)

        async def bulk(req):
            d = req.json()
            ids = [int(x) for x in d.get("ids", [])]
            act = d.get("action")
            res = 0
            for uid in ids:
                u = s.user(uid=uid)
                if not u:
                    continue
                if act == "delete":
                    s.x("DELETE FROM users WHERE id=?", (uid,))
                elif act in ("enable", "disable"):
                    u["enabled"] = act == "enable"
                    s.save_user(u)
                elif act in ("add_level", "remove_level"):
                    lid = int(d["level_id"])
                    u["levels"] = list({*u["levels"], lid}) if act == "add_level" else [x for x in u["levels"] if x != lid]
                    s.save_user(u)
                elif act == "set_end":
                    u["end_date"] = d.get("end_date") or ""
                    s.save_user(u)
                await m.push_user(u["pin"])
                res += 1
            return {"aggiornati": res}
        r("POST", "/api/users/bulk", bulk)

        async def learn(req):
            d = req.json()
            res = await m.learn_card(float(d.get("timeout", 45)), int(d["panel_id"]) if d.get("panel_id") else None)
            return {"card": res}
        r("POST", "/api/learn", learn)

        # fasce orarie / festività
        async def tzs(req):
            return [{**t, "data": json.loads(t["data"])} for t in s.q("SELECT * FROM timezones ORDER BY id")]
        r("GET", "/api/timezones", tzs)

        async def tz_save(req):
            d = req.json()
            tid = int(d.get("id") or 0) or (int(s.q1("SELECT MAX(id) m FROM timezones")["m"] or 0) + 1)
            s.x("INSERT INTO timezones(id,name,data) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name, data=excluded.data",
                (tid, d.get("name") or f"Fascia {tid}", json.dumps(d.get("data") or {})))
            return {"id": tid}
        r("POST", "/api/timezones", tz_save)

        async def tz_delete(req):
            tid = int(req.params["id"])
            if tid == 1:
                raise ApiError("La fascia 1 (24 ore) non si può eliminare")
            s.x("UPDATE levels SET timezone_id=1 WHERE timezone_id=?", (tid,))
            s.x("DELETE FROM timezones WHERE id=?", (tid,))
            return {"ok": True}
        r("DELETE", r"/api/timezones/(?P<id>\d+)", tz_delete)

        async def holidays(req):
            return s.q("SELECT * FROM holidays ORDER BY day")
        r("GET", "/api/holidays", holidays)

        async def holiday_save(req):
            d = req.json()
            if d.get("id"):
                s.x("UPDATE holidays SET name=?,day=?,type=?,loop=? WHERE id=?", (d["name"], d["day"], int(d.get("type", 1)), 1 if d.get("loop") else 0, int(d["id"])))
            else:
                s.x("INSERT INTO holidays(name,day,type,loop) VALUES(?,?,?,?)", (d["name"], d["day"], int(d.get("type", 1)), 1 if d.get("loop") else 0))
            return {"ok": True}
        r("POST", "/api/holidays", holiday_save)

        async def holiday_delete(req):
            s.x("DELETE FROM holidays WHERE id=?", (int(req.params["id"]),))
            return {"ok": True}
        r("DELETE", r"/api/holidays/(?P<id>\d+)", holiday_delete)

        async def push_tz(req):
            out = {}
            for pid in list(m.panels):
                try:
                    await m.push_timezones(pid)
                    out[s.panel(pid)["name"]] = "ok"
                except Exception as err:  # noqa: BLE001
                    out[s.panel(pid)["name"]] = str(err)
            return out
        r("POST", "/api/timezones/push", push_tz)

        # eventi
        async def events(req):
            return s.events(int(req.arg("limit", "200")), float(req.arg("before") or 0) or None, req.arg("q"),
                            int(req.arg("panel") or 0) or None, req.arg("category"), float(req.arg("since") or 0) or None)
        r("GET", "/api/events", events)

        async def events_csv(req):
            rows = s.events(100000, None, req.arg("q"), int(req.arg("panel") or 0) or None, req.arg("category"),
                            float(req.arg("since") or 0) or None)
            buf = io.StringIO()
            w = csv.writer(buf, delimiter=";")
            w.writerow(["Data/ora", "Dispositivo", "Porta", "Evento", "Utente", "PIN", "Tessera", "Categoria"])
            for e in rows:
                w.writerow([time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e["ts"])), e["panel"], e["door"], e["description"],
                            e["user"], e["pin"], e["card"], CATEGORIES.get(e["category"], e["category"])])
            return Response("﻿" + buf.getvalue(), 200, "text/csv; charset=utf-8",
                            {"Content-Disposition": "attachment; filename=eventi_accessi.csv"})
        r("GET", "/api/events.csv", events_csv)

        # import / backup / impostazioni
        async def import_mdb(req):
            if req.body:
                path = api.path_in_data("upload_zkaccess.mdb")
                with open(path, "wb") as f:
                    f.write(req.body)
            else:
                path = req.arg("path") or self.options.get("import_path") or ""
                if not os.path.exists(path):
                    raise ApiError(f"File non trovato: {path}")
            replace = req.arg("replace") == "1"
            stats = await asyncio.get_running_loop().run_in_executor(None, lambda: zkaccess_import.import_mdb(path, s, replace))
            await m.reload_all()
            return stats
        r("POST", "/api/import/mdb", import_mdb)

        async def import_chunk(req):
            # upload a pezzi: il proxy ingress di Home Assistant rifiuta corpi grandi (errore 413)
            n, total = int(req.arg("n", "0")), int(req.arg("total", "1"))
            part = api.path_in_data("upload_zkaccess.mdb.part")
            with open(part, "wb" if n == 0 else "ab") as f:
                f.write(req.body)
            if n + 1 < total:
                return {"received": n + 1, "total": total}
            path = api.path_in_data("upload_zkaccess.mdb")
            os.replace(part, path)
            replace = req.arg("replace") == "1"
            stats = await asyncio.get_running_loop().run_in_executor(None, lambda: zkaccess_import.import_mdb(path, s, replace))
            await m.reload_all()
            return stats
        r("POST", "/api/import/chunk", import_chunk)

        async def backup(req):
            return Response(api.export_backup(), 200, "application/json; charset=utf-8",
                            {"Content-Disposition": "attachment; filename=zkteco_access_backup.json"})
        r("GET", "/api/backup", backup)

        async def restore(req):
            res = api.import_backup(req.body.decode("utf-8"))
            await m.reload_all()
            return res
        r("POST", "/api/restore", restore)

        async def settings_save(req):
            return api.save_settings(req.json())
        r("POST", "/api/settings", settings_save)

        async def cmd(req):
            return {"result": await api.command(req.json())}
        r("POST", "/api/cmd", cmd)


def load_options() -> dict:
    path = os.path.join(DATA_DIR, "options.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


async def amain() -> None:
    options = load_options()
    level = getattr(logging, str(options.get("log_level", "info")).upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    logging.getLogger("c3").setLevel(logging.WARNING if level > logging.DEBUG else logging.DEBUG)
    app = App(options)
    await app.start()
    port = int(options.get("port", os.environ.get("ZK_PORT", 8099)))
    server = await asyncio.start_server(app.handle_conn, "0.0.0.0", port, limit=2 ** 20)
    _LOGGER.info("ZKTeco Access %s in ascolto sulla porta %s", VERSION, port)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover
            pass
    await stop.wait()
    server.close()
    if app.mqtt:
        await app.mqtt.publish(f"{options.get('mqtt_topic', 'zkteco_access')}/status", "offline", retain=True)
        await app.mqtt.stop()
    await app.manager.stop()


if __name__ == "__main__":
    asyncio.run(amain())
