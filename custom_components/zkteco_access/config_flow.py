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
        self._error = ""

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
            except AddonError as err:
                errors["base"] = "forbidden" if err.kind == "forbidden" else "cannot_connect"
                self._error = str(err)
            else:
                return self.async_create_entry(title="ZKTeco Access", data={CONF_HOST: self._host, CONF_PORT: self._port})
        self._set_confirm_only()
        return self.async_show_form(step_id="hassio_confirm", errors=errors,
                                    description_placeholders={"addon": self._addon_name,
                                                              "error": f"Errore: {self._error}" if errors else ""})

    def _addon_hosts(self) -> list[str]:
        """Nomi host dell'add-on installato (ricavati dal Supervisor), più quello dell'add-on locale."""
        hosts: list[str] = []
        try:
            from homeassistant.components.hassio import get_addons_list, hostname_from_addon_slug

            for addon in get_addons_list(self.hass) or []:
                if str(addon.get("slug", "")).endswith("zkteco_access"):
                    hosts.append(hostname_from_addon_slug(addon["slug"]))
        except Exception:  # noqa: BLE001 - Supervisor assente o non pronto
            pass
        if "local-zkteco-access" not in hosts:
            hosts.append("local-zkteco-access")
        return hosts

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        errors: dict[str, str] = {}
        detail = ""
        hosts = self._addon_hosts()
        if user_input is None:
            # prova da sola gli host dell'add-on: se uno risponde, chiede solo conferma
            for host in hosts:
                try:
                    await self._check(host, DEFAULT_PORT)
                except AddonError:
                    continue
                self._host, self._port = host, DEFAULT_PORT
                return await self.async_step_hassio_confirm()
        else:
            try:
                st = await self._check(user_input[CONF_HOST].strip(), user_input[CONF_PORT], user_input.get(CONF_TOKEN, ""))
            except AddonError as err:
                errors["base"] = "forbidden" if err.kind == "forbidden" else "cannot_connect"
                detail = str(err)
            else:
                if "doors" not in st:
                    errors["base"] = "not_addon"
                else:
                    return self.async_create_entry(title="ZKTeco Access", data={**user_input, CONF_HOST: user_input[CONF_HOST].strip()})
        schema = vol.Schema({
            vol.Required(CONF_HOST, default=(user_input or {}).get(CONF_HOST, hosts[0])): str,
            vol.Required(CONF_PORT, default=(user_input or {}).get(CONF_PORT, DEFAULT_PORT)): int,
            vol.Optional(CONF_TOKEN, default=(user_input or {}).get(CONF_TOKEN, "")): str,
        })
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors,
                                    description_placeholders={"error": detail, "hosts": ", ".join(hosts)})

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
