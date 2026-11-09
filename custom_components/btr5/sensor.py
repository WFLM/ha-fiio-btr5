from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from . import gaia
from .const import BATTERY_POLL_INTERVAL_MINUTES, DEFAULT_TIMEOUT, DOMAIN

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(seconds=3)

_CONNECTION_STATUS_ONE_TIME = "one_time"
_CONNECTION_STATUS_OPTIONS = [
    _CONNECTION_STATUS_ONE_TIME,
    gaia.PersistentConnection.STATUS_DISCONNECTED,
    gaia.PersistentConnection.STATUS_CONNECTING,
    gaia.PersistentConnection.STATUS_CONNECTED,
]


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    mac = entry.data["mac"]
    data = hass.data[DOMAIN][entry.entry_id]
    lock: asyncio.Lock = data["lock"]
    entity_registry = er.async_get(hass)
    legacy_battery_entity_id = entity_registry.async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_battery"
    )
    if legacy_battery_entity_id is not None:
        entity_registry.async_remove(legacy_battery_entity_id)

    async def _async_update_data() -> int:
        async with lock:
            # Stay Connected may have been switched while waiting for the lock.
            persistent = data.get("persistent")
            try:
                return await hass.async_add_executor_job(
                    gaia.read_battery_persistent_aware, persistent, mac, DEFAULT_TIMEOUT
                )
            except gaia.GaiaError as exc:
                raise UpdateFailed(str(exc)) from exc

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name=f"{DOMAIN}_battery_{entry.entry_id}",
        update_method=_async_update_data,
        update_interval=timedelta(minutes=BATTERY_POLL_INTERVAL_MINUTES),
    )
    data["battery_coordinator"] = coordinator
    # The first poll comes one interval after setup. One at setup isn't
    # needed: nothing reads the value, and with Stay Connected on, the held
    # link's own battery read checks the connection.
    data["battery_poll_unsubscribe"] = coordinator.async_add_listener(lambda: None)

    async_add_entities([Btr5ConnectionStatusSensor(entry, mac, data)])


class Btr5ConnectionStatusSensor(SensorEntity):
    """Stay Connected's connection state, polled: see PersistentConnection."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = _CONNECTION_STATUS_OPTIONS
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:bluetooth-transfer"
    _attr_translation_key = "connection_status"

    def __init__(self, entry: ConfigEntry, mac: str, data: dict) -> None:
        # Kept after the entry unloads, when hass.data no longer has it.
        self._data = data
        self._attr_name = "Connection Status"
        self._attr_unique_id = f"{entry.entry_id}_connection_status"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, mac)},
            name="FiiO BTR5",
            manufacturer="FiiO",
            model="BTR5 2021",
        )

    @property
    def native_value(self) -> str:
        persistent = self._data.get("persistent")
        # Stopped: Stay Connected is off, and the link is about to close.
        if persistent is None or persistent.stopped:
            return _CONNECTION_STATUS_ONE_TIME
        return persistent.status
