"""Entità base: un dispositivo Home Assistant per ogni controller, collegato all'hub add-on."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ZKTecoCoordinator


def hub_device(coordinator: ZKTecoCoordinator) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, f"hub_{coordinator.config_entry.entry_id}")},
        name="ZKTeco Access",
        manufacturer="ZKTeco",
        model="Add-on controllo accessi",
        sw_version=coordinator.data.get("version"),
    )


class PanelEntity(CoordinatorEntity[ZKTecoCoordinator]):
    """Entità legata a un controller."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: ZKTecoCoordinator, panel_id: int, key: str) -> None:
        super().__init__(coordinator)
        self.panel_id = panel_id
        p = coordinator.panel(panel_id)
        info = p.get("info") or {}
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_p{panel_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{coordinator.config_entry.entry_id}_panel_{panel_id}")},
            name=p.get("name") or f"Controller {panel_id}",
            manufacturer="ZKTeco",
            model=info.get("~DeviceName") or "C3/inBio",
            sw_version=info.get("FirmVer"),
            serial_number=info.get("~SerialNumber"),
            via_device=(DOMAIN, f"hub_{coordinator.config_entry.entry_id}"),
        )

    @property
    def panel(self) -> dict:
        return self.coordinator.panel(self.panel_id)

    @property
    def available(self) -> bool:
        return super().available and bool(self.panel.get("online"))


class DoorEntity(PanelEntity):
    """Entità legata a una porta."""

    def __init__(self, coordinator: ZKTecoCoordinator, door: dict, key: str) -> None:
        super().__init__(coordinator, door["panel_id"], f"door{door['id']}_{key}")
        self.door_id = door["id"]

    @property
    def door(self) -> dict:
        return self.coordinator.data["doors"].get(self.door_id) or {}

    @property
    def door_state(self) -> dict:
        return self.coordinator.door_state(self.door)
