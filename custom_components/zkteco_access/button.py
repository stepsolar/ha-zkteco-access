"""Pulsanti: apri porta, e per ogni controller annulla allarmi / sincronizza ora / riavvia."""

from __future__ import annotations

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import AddonError
from .const import CONF_UNLOCK_SECONDS
from .entity import DoorEntity, PanelEntity

PANEL_BUTTONS = (
    ("cancel_alarm", "Annulla allarmi", "mdi:alarm-off", None, None),
    ("sync_time", "Sincronizza ora", "mdi:clock-check-outline", EntityCategory.CONFIG, None),
    ("restart", "Riavvia", None, EntityCategory.CONFIG, ButtonDeviceClass.RESTART),
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coord = entry.runtime_data
    ents: list[ButtonEntity] = [ZKOpenButton(coord, d) for d in coord.data["doors"].values() if not d.get("hidden")]
    for pid in coord.data["panels"]:
        ents += [ZKPanelButton(coord, pid, *b) for b in PANEL_BUTTONS]
    async_add_entities(ents)


class ZKOpenButton(DoorEntity, ButtonEntity):
    _attr_icon = "mdi:door-open"

    def __init__(self, coordinator, door: dict) -> None:
        super().__init__(coordinator, door, "open")

    @property
    def name(self) -> str:
        return f"Apri {self.door.get('name')}"

    async def async_press(self) -> None:
        try:
            await self.coordinator.client.open_door(
                self.door_id, self.coordinator.config_entry.options.get(CONF_UNLOCK_SECONDS) or None)
        except AddonError as err:
            raise HomeAssistantError(str(err)) from err


class ZKPanelButton(PanelEntity, ButtonEntity):
    def __init__(self, coordinator, panel_id: int, action: str, name: str, icon, category, device_class) -> None:
        super().__init__(coordinator, panel_id, f"btn_{action}")
        self.action = action
        self._attr_name = name
        self._attr_icon = icon
        self._attr_entity_category = category
        self._attr_device_class = device_class

    async def async_press(self) -> None:
        try:
            await self.coordinator.client.panel_action(self.panel_id, self.action)
        except AddonError as err:
            raise HomeAssistantError(str(err)) from err
