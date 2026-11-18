"""Serrature: una per porta. Sblocca/Apri = impulso di apertura, Blocca = richiude subito."""

from __future__ import annotations

from typing import Any

from homeassistant.components.lock import LockEntity, LockEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import AddonError
from .const import CONF_UNLOCK_SECONDS
from .entity import DoorEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coord = entry.runtime_data
    async_add_entities(ZKDoorLock(coord, d) for d in coord.data["doors"].values() if not d.get("hidden"))


class ZKDoorLock(DoorEntity, LockEntity):
    _attr_supported_features = LockEntityFeature.OPEN

    def __init__(self, coordinator, door: dict) -> None:
        super().__init__(coordinator, door, "lock")
        self._attr_name = door["name"]

    @property
    def name(self) -> str:
        return self.door.get("name") or self._attr_name

    @property
    def is_locked(self) -> bool | None:
        st = self.door_state
        if not st:
            return None
        return not (st.get("unlocked") or st.get("normal_open"))

    @property
    def is_open(self) -> bool | None:
        sensor = self.door_state.get("sensor")
        return True if sensor == 2 else False if sensor == 1 else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        st = self.door_state
        last = st.get("last_event") or {}
        return {
            "controller": self.panel.get("name"),
            "porta_n": self.door.get("door_no"),
            "sempre_aperta": st.get("normal_open"),
            "allarme": bool(st.get("alarm")),
            "ultimo_evento": last.get("description"),
            "ultimo_utente": last.get("user") or None,
            "ultima_tessera": last.get("card") or None,
        }

    def _seconds(self) -> int | None:
        return self.coordinator.config_entry.options.get(CONF_UNLOCK_SECONDS) or None

    async def _do(self, coro) -> None:
        try:
            await coro
        except AddonError as err:
            raise HomeAssistantError(f"{self.name}: {err}") from err

    async def async_unlock(self, **kwargs: Any) -> None:
        await self._do(self.coordinator.client.open_door(self.door_id, self._seconds()))

    async def async_open(self, **kwargs: Any) -> None:
        await self._do(self.coordinator.client.open_door(self.door_id, self._seconds()))

    async def async_lock(self, **kwargs: Any) -> None:
        await self._do(self.coordinator.client.close_door(self.door_id))
