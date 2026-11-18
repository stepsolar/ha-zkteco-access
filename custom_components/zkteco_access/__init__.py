"""ZKTeco Access: integrazione che usa l'add-on ZKTeco Access come unico punto di accesso ai controller.

L'integrazione non apre connessioni verso i controller C3/inBio: ogni comando passa dall'add-on,
così non ci sono due programmi che si contendono gli eventi o sovrascrivono le tessere.
"""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN, Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import AddonClient, AddonError
from .const import DOMAIN
from .coordinator import ZKTecoCoordinator

_LOGGER = logging.getLogger(__name__)
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
PLATFORMS = [Platform.LOCK, Platform.SWITCH, Platform.BINARY_SENSOR, Platform.BUTTON, Platform.SENSOR, Platform.EVENT]

type ZKTecoConfigEntry = ConfigEntry[ZKTecoCoordinator]

CARD_REF = {vol.Optional("card"): cv.string, vol.Optional("pin"): cv.string}
SCHEMA_GRANT = vol.Schema({
    **CARD_REF,
    vol.Optional("name"): cv.string,
    vol.Optional("group"): cv.string,
    vol.Optional("levels"): vol.All(cv.ensure_list, [cv.string]),
    vol.Optional("doors"): vol.All(cv.ensure_list, [cv.string]),
    vol.Optional("start_date"): cv.string,
    vol.Optional("end_date"): cv.string,
    vol.Optional("add", default=False): cv.boolean,
})
SCHEMA_REF = vol.Schema(CARD_REF)
SCHEMA_LEARN = vol.Schema({vol.Optional("timeout", default=45): vol.All(vol.Coerce(int), vol.Range(min=5, max=120))})
SCHEMA_SYNC = vol.Schema({vol.Optional("remove_unknown", default=True): cv.boolean})


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    def client() -> AddonClient:
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            return entry.runtime_data.client
        raise ServiceValidationError("Integrazione ZKTeco Access non configurata o add-on non raggiungibile")

    async def cmd(data: dict, timeout: float = 30) -> dict:
        try:
            res = await client().command({k: v for k, v in data.items() if v not in (None, "")}, timeout=timeout)
        except AddonError as err:
            raise HomeAssistantError(str(err)) from err
        return res if isinstance(res, dict) else {"result": res}

    def need_ref(call: ServiceCall) -> None:
        if not call.data.get("card") and not call.data.get("pin"):
            raise ServiceValidationError("Indica il numero tessera (card) oppure il PIN")

    async def grant(call: ServiceCall) -> ServiceResponse:
        need_ref(call)
        data = dict(call.data)
        if "group" in data:
            data["grp"] = data.pop("group")
        data["action"] = "grant"
        res = await cmd(data)
        r = res.get("result") or {}
        return {"pin": (r.get("user") or {}).get("pin"), "controller": r.get("push")}

    async def simple(action: str, call: ServiceCall) -> None:
        need_ref(call)
        await cmd({**call.data, "action": action})

    async def revoke(call: ServiceCall) -> None:
        await simple("revoke", call)

    async def enable(call: ServiceCall) -> None:
        await simple("enable", call)

    async def disable(call: ServiceCall) -> None:
        await simple("disable", call)

    async def learn(call: ServiceCall) -> ServiceResponse:
        t = call.data["timeout"]
        res = await cmd({"action": "learn", "timeout": t}, timeout=t + 10)
        card = res.get("result")
        return {"card": card.get("card") if card else None, "lettore": card.get("door") if card else None,
                "utente_esistente": card.get("known_user") if card else None}

    async def sync_all(call: ServiceCall) -> ServiceResponse:
        res = await cmd({"action": "sync_all", "remove_unknown": call.data["remove_unknown"]}, timeout=600)
        return {"risultato": res.get("result")}

    hass.services.async_register(DOMAIN, "grant_card", grant, SCHEMA_GRANT, supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, "revoke_card", revoke, SCHEMA_REF)
    hass.services.async_register(DOMAIN, "enable_card", enable, SCHEMA_REF)
    hass.services.async_register(DOMAIN, "disable_card", disable, SCHEMA_REF)
    hass.services.async_register(DOMAIN, "learn_card", learn, SCHEMA_LEARN, supports_response=SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, "sync_all", sync_all, SCHEMA_SYNC, supports_response=SupportsResponse.OPTIONAL)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ZKTecoConfigEntry) -> bool:
    client = AddonClient(async_get_clientsession(hass), entry.data[CONF_HOST], entry.data.get(CONF_PORT, 8099),
                         entry.data.get(CONF_TOKEN, ""))
    coordinator = ZKTecoCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    if not coordinator.data:
        raise ConfigEntryNotReady("add-on ZKTeco Access non raggiungibile")
    entry.runtime_data = coordinator
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, f"hub_{entry.entry_id}")}, name="ZKTeco Access",
        manufacturer="ZKTeco", model="Add-on controllo accessi", sw_version=coordinator.data.get("version"))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator.start_stream()
    entry.async_on_unload(entry.add_update_listener(_reload))
    return True


async def _reload(hass: HomeAssistant, entry: ZKTecoConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ZKTecoConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
