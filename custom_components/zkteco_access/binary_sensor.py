"""Sensori binari: contatto porta, allarme porta, ingressi ausiliari, connessione controller."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import DoorEntity, PanelEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coord = entry.runtime_data
    ents: list[BinarySensorEntity] = []
    for d in coord.data["doors"].values():
        if not d.get("hidden"):
            ents += [ZKDoorContact(coord, d), ZKDoorAlarm(coord, d)]
    ents += [ZKAuxIn(coord, a) for a in coord.data["aux"].values() if a["kind"] == "in" and not a.get("hidden")]
    ents += [ZKPanelOnline(coord, pid) for pid in coord.data["panels"]]
    async_add_entities(ents)


class ZKDoorContact(DoorEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.DOOR

    def __init__(self, coordinator, door: dict) -> None:
        super().__init__(coordinator, door, "contact")

    @property
    def name(self) -> str:
        return f"{self.door.get('name')} sensore"

    @property
    def is_on(self) -> bool | None:
        s = self.door_state.get("sensor")
        return True if s == 2 else False if s == 1 else None


class ZKDoorAlarm(DoorEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator, door: dict) -> None:
        super().__init__(coordinator, door, "alarm")

    @property
    def name(self) -> str:
        return f"{self.door.get('name')} allarme"

    @property
    def is_on(self) -> bool | None:
        st = self.door_state
        return bool(st.get("alarm")) if st else None

    @property
    def extra_state_attributes(self) -> dict:
        a = int(self.door_state.get("alarm") or 0)
        return {"codice_allarme": a, "porta_aperta_troppo_a_lungo": bool(a & 2)}


class ZKAuxIn(PanelEntity, BinarySensorEntity):
    def __init__(self, coordinator, aux: dict) -> None:
        super().__init__(coordinator, aux["panel_id"], f"auxin{aux['id']}")
        self.aux_no = aux["aux_no"]
        self._attr_name = aux["name"]

    @property
    def is_on(self) -> bool | None:
        v = (self.panel.get("aux_in") or {}).get(str(self.aux_no))
        return None if v is None else bool(v)


class ZKPanelOnline(PanelEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Connessione"

    def __init__(self, coordinator, panel_id: int) -> None:
        super().__init__(coordinator, panel_id, "online")

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    @property
    def is_on(self) -> bool:
        return bool(self.panel.get("online"))

    @property
    def extra_state_attributes(self) -> dict:
        p = self.panel
        return {"indirizzo": p.get("host"), "errore": p.get("error") or None, "modifiche_in_coda": p.get("queue", 0)}
