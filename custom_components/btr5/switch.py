from __future__ import annotations

import asyncio
import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import gaia
from .const import CONF_STAY_CONNECTED, DEFAULT_STAY_CONNECTED, DEFAULT_TIMEOUT, DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    mac = entry.data["mac"]
    async_add_entities([Btr5StayConnectedSwitch(entry, mac, hass.data[DOMAIN][entry.entry_id])])


class Btr5StayConnectedSwitch(SwitchEntity):
    """Keep one Bluetooth connection open instead of one per action.

    Turned on, it connects in the background, retrying for a while; button
    presses and battery polls then use that link and reconnect it when it
    breaks. See PersistentConnection in gaia.py.
    """

    _attr_icon = "mdi:bluetooth-connect"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: ConfigEntry, mac: str, data: dict) -> None:
        self._entry = entry
        self._mac = mac
        # Kept after the entry unloads, when hass.data no longer has it.
        self._data = data
        self._attr_name = "Stay Connected"
        self._attr_unique_id = f"{entry.entry_id}_stay_connected"
        self._attr_is_on = entry.options.get(
            CONF_STAY_CONNECTED, DEFAULT_STAY_CONNECTED
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, mac)},
            name="FiiO BTR5",
            manufacturer="FiiO",
            model="BTR5 2021",
        )
        self._connect_task: asyncio.Task | None = None
        # Asked to connect while a connect was under way.
        self._connect_again = False

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if CONF_STAY_CONNECTED not in self._entry.options:
            self._save_preference(self._attr_is_on)
        if self._attr_is_on:
            self._start_persistent_connection()

    async def async_turn_on(self, **kwargs) -> None:
        if not self._attr_is_on:
            self._attr_is_on = True
            self._save_preference(True)
            self.async_write_ha_state()
        # Even when on already: checks the link, reconnecting if it's down.
        self._start_persistent_connection()

    async def async_turn_off(self, **kwargs) -> None:
        self._attr_is_on = False
        self._save_preference(False)
        self.async_write_ha_state()
        await self._async_close_persistent_connection()

    async def async_will_remove_from_hass(self) -> None:
        await self._async_close_persistent_connection()

    def _save_preference(self, enabled: bool) -> None:
        self.hass.config_entries.async_update_entry(
            self._entry,
            options={**self._entry.options, CONF_STAY_CONNECTED: enabled},
        )

    @callback
    def _start_persistent_connection(self) -> None:
        # Home Assistant's stop event has come and gone: nothing would end
        # the retries.
        if self.hass.is_stopping:
            return
        persistent = self._data.get("persistent")
        if persistent is None:
            persistent = gaia.PersistentConnection(self._mac)
            self._data["persistent"] = persistent
        else:
            # Turned back on before the link was closed: it's kept.
            persistent.resume()
            # A connect under way carries on, and if it fails, runs one more
            # round of retries, however many requests came meanwhile.
            if self._connect_task is not None and not self._connect_task.done():
                self._connect_again = True
                return
        # Connecting may take a while, so it holds up neither setup nor the
        # service call.
        self._connect_task = self._entry.async_create_background_task(
            self.hass,
            self._async_open_persistent_connection(persistent),
            f"{DOMAIN} open connection {self._mac}",
        )

    async def _async_close_persistent_connection(self) -> None:
        persistent = self._data.get("persistent")
        if persistent is None:
            return
        # Ends a retry pause at once, so the lock comes free sooner.
        persistent.stop()
        # Closed even if the turn_off call is cancelled while waiting.
        await asyncio.shield(
            self._entry.async_create_background_task(
                self.hass,
                self._async_close_once_free(persistent),
                f"{DOMAIN} close connection {self._mac}",
            )
        )

    async def _async_close_once_free(self, persistent: gaia.PersistentConnection) -> None:
        lock: asyncio.Lock = self._data["lock"]
        async with lock:
            # Turned back on meanwhile: the link stays.
            if not persistent.stopped:
                return
            if self._data.get("persistent") is persistent:
                del self._data["persistent"]
            await self.hass.async_add_executor_job(persistent.close)

    async def _async_open_persistent_connection(self, persistent: gaia.PersistentConnection) -> None:
        lock: asyncio.Lock = self._data["lock"]
        async with lock:
            while True:
                self._connect_again = False
                try:
                    await self.hass.async_add_executor_job(
                        gaia.connect_persistent, persistent, DEFAULT_TIMEOUT
                    )
                except gaia.GaiaCancelledError:
                    # Turned back on just as it gave up: connect after all.
                    if not persistent.stopped:
                        continue
                except gaia.GaiaError as exc:
                    # Asked again meanwhile, maybe as the BTR5 came back.
                    if self._connect_again:
                        continue
                    _LOGGER.warning(
                        "Stay Connected remains enabled for BTR5 %s, but connecting failed: %s",
                        self._mac,
                        exc,
                    )
                return
