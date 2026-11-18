"""Coordinatore: stato dall'add-on via REST e aggiornamenti in tempo reale via stream."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import AddonClient, AddonError
from .const import DOMAIN, SIGNAL_EVENT

_LOGGER = logging.getLogger(__name__)


class ZKTecoCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Tiene in memoria pannelli, porte e ausiliari come li vede l'add-on."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: AddonClient) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=DOMAIN, update_interval=timedelta(minutes=5))
        self.client = client
        self._stream_task: asyncio.Task | None = None
        self.stream_connected = False
        self.structure: tuple = ()

    # ------------------------------------------------------------ dati
    async def _async_update_data(self) -> dict[str, Any]:
        try:
            state = await self.client.state()
        except AddonError as err:
            raise UpdateFailed(str(err)) from err
        return self._index(state)

    @staticmethod
    def _index(state: dict) -> dict[str, Any]:
        return {
            "version": state.get("version"),
            "panels": {p["id"]: p for p in state.get("panels", [])},
            "doors": {d["id"]: d for d in state.get("doors", [])},
            "aux": {a["id"]: a for a in state.get("aux", [])},
            "levels": state.get("levels", []),
        }

    @staticmethod
    def structure_of(data: dict) -> tuple:
        return (
            tuple(sorted(data["panels"])),
            tuple(sorted(i for i, d in data["doors"].items() if not d.get("hidden"))),
            tuple(sorted(i for i, a in data["aux"].items() if not a.get("hidden"))),
        )

    def panel(self, panel_id: int) -> dict:
        return self.data["panels"].get(panel_id) or {}

    def door_state(self, door: dict) -> dict:
        p = self.panel(door["panel_id"])
        return (p.get("doors") or {}).get(str(door["door_no"])) or (p.get("doors") or {}).get(door["door_no"]) or {}

    # ---------------------------------------------------------- stream
    @callback
    def start_stream(self) -> None:
        self.structure = self.structure_of(self.data)
        self._stream_task = self.config_entry.async_create_background_task(
            self.hass, self._stream_loop(), f"{DOMAIN}_stream")

    async def async_shutdown(self) -> None:
        if self._stream_task:
            self._stream_task.cancel()
        await super().async_shutdown()

    async def _stream_loop(self) -> None:
        delay = 2
        while True:
            try:
                async for msg in self.client.stream():
                    if not self.stream_connected:
                        self.stream_connected = True
                        delay = 2
                        _LOGGER.debug("Stream add-on connesso")
                    await self._handle(msg)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                if self.stream_connected:
                    _LOGGER.warning("Connessione in tempo reale con l'add-on persa: %s", err)
            self.stream_connected = False
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)
            # dopo una disconnessione riallinea tutto
            await self.async_request_refresh()

    async def _handle(self, msg: dict) -> None:
        kind, data = msg.get("type"), msg.get("data") or {}
        if kind == "state":
            panels = {p["id"]: p for p in data.get("panels", [])}
            if panels:
                self.async_set_updated_data({**self.data, "panels": panels})
        elif kind == "panel" and "id" in data:
            self.async_set_updated_data({**self.data, "panels": {**self.data["panels"], data["id"]: data}})
        elif kind == "event":
            async_dispatcher_send(self.hass, f"{SIGNAL_EVENT}_{self.config_entry.entry_id}", data)
        elif kind == "panels":
            await self.async_refresh()
            if self.data and self.structure_of(self.data) != self.structure:
                _LOGGER.info("Configurazione porte/controller cambiata nell'add-on: ricarico l'integrazione")
                self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)
