"""Interruttori: porta "sempre aperta" e uscite ausiliarie."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import AddonError
from .entity import DoorEntity, PanelEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coord = entry.runtime_data
    ents: list[SwitchEntity] = [ZKNormalOpen(coord, d) for d in coord.data["doors"].values() if not d.get("hidden")]
    ents += [ZKAuxOut(coord, a) for a in coord.data["aux"].values() if a["kind"] == "out" and not a.get("hidden")]
    async_add_entities(ents)


class ZKNormalOpen(DoorEntity, SwitchEntity):
    _attr_icon = "mdi:door-sliding-open"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, door: dict) -> None:
        super().__init__(coordinator, door, "normal_open")

    @property
    def name(self) -> str:
        return f"{self.door.get('name')} sempre aperta"

    @property
    def is_on(self) -> bool | None:
        st = self.door_state
        return bool(st.get("normal_open")) if st else None

    async def _set(self, on: bool) -> None:
        try:
            await self.coordinator.client.normal_open(self.door_id, on)
        except AddonError as err:
            raise HomeAssistantError(str(err)) from err

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)


class ZKAuxOut(PanelEntity, SwitchEntity):
    _attr_icon = "mdi:electric-switch"

    def __init__(self, coordinator, aux: dict) -> None:
        super().__init__(coordinator, aux["panel_id"], f"auxout{aux['id']}")
        self.aux_id, self.aux_no = aux["id"], aux["aux_no"]
        self._attr_name = aux["name"]

    @property
    def is_on(self) -> bool | None:
        v = (self.panel.get("aux_out") or {}).get(str(self.aux_no))
        return None if v is None else bool(v)

    async def _set(self, seconds: int) -> None:
        try:
            await self.coordinator.client.aux_output(self.aux_id, seconds)
        except AddonError as err:
            raise HomeAssistantError(str(err)) from err

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(255)  # 255 = attiva finché non viene spenta

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(0)
