from __future__ import annotations

import logging
import re

import voluptuous as vol
from homeassistant import config_entries

from . import gaia
from .const import DEFAULT_TIMEOUT, DOMAIN

_LOGGER = logging.getLogger(__name__)

_MAC_PATTERN = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")


class Btr5ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors: dict[str, str] = {}
        if user_input is not None:
            mac = user_input["mac"].strip().upper()
            if not _MAC_PATTERN.match(mac):
                errors["base"] = "invalid_mac"
            else:
                await self.async_set_unique_id(mac)
                self._abort_if_unique_id_configured()
                try:
                    await self.hass.async_add_executor_job(gaia.read_battery, mac, DEFAULT_TIMEOUT)
                except gaia.GaiaError as exc:
                    _LOGGER.warning("BTR5 connectivity probe failed for %s: %s", mac, exc)
                    errors["base"] = "cannot_connect"
                else:
                    return self.async_create_entry(title=f"FiiO BTR5 ({mac})", data={"mac": mac})

        schema = vol.Schema({vol.Required("mac"): str})
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)
