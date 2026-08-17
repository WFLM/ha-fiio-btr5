from __future__ import annotations

import asyncio
import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later

from . import gaia
from .const import DEFAULT_DEBOUNCE_SECONDS, DEFAULT_STEP_SIZE, DEFAULT_TIMEOUT, DOMAIN

_LOGGER = logging.getLogger(__name__)

_ERROR_MESSAGES = {
    gaia.GaiaDiscoveryError: "Could not find BTR5's GAIA Bluetooth service. Is it powered on and in range?",
    gaia.GaiaConnectError: "BTR5 refused the Bluetooth connection. It may already be connected to another host.",
    gaia.GaiaAckTimeoutError: "BTR5 did not acknowledge the volume command in time.",
    gaia.GaiaAckError: "BTR5 rejected the volume command.",
}

_ICONS = {
    "up": "mdi:volume-plus",
    "down": "mdi:volume-minus",
}


def _error_message(exc: gaia.GaiaError) -> str:
    return _ERROR_MESSAGES.get(type(exc), str(exc))


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    mac = entry.data["mac"]
    lock = hass.data[DOMAIN][entry.entry_id]["lock"]
    async_add_entities(
        [
            Btr5VolumeButton(entry, mac, "up", "Volume Up", lock),
            Btr5VolumeButton(entry, mac, "down", "Volume Down", lock),
        ]
    )


class Btr5VolumeButton(ButtonEntity):
    def __init__(
        self,
        entry: ConfigEntry,
        mac: str,
        direction: str,
        name: str,
        lock: asyncio.Lock,
    ) -> None:
        self._entry = entry
        self._mac = mac
        self._direction = direction
        self._lock = lock
        self._attr_name = name
        self._attr_unique_id = f"{entry.entry_id}_{direction}"
        self._attr_icon = _ICONS[direction]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, mac)},
            name="FiiO BTR5",
            manufacturer="FiiO",
            model="BTR5 2021",
        )
        self._pending_steps = 0
        self._cancel_flush: CALLBACK_TYPE | None = None

    async def async_press(self) -> None:
        """Record the press and (re)schedule the debounced flush.

        Must return near-instantly and must not touch ``self._lock`` — the
        lock only guards the deferred hardware call in ``_async_flush``.
        """
        step_size = self._entry.options.get("step_size", DEFAULT_STEP_SIZE)
        debounce_seconds = self._entry.options.get("debounce_seconds", DEFAULT_DEBOUNCE_SECONDS)
        self._pending_steps += step_size
        if self._cancel_flush is not None:
            self._cancel_flush()
        self._cancel_flush = async_call_later(self.hass, debounce_seconds, self._async_flush)

    async def async_will_remove_from_hass(self) -> None:
        if self._cancel_flush is not None:
            self._cancel_flush()
            self._cancel_flush = None
        self._pending_steps = 0

    async def _async_flush(self, _now) -> None:
        """Send the accumulated step count over a single Bluetooth session.

        Runs in a scheduled callback with no caller waiting on it, so a
        failure can no longer raise back through the button.press service
        call — it is logged instead.
        """
        self._cancel_flush = None
        count = self._pending_steps
        self._pending_steps = 0
        if count == 0:
            return
        persistent = self.hass.data[DOMAIN][self._entry.entry_id].get("persistent")
        async with self._lock:
            try:
                await self.hass.async_add_executor_job(
                    gaia.send_volume_steps_persistent_aware,
                    persistent,
                    self._mac,
                    self._direction,
                    count,
                    DEFAULT_TIMEOUT,
                )
            except gaia.GaiaError as exc:
                _LOGGER.error(
                    "Deferred BTR5 %s volume command (%d steps) failed: %s",
                    self._direction,
                    count,
                    _error_message(exc),
                )
