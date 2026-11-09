from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

import voluptuous as vol
from homeassistant import config_entries

from . import gaia
from .const import DEFAULT_TIMEOUT, DOMAIN

if TYPE_CHECKING:
    from homeassistant.components.bluetooth import BluetoothServiceInfoBleak

_LOGGER = logging.getLogger(__name__)

_MAC_PATTERN = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
# The name in the BTR5's BLE advertisement, matched in manifest.json too.
_ADVERTISED_NAME = "FiiO BTR5"
_MANUAL = "manual"


class Btr5ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._mac: str | None = None

    async def async_step_bluetooth(self, discovery_info: BluetoothServiceInfoBleak):
        """A BTR5 found by its BLE advertisement, sent from its Bluetooth address."""
        self._mac = discovery_info.address.upper()
        await self.async_set_unique_id(self._mac)
        self._abort_if_unique_id_configured()
        self.context["title_placeholders"] = {"mac": self._mac}
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(self, user_input=None):
        errors: dict[str, str] = {}
        if user_input is not None and await self._async_probe(self._mac, errors):
            return self._create_entry(self._mac)
        self._set_confirm_only()
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={"mac": self._mac},
            errors=errors,
        )

    async def async_step_user(self, user_input=None):
        """Pick a BTR5 found nearby, or enter its MAC address."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input["mac"] == _MANUAL:
                return await self.async_step_manual()
            # Not always one picked from the list: a flow can be started
            # with data.
            if entry := await self._async_try_mac(user_input["mac"], errors):
                return entry

        nearby = self._nearby_macs()
        if not nearby:
            return self._show_manual_form(errors)
        options = {mac: f"{_ADVERTISED_NAME} ({mac})" for mac in nearby}
        options[_MANUAL] = "Enter the MAC address"
        schema = vol.Schema({vol.Required("mac"): vol.In(options)})
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_manual(self, user_input=None):
        errors: dict[str, str] = {}
        if user_input is not None and (
            entry := await self._async_try_mac(user_input["mac"], errors)
        ):
            return entry
        return self._show_manual_form(errors)

    def _show_manual_form(self, errors: dict[str, str]):
        schema = vol.Schema({vol.Required("mac"): str})
        return self.async_show_form(step_id="manual", data_schema=schema, errors=errors)

    def _nearby_macs(self) -> list[str]:
        """BTR5s Home Assistant's Bluetooth integration has heard, not yet set up."""
        # Until that integration is loaded there's nothing to list.
        if "bluetooth" not in self.hass.config.components:
            return []
        from homeassistant.components.bluetooth import async_discovered_service_info

        configured = self._async_current_ids(include_ignore=False)
        return sorted(
            {
                info.address.upper()
                for info in async_discovered_service_info(self.hass, connectable=False)
                if (info.name or "").startswith(_ADVERTISED_NAME)
            }
            - set(configured)
        )

    async def _async_try_mac(self, text: str, errors: dict[str, str]):
        """The new entry, if ``text`` is the MAC address of a BTR5 that answers."""
        mac = text.strip().upper()
        if not _MAC_PATTERN.match(mac):
            errors["base"] = "invalid_mac"
            return None
        if not await self._async_probe(mac, errors):
            return None
        return self._create_entry(mac)

    async def _async_probe(self, mac: str, errors: dict[str, str]) -> bool:
        """Check the BTR5 answers, unless it's set up already (that aborts)."""
        # A discovery card for the same BTR5 doesn't stop setting it up here.
        await self.async_set_unique_id(mac, raise_on_progress=False)
        self._abort_if_unique_id_configured()
        try:
            await self.hass.async_add_executor_job(gaia.read_battery, mac, DEFAULT_TIMEOUT)
        except gaia.GaiaError as exc:
            _LOGGER.warning("BTR5 connectivity probe failed for %s: %s", mac, exc)
            errors["base"] = "cannot_connect"
            if self.source != config_entries.SOURCE_BLUETOOTH:
                # Kept, it would abort a discovery of this BTR5 meanwhile.
                await self.async_set_unique_id(None)
            return False
        return True

    def _create_entry(self, mac: str):
        return self.async_create_entry(title=f"FiiO BTR5 ({mac})", data={"mac": mac})
