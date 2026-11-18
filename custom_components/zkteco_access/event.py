"""Entità evento: una per porta e una generale, per le automazioni (accesso consentito/negato, tessera sconosciuta...)."""

from __future__ import annotations

from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import EVENT_TYPES, SIGNAL_EVENT
from .entity import DoorEntity, hub_device
from .sensor import event_attrs


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coord = entry.runtime_data
    ents: list[EventEntity] = [ZKDoorEvent(coord, d) for d in coord.data["doors"].values() if not d.get("hidden")]
    ents.append(ZKAllEvents(coord))
    async_add_entities(ents)


class _Base(EventEntity):
    _attr_event_types = EVENT_TYPES
    _attr_icon = "mdi:card-account-details-outline"

    def _match(self, ev: dict) -> bool:
        return True

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(async_dispatcher_connect(
            self.hass, f"{SIGNAL_EVENT}_{self.coordinator.config_entry.entry_id}", self._on_event))

    @callback
    def _on_event(self, ev: dict) -> None:
        if not self._match(ev):
            return
        cat = ev.get("category") if ev.get("category") in EVENT_TYPES else "info"
        self._trigger_event(cat, event_attrs(ev))
        self.async_write_ha_state()


class ZKDoorEvent(DoorEntity, _Base):
    def __init__(self, coordinator, door: dict) -> None:
        super().__init__(coordinator, door, "event")

    @property
    def name(self) -> str:
        return f"{self.door.get('name')} accessi"

    def _match(self, ev: dict) -> bool:
        d = self.door
        return not ev.get("aux") and ev.get("panel_id") == d.get("panel_id") and ev.get("door_no") == d.get("door_no")


class ZKAllEvents(CoordinatorEntity, _Base):
    _attr_has_entity_name = True
    _attr_name = "Eventi accesso"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_all_events"
        self._attr_device_info = hub_device(coordinator)

    @property
    def available(self) -> bool:
        return True
