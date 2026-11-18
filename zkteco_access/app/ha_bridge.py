"""Bridge MQTT verso Home Assistant (MQTT discovery): serrature, sensori, eventi, comandi."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

from api import Api, ApiError
from events_it import CATEGORIES
from manager import Manager
from mqtt import MQTTClient
from store import Store

_LOGGER = logging.getLogger("zkteco.ha")
VERSION = "1.0.2"


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", text.lower()).strip("_")


class HABridge:
    def __init__(self, store: Store, manager: Manager, api: Api, mqtt: MQTTClient, base: str = "zkteco_access",
                 discovery_prefix: str = "homeassistant") -> None:
        self.store, self.m, self.api, self.mqtt = store, manager, api, mqtt
        self.base = base
        self.prefix = discovery_prefix
        self._published: set[str] = set()
        self._last: dict[str, str] = {}
        self._republish_task: asyncio.Task | None = None
        mqtt.on_connect = self._on_connect
        mqtt.on_message = self._on_message
        manager.subscribe(self._on_manager)

    # ------------------------------------------------------------ setup
    async def _on_connect(self) -> None:
        self._published.clear()
        self._last.clear()
        await self.mqtt.publish(f"{self.base}/status", "online", retain=True)
        await self.mqtt.subscribe(f"{self.base}/door/+/set")
        await self.mqtt.subscribe(f"{self.base}/door/+/open")
        await self.mqtt.subscribe(f"{self.base}/door/+/normal_open/set")
        await self.mqtt.subscribe(f"{self.base}/aux/+/set")
        await self.mqtt.subscribe(f"{self.base}/panel/+/action")
        await self.mqtt.subscribe(f"{self.base}/cmd")
        await self.mqtt.subscribe(f"{self.prefix}/status")
        await self.publish_discovery()
        await self.publish_all_states()

    def _device(self, p: dict) -> dict:
        return {
            "identifiers": [f"zkteco_panel_{p['id']}"],
            "name": f"ZKTeco {p['name']}",
            "manufacturer": "ZKTeco",
            "model": p.get("model") or "C3/inBio",
            "sw_version": p.get("firmware") or "",
            "serial_number": p.get("serial") or "",
            "configuration_url": None,
            "via_device": "zkteco_access_hub",
        }

    def _hub(self) -> dict:
        return {"identifiers": ["zkteco_access_hub"], "name": "ZKTeco Access", "manufacturer": "ZKTeco",
                "model": "Add-on controllo accessi", "sw_version": VERSION}

    async def _disc(self, component: str, uid: str, cfg: dict) -> None:
        cfg = {k: v for k, v in cfg.items() if v is not None}
        if isinstance(cfg.get("device"), dict):
            cfg["device"] = {k: v for k, v in cfg["device"].items() if v not in (None, "")}
        cfg.setdefault("unique_id", uid)
        cfg.setdefault("object_id", uid)
        cfg.setdefault("availability", [{"topic": f"{self.base}/status"}])
        self._published.add(f"{component}/{uid}")
        await self._pub(f"{self.prefix}/{component}/{uid}/config", json.dumps(cfg, ensure_ascii=False))

    async def publish_discovery(self) -> None:
        old = set(self.store.get_setting("ha_entities", []))
        self._published = set()
        hub = self._hub()
        await self._disc("sensor", "zk_last_card", {
            "name": "Ultima tessera letta", "state_topic": f"{self.base}/last_card", "icon": "mdi:card-account-details",
            "json_attributes_topic": f"{self.base}/last_card/attr", "device": hub})
        await self._disc("event", "zk_access_events", {
            "name": "Eventi accesso", "state_topic": f"{self.base}/event", "event_types": list(CATEGORIES),
            "icon": "mdi:door-closed-lock", "device": hub})
        await self._disc("button", "zk_sync_all", {
            "name": "Sincronizza tutti i pannelli", "command_topic": f"{self.base}/cmd",
            "payload_press": json.dumps({"action": "sync_all"}), "icon": "mdi:sync", "device": hub, "entity_category": "config"})
        panels = {p["id"]: p for p in self.store.panels()}
        for p in panels.values():
            dev = self._device(p)
            pid = p["id"]
            avail = [{"topic": f"{self.base}/status"}]
            await self._disc("binary_sensor", f"zk_p{pid}_online", {
                "name": "Connessione", "state_topic": f"{self.base}/panel/{pid}/online", "device_class": "connectivity",
                "device": dev, "has_entity_name": True, "entity_category": "diagnostic"})
            for act, name, icon in (("restart", "Riavvia", "mdi:restart"), ("cancel_alarm", "Annulla allarmi", "mdi:alarm-off"),
                                    ("sync_time", "Sincronizza ora", "mdi:clock-check"), ("sync", "Sincronizza utenti", "mdi:account-sync")):
                await self._disc("button", f"zk_p{pid}_{act}", {
                    "name": name, "command_topic": f"{self.base}/panel/{pid}/action", "payload_press": act,
                    "icon": icon, "device": dev, "has_entity_name": True, "availability": avail,
                    "entity_category": "config" if act != "cancel_alarm" else None})
            await self._disc("sensor", f"zk_p{pid}_queue", {
                "name": "Modifiche in coda", "state_topic": f"{self.base}/panel/{pid}/queue", "icon": "mdi:tray-full",
                "device": dev, "has_entity_name": True, "entity_category": "diagnostic"})
        for d in self.store.doors():
            if d["hidden"] or d["panel_id"] not in panels:
                continue
            dev = self._device(panels[d["panel_id"]])
            did, nm = d["id"], d["name"]
            avail = [{"topic": f"{self.base}/status"}, {"topic": f"{self.base}/panel/{d['panel_id']}/online", "payload_available": "ON", "payload_not_available": "OFF"}]
            common = {"device": dev, "availability": avail, "availability_mode": "all"}
            await self._disc("lock", f"zk_door{did}_lock", {
                **common, "name": nm, "command_topic": f"{self.base}/door/{did}/set", "state_topic": f"{self.base}/door/{did}/lock",
                "payload_lock": "LOCK", "payload_unlock": "UNLOCK", "payload_open": "OPEN",
                "state_locked": "LOCKED", "state_unlocked": "UNLOCKED", "icon": "mdi:door"})
            await self._disc("button", f"zk_door{did}_open", {
                **common, "name": f"{nm} apri", "command_topic": f"{self.base}/door/{did}/open", "payload_press": "OPEN",
                "icon": "mdi:door-open"})
            await self._disc("switch", f"zk_door{did}_normal_open", {
                **common, "name": f"{nm} sempre aperta", "command_topic": f"{self.base}/door/{did}/normal_open/set",
                "state_topic": f"{self.base}/door/{did}/normal_open", "icon": "mdi:door-sliding-open", "entity_category": "config"})
            await self._disc("binary_sensor", f"zk_door{did}_contact", {
                **common, "name": f"{nm} sensore", "state_topic": f"{self.base}/door/{did}/contact", "device_class": "door"})
            await self._disc("binary_sensor", f"zk_door{did}_alarm", {
                **common, "name": f"{nm} allarme", "state_topic": f"{self.base}/door/{did}/alarm", "device_class": "problem"})
            await self._disc("sensor", f"zk_door{did}_last", {
                **common, "name": f"{nm} ultimo evento", "state_topic": f"{self.base}/door/{did}/last",
                "json_attributes_topic": f"{self.base}/door/{did}/last/attr", "icon": "mdi:account-key"})
            await self._disc("event", f"zk_door{did}_event", {
                **common, "name": f"{nm} accessi", "state_topic": f"{self.base}/door/{did}/event",
                "event_types": list(CATEGORIES), "icon": "mdi:card-account-details-outline"})
        for a in self.store.aux_list():
            if a["hidden"] or a["panel_id"] not in panels:
                continue
            dev = self._device(panels[a["panel_id"]])
            if a["kind"] == "out":
                await self._disc("switch", f"zk_aux{a['id']}", {
                    "name": a["name"], "command_topic": f"{self.base}/aux/{a['id']}/set", "state_topic": f"{self.base}/aux/{a['id']}/state",
                    "device": dev, "icon": "mdi:electric-switch", "optimistic": False})
            else:
                await self._disc("binary_sensor", f"zk_aux{a['id']}", {
                    "name": a["name"], "state_topic": f"{self.base}/aux/{a['id']}/state", "device": dev})
        # rimuove entità non più esistenti
        for gone in old - self._published:
            self._last.pop(f"{self.prefix}/{gone}/config", None)
            await self.mqtt.publish(f"{self.prefix}/{gone}/config", "", retain=True)
        self.store.set_setting("ha_entities", sorted(self._published))

    # ------------------------------------------------------------ stati
    async def _pub(self, topic: str, value: Any, retain: bool = True) -> None:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        if retain and self._last.get(topic) == text:
            return
        self._last[topic] = text
        await self.mqtt.publish(topic, text, retain=retain)

    async def publish_panel(self, st: dict) -> None:
        pid = st["id"]
        await self._pub(f"{self.base}/panel/{pid}/online", "ON" if st["online"] else "OFF")
        await self._pub(f"{self.base}/panel/{pid}/queue", str(st.get("queue", 0)))
        doors = {d["door_no"]: d for d in self.store.q("SELECT * FROM doors WHERE panel_id=?", (pid,))}
        for n, ds in st["doors"].items():
            d = doors.get(int(n))
            if not d:
                continue
            did = d["id"]
            await self._pub(f"{self.base}/door/{did}/lock", "UNLOCKED" if ds["unlocked"] else "LOCKED")
            await self._pub(f"{self.base}/door/{did}/normal_open", "ON" if ds["normal_open"] else "OFF")
            if ds["sensor"] in (1, 2):
                await self._pub(f"{self.base}/door/{did}/contact", "ON" if ds["sensor"] == 2 else "OFF")
            await self._pub(f"{self.base}/door/{did}/alarm", "ON" if ds["alarm"] else "OFF")
        auxes = self.store.q("SELECT * FROM aux WHERE panel_id=?", (pid,))
        for a in auxes:
            src = st["aux_out"] if a["kind"] == "out" else st["aux_in"]
            v = src.get(a["aux_no"]) if a["aux_no"] in src else src.get(str(a["aux_no"]))
            if v is not None:
                await self._pub(f"{self.base}/aux/{a['id']}/state", "ON" if v else "OFF")

    async def publish_all_states(self) -> None:
        for st in self.m.full_state()["panels"]:
            await self.publish_panel(st)

    async def publish_event(self, ev: dict) -> None:
        attrs = {k: ev.get(k) for k in ("panel", "door", "description", "user", "pin", "card", "inout_text", "verify_text", "event")}
        attrs["time"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ev["ts"]))
        payload = {"event_type": ev["category"], **attrs}
        await self.mqtt.publish(f"{self.base}/event", json.dumps(payload, ensure_ascii=False))
        if not ev.get("aux") and ev.get("door_no"):
            d = self.store.q1("SELECT id FROM doors WHERE panel_id=? AND door_no=?", (ev["panel_id"], ev["door_no"]))
            if d:
                await self.mqtt.publish(f"{self.base}/door/{d['id']}/event", json.dumps(payload, ensure_ascii=False))
                if ev["category"] not in ("door",):
                    text = f"{ev['description']}" + (f" – {ev['user']}" if ev.get("user") else (f" – tessera {ev['card']}" if ev.get("card") else ""))
                    await self._pub(f"{self.base}/door/{d['id']}/last", text[:250])
                    await self._pub(f"{self.base}/door/{d['id']}/last/attr", attrs)
        if ev.get("card"):
            await self._pub(f"{self.base}/last_card", ev["card"])
            await self._pub(f"{self.base}/last_card/attr", attrs)

    def _on_manager(self, kind: str, data: dict) -> None:
        if not self.mqtt.connected:
            return
        if kind == "panel":
            asyncio.ensure_future(self.publish_panel(data))
        elif kind == "event":
            asyncio.ensure_future(self.publish_event(data))
        elif kind == "panels":
            # coalesce: molte modifiche ravvicinate -> una sola ripubblicazione
            if self._republish_task is None or self._republish_task.done():
                self._republish_task = asyncio.ensure_future(self._republish())

    async def _republish(self) -> None:
        await asyncio.sleep(2)
        await self.publish_discovery()
        await self.publish_all_states()

    # --------------------------------------------------------- comandi
    async def _on_message(self, topic: str, payload: bytes) -> None:
        text = payload.decode("utf-8", "replace").strip()
        try:
            if topic == f"{self.prefix}/status":
                if text == "online":
                    await asyncio.sleep(2)
                    await self._on_connect()
                return
            if topic == f"{self.base}/cmd":
                data = json.loads(text) if text.startswith("{") else {"action": text}
                res = await self.api.command(data)
                await self.mqtt.publish(f"{self.base}/cmd/result", json.dumps({"ok": True, "action": data.get("action"), "result": res}, ensure_ascii=False, default=str))
                return
            rel = topic[len(self.base) + 1:].split("/") if topic.startswith(self.base + "/") else []
            if len(rel) >= 3 and rel[0] == "door":
                did = int(rel[1])
                if rel[2] == "open" or (rel[2] == "set" and text in ("UNLOCK", "OPEN")):
                    await self.m.open_door(did)
                elif rel[2] == "set" and text == "LOCK":
                    await self.m.close_door(did)
                elif rel[2] == "normal_open":
                    await self.m.normal_open(did, text == "ON")
            elif len(rel) >= 3 and rel[0] == "aux":
                await self.m.aux_output(int(rel[1]), 255 if text == "ON" else 0)
                await self._pub(f"{self.base}/aux/{rel[1]}/state", "ON" if text == "ON" else "OFF")
            elif len(rel) >= 3 and rel[0] == "panel":
                pid = int(rel[1])
                if text == "sync":
                    await self.m.full_sync(pid)
                else:
                    await self.m.panel_action(pid, text)
        except (ApiError, ValueError, RuntimeError, KeyError) as err:
            _LOGGER.warning("Comando MQTT %s fallito: %s", topic, err)
            await self.mqtt.publish(f"{self.base}/cmd/result", json.dumps({"ok": False, "topic": topic, "error": str(err)}, ensure_ascii=False))
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Comando MQTT %s fallito", topic)
            await self.mqtt.publish(f"{self.base}/cmd/result", json.dumps({"ok": False, "topic": topic, "error": str(err)}, ensure_ascii=False))
