from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
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
    lock: asyncio.Lock = hass.data[DOMAIN][entry.entry_id]["lock"]

    async def _async_update_data() -> int:
        persistent = hass.data[DOMAIN][entry.entry_id].get("persistent")
        async with lock:
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
    hass.data[DOMAIN][entry.entry_id]["battery_coordinator"] = coordinator
    await coordinator.async_refresh()

    async_add_entities(
        [
            Btr5BatterySensor(entry, mac, coordinator),
            Btr5ConnectionStatusSensor(entry, mac),
        ]
    )


class Btr5BatterySensor(CoordinatorEntity[DataUpdateCoordinator[int]], SensorEntity):
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self, entry: ConfigEntry, mac: str, coordinator: DataUpdateCoordinator
    ) -> None:
        super().__init__(coordinator)
        self._attr_name = "Battery"
        self._attr_unique_id = f"{entry.entry_id}_battery"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, mac)},
            name="FiiO BTR5",
            manufacturer="FiiO",
            model="BTR5 2021",
        )

    @property
    def native_value(self) -> int | None:
        return self.coordinator.data


class Btr5ConnectionStatusSensor(SensorEntity):
    """Reports Stay Connected's current connection state.

    Plain-polled (see module-level SCAN_INTERVAL) rather than pushed: the
    persistent connection's status is just an attribute on a plain object
    with no event-loop access of its own to push updates from.
    """

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = _CONNECTION_STATUS_OPTIONS
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:bluetooth-transfer"
    _attr_translation_key = "connection_status"

    def __init__(self, entry: ConfigEntry, mac: str) -> None:
        self._entry = entry
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
        persistent = self.hass.data[DOMAIN][self._entry.entry_id].get("persistent")
        if persistent is None:
            return _CONNECTION_STATUS_ONE_TIME
        return persistent.status
