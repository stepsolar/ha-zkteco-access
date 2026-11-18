"""Azioni condivise tra interfaccia web, API REST e comandi MQTT."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from manager import Manager
from store import Store

_LOGGER = logging.getLogger("zkteco.api")


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class Api:
    def __init__(self, store: Store, manager: Manager, data_dir: str) -> None:
        self.store = store
        self.m = manager
        self.data_dir = data_dir

    # ------------------------------------------------------------ util
    def _door_id(self, ref: Any) -> int:
        """Accetta id numerico o nome porta."""
        if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit() and self.store.door(int(ref))):
            d = self.store.door(int(ref))
            if d:
                return d["id"]
        r = self.store.q1("SELECT id FROM doors WHERE lower(name)=lower(?)", (str(ref),))
        if not r:
            raise ApiError(f"Porta '{ref}' non trovata", 404)
        return r["id"]

    def _level_id(self, ref: Any) -> int:
        if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit() and self.store.q1("SELECT id FROM levels WHERE id=?", (int(ref),))):
            return int(ref)
        r = self.store.q1("SELECT id FROM levels WHERE lower(name)=lower(?)", (str(ref),))
        if not r:
            raise ApiError(f"Livello '{ref}' non trovato", 404)
        return r["id"]

    def _find_user(self, data: dict) -> dict:
        u = None
        if data.get("id"):
            u = self.store.user(uid=int(data["id"]))
        if not u and data.get("pin") not in (None, ""):
            u = self.store.user(pin=str(data["pin"]))
        if not u and data.get("card") not in (None, ""):
            u = self.store.user(card=str(data["card"]))
        if not u:
            raise ApiError("Utente/tessera non trovato", 404)
        return u

    # ------------------------------------------------------- utenti
    async def save_user(self, data: dict) -> dict:
        old = self.store.user(uid=int(data["id"])) if data.get("id") else None
        if old:
            merged = {**old, **data}
        else:
            merged = dict(data)
        if "levels" in data:
            merged["levels"] = [self._level_id(x) for x in data.get("levels") or []]
        if "doors" in data:
            merged["doors"] = [self._door_id(x) for x in data.get("doors") or []]
        try:
            uid = self.store.save_user(merged)
        except ValueError as err:
            raise ApiError(str(err)) from err
        u = self.store.user(uid=uid)
        push = await self.m.push_user(u["pin"], old["pin"] if old else None)
        return {"user": u, "push": push}

    async def delete_user(self, data: dict) -> dict:
        u = self._find_user(data)
        self.store.x("DELETE FROM users WHERE id=?", (u["id"],))
        push = await self.m.push_user(u["pin"])
        return {"deleted": u["pin"], "push": push}

    async def set_user_enabled(self, data: dict, enabled: bool) -> dict:
        u = self._find_user(data)
        u["enabled"] = enabled
        self.store.save_user(u)
        return {"push": await self.m.push_user(u["pin"]), "enabled": enabled}

    async def grant(self, data: dict) -> dict:
        """Crea o aggiorna una tessera (cercata per pin o numero tessera) e la invia ai pannelli."""
        existing = None
        try:
            existing = self._find_user(data)
        except ApiError:
            pass
        payload = dict(data)
        payload.pop("action", None)
        if existing:
            payload["id"] = existing["id"]
            if data.get("add", False):
                if "levels" in data:
                    payload["levels"] = list({*existing["levels"], *[self._level_id(x) for x in data["levels"]]})
                if "doors" in data:
                    payload["doors"] = list({*existing["doors"], *[self._door_id(x) for x in data["doors"]]})
        payload.setdefault("enabled", True)
        return await self.save_user(payload)

    # -------------------------------------------------------- comandi
    async def command(self, data: dict) -> Any:
        action = (data.get("action") or "").lower()
        if action in ("open", "open_door", "apri"):
            await self.m.open_door(self._door_id(data.get("door")), data.get("seconds"))
            return True
        if action in ("close", "close_door", "chiudi"):
            await self.m.close_door(self._door_id(data.get("door")))
            return True
        if action in ("normal_open", "sempre_aperta"):
            await self.m.normal_open(self._door_id(data.get("door")), bool(data.get("enable", True)))
            return True
        if action in ("grant", "assegna", "add_card"):
            return await self.grant(data)
        if action in ("revoke", "delete_card", "elimina"):
            return await self.delete_user(data)
        if action in ("disable", "disabilita"):
            return await self.set_user_enabled(data, False)
        if action in ("enable", "abilita"):
            return await self.set_user_enabled(data, True)
        if action in ("cancel_alarm", "restart", "sync_time"):
            pids = [int(data["panel_id"])] if data.get("panel_id") else list(self.m.panels)
            for pid in pids:
                await self.m.panel_action(pid, action)
            return True
        if action == "sync_all":
            return await self.m.full_sync_all(bool(data.get("remove_unknown", True)))
        if action == "learn":
            return await self.m.learn_card(float(data.get("timeout", 60)), data.get("panel_id"))
        raise ApiError(f"Azione sconosciuta: {action}")

    # ----------------------------------------------------- impostazioni
    def settings(self) -> dict:
        return {
            "event_retention_days": self.store.get_setting("event_retention_days", 365),
            "mqtt_enabled": self.store.get_setting("mqtt_enabled", True),
        }

    def save_settings(self, data: dict) -> dict:
        for k in ("event_retention_days", "mqtt_enabled"):
            if k in data:
                self.store.set_setting(k, data[k])
        return self.settings()

    def export_backup(self) -> str:
        out = {}
        for t in ("panels", "doors", "aux", "timezones", "holidays", "levels", "level_doors", "users", "user_levels", "user_doors", "settings"):
            out[t] = self.store.q(f"SELECT * FROM {t}")
        return json.dumps(out, ensure_ascii=False, indent=1)

    def import_backup(self, text: str) -> dict:
        data = json.loads(text)
        order = ("panels", "doors", "aux", "timezones", "holidays", "levels", "level_doors", "users", "user_levels", "user_doors", "settings")
        with self.store.tx():
            for t in reversed(order):
                self.store.x(f"DELETE FROM {t}")
            for t in order:
                for row in data.get(t, []):
                    cols = list(row)
                    self.store.x(f"INSERT INTO {t}({','.join(cols)}) VALUES({','.join('?' * len(cols))})", tuple(row[c] for c in cols))
        return {t: len(data.get(t, [])) for t in order}

    def path_in_data(self, name: str) -> str:
        return os.path.join(self.data_dir, name)
