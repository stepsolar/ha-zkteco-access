"""Configurazione: scoperta automatica dall'add-on (Supervisor) oppure inserimento manuale."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service_info.hassio import HassioServiceInfo

from .api import AddonClient, AddonError
from .const import CONF_UNLOCK_SECONDS, DEFAULT_PORT, DOMAIN


class ZKTecoConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._host: str | None = None
        self._port: int = DEFAULT_PORT
        self._addon_name = "ZKTeco Access"

    async def _check(self, host: str, port: int, token: str = "") -> dict:
        return await AddonClient(async_get_clientsession(self.hass), host, port, token).state()

    async def async_step_hassio(self, discovery_info: HassioServiceInfo) -> ConfigFlowResult:
        """L'add-on si è annunciato al Supervisor."""
        await self.async_set_unique_id(DOMAIN)
        self._host = discovery_info.config["host"]
        self._port = int(discovery_info.config.get("port", DEFAULT_PORT))
        self._abort_if_unique_id_configured(updates={CONF_HOST: self._host, CONF_PORT: self._port})
        self._addon_name = discovery_info.name or self._addon_name
        return await self.async_step_hassio_confirm()

    async def async_step_hassio_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await self._check(self._host, self._port)
            except AddonError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(title="ZKTeco Access", data={CONF_HOST: self._host, CONF_PORT: self._port})
        self._set_confirm_only()
        return self.async_show_form(step_id="hassio_confirm", errors=errors,
                                    description_placeholders={"addon": self._addon_name})

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                st = await self._check(user_input[CONF_HOST], user_input[CONF_PORT], user_input.get(CONF_TOKEN, ""))
            except AddonError:
                errors["base"] = "cannot_connect"
            else:
                if "doors" not in st:
                    errors["base"] = "not_addon"
                else:
                    return self.async_create_entry(title="ZKTeco Access", data=user_input)
        schema = vol.Schema({
            vol.Required(CONF_HOST, default=(user_input or {}).get(CONF_HOST, "local-zkteco-access")): str,
            vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
            vol.Optional(CONF_TOKEN, default=""): str,
        })
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return ZKTecoOptionsFlow()


class ZKTecoOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        cur = self.config_entry.options.get(CONF_UNLOCK_SECONDS, 0)
        return self.async_show_form(step_id="init", data_schema=vol.Schema({
            vol.Required(CONF_UNLOCK_SECONDS, default=cur): vol.All(vol.Coerce(int), vol.Range(min=0, max=254)),
        }))
