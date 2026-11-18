"""Sensori: ultimo evento per porta, ultima tessera letta."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import SIGNAL_EVENT
from .entity import DoorEntity, hub_device


def event_attrs(ev: dict) -> dict[str, Any]:
    return {
        "controller": ev.get("panel"), "porta": ev.get("door"), "evento": ev.get("description"),
        "categoria": ev.get("category"), "utente": ev.get("user") or None, "pin": ev.get("pin") or None,
        "tessera": ev.get("card") or None, "verifica": ev.get("verify_text") or None,
        "direzione": ev.get("inout_text") or None, "codice": ev.get("event"),
        "ora": datetime.fromtimestamp(ev.get("ts", 0), timezone.utc).isoformat() if ev.get("ts") else None,
    }


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coord = entry.runtime_data
    ents: list[SensorEntity] = [ZKLastEvent(coord, d) for d in coord.data["doors"].values() if not d.get("hidden")]
    ents.append(ZKLastCard(coord))
    async_add_entities(ents)


class ZKLastEvent(DoorEntity, SensorEntity):
    _attr_icon = "mdi:account-key"

    def __init__(self, coordinator, door: dict) -> None:
        super().__init__(coordinator, door, "last_event")
        self._ev: dict | None = None

    @property
    def name(self) -> str:
        return f"{self.door.get('name')} ultimo evento"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(async_dispatcher_connect(
            self.hass, f"{SIGNAL_EVENT}_{self.coordinator.config_entry.entry_id}", self._on_event))

    @callback
    def _on_event(self, ev: dict) -> None:
        d = self.door
        if ev.get("aux") or ev.get("panel_id") != d.get("panel_id") or ev.get("door_no") != d.get("door_no"):
            return
        if ev.get("category") == "door":
            return
        self._ev = ev
        self.async_write_ha_state()

    @property
    def native_value(self) -> str | None:
        if self._ev:
            ev = self._ev
            who = ev.get("user") or (f"tessera {ev['card']}" if ev.get("card") else "")
            return (f"{ev.get('description')} – {who}" if who else ev.get("description"))[:255]
        last = self.door_state.get("last_event")
        if last:
            who = last.get("user") or (f"tessera {last['card']}" if last.get("card") else "")
            return (f"{last.get('description')} – {who}" if who else last.get("description"))[:255]
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        return event_attrs(self._ev) if self._ev else None


class ZKLastCard(CoordinatorEntity, RestoreEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_name = "Ultima tessera letta"
    _attr_icon = "mdi:card-account-details"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_last_card"
        self._attr_device_info = hub_device(coordinator)
        self._ev: dict | None = None
        self._attr_native_value = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) and last.state not in ("unknown", "unavailable"):
            self._attr_native_value = last.state
        self.async_on_remove(async_dispatcher_connect(
            self.hass, f"{SIGNAL_EVENT}_{self.coordinator.config_entry.entry_id}", self._on_event))

    @callback
    def _on_event(self, ev: dict) -> None:
        if ev.get("card") and ev.get("category") != "door":
            self._ev = ev
            self._attr_native_value = ev["card"]
            self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        return event_attrs(self._ev) if self._ev else None
