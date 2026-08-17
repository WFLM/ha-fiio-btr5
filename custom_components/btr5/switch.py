from __future__ import annotations

import asyncio
import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import gaia
from .const import DEFAULT_TIMEOUT, DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    mac = entry.data["mac"]
    async_add_entities([Btr5StayConnectedSwitch(entry, mac)])


class Btr5StayConnectedSwitch(SwitchEntity):
    """Opt-in: keep one Bluetooth connection open instead of one per action.

    Purely a latency optimization for the next single button press or
    battery poll — not a guarantee of connectivity. See PersistentConnection
    in gaia.py.
    """

    _attr_icon = "mdi:bluetooth-connect"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: ConfigEntry, mac: str) -> None:
        self._entry = entry
        self._mac = mac
        self._attr_name = "Stay Connected"
        self._attr_unique_id = f"{entry.entry_id}_stay_connected"
        self._attr_is_on = False
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, mac)},
            name="FiiO BTR5",
            manufacturer="FiiO",
            model="BTR5 2021",
        )

    def _data(self) -> dict:
        return self.hass.data[DOMAIN][self._entry.entry_id]

    async def async_turn_on(self, **kwargs) -> None:
        lock: asyncio.Lock = self._data()["lock"]
        persistent = gaia.PersistentConnection(self._mac)
        async with lock:
            try:
                await self.hass.async_add_executor_job(persistent.open, DEFAULT_TIMEOUT)
            except gaia.GaiaError as exc:
                raise HomeAssistantError(
                    f"Could not open a persistent Bluetooth connection to the BTR5: {exc}"
                ) from exc
        self._data()["persistent"] = persistent
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        lock: asyncio.Lock = self._data()["lock"]
        persistent = self._data().pop("persistent", None)
        if persistent is not None:
            async with lock:
                await self.hass.async_add_executor_job(persistent.close)
        self._attr_is_on = False
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        persistent = self._data().pop("persistent", None)
        if persistent is not None:
            await self.hass.async_add_executor_job(persistent.close)
