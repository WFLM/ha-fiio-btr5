from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from itertools import groupby

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later

from . import gaia
from .const import (
    DEFAULT_DEBOUNCE_SECONDS,
    DEFAULT_STEP_SIZE,
    DEFAULT_TIMEOUT,
    DOMAIN,
    MAX_PRESS_DELAY_SECONDS,
)

_LOGGER = logging.getLogger(__name__)

_ERROR_MESSAGES = {
    gaia.GaiaDiscoveryError: "Could not find BTR5's GAIA Bluetooth service. Is it powered on and in range?",
    gaia.GaiaConnectError: (
        "Bluetooth connection to BTR5 failed. Make sure it's connected over USB as the "
        "computer's audio output; otherwise see Troubleshooting in the README."
    ),
    gaia.GaiaAckTimeoutError: "BTR5 did not acknowledge the volume command in time.",
    gaia.GaiaAckError: "BTR5 rejected the volume command.",
}

_ICONS = {
    "up": "mdi:volume-plus",
    "down": "mdi:volume-minus",
}


@dataclass
class _Press:
    direction: str
    steps: int
    pressed_at: float


def _error_message(exc: gaia.GaiaError) -> str:
    """A hint for the error, with its cause."""
    hint = _ERROR_MESSAGES.get(type(exc))
    return f"{hint} Cause: {exc}" if hint else str(exc)


def _describe(steps: list[str]) -> str:
    """'2 up, 1 down' for ['up', 'up', 'down']."""
    return ", ".join(f"{len(list(run))} {direction}" for direction, run in groupby(steps))


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    mac = entry.data["mac"]
    presses = _Presses(hass, entry, mac, hass.data[DOMAIN][entry.entry_id])
    # Not when one button is removed: the other may still have presses.
    entry.async_on_unload(presses.clear)
    async_add_entities(
        [
            Btr5VolumeButton(entry, mac, "up", "Volume Up", presses),
            Btr5VolumeButton(entry, mac, "down", "Volume Down", presses),
        ]
    )


class _Presses:
    """Presses on both buttons, sent in the order they were made.

    One debounce window for both: a burst of presses goes out in one
    Bluetooth session once neither button has been pressed for the Debounce
    Window. At the end of the volume range, up then down and down then up
    end at different volumes, so the order is kept.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, mac: str, data: dict) -> None:
        self._hass = hass
        self._entry = entry
        self._mac = mac
        # Kept after the entry unloads, when hass.data no longer has it.
        self._data = data
        self._pending: list[_Press] = []
        self._cancel_flush: CALLBACK_TYPE | None = None

    @callback
    def add(self, direction: str) -> None:
        """Record the press and (re)schedule the debounced flush."""
        step_size = self._entry.options.get("step_size", DEFAULT_STEP_SIZE)
        debounce_seconds = self._entry.options.get("debounce_seconds", DEFAULT_DEBOUNCE_SECONDS)
        self._pending.append(_Press(direction, step_size, time.monotonic()))
        if self._cancel_flush is not None:
            self._cancel_flush()
        self._cancel_flush = async_call_later(self._hass, debounce_seconds, self._async_flush)

    @callback
    def clear(self) -> None:
        if self._cancel_flush is not None:
            self._cancel_flush()
            self._cancel_flush = None
        self._pending.clear()

    async def _async_flush(self, _now) -> None:
        """Send the queued presses over a single Bluetooth session.

        No caller waits on it, so a failure is logged, not raised.

        Presses made while waiting for the lock go out in this send too.
        Dropped are presses that waited over MAX_PRESS_DELAY_SECONDS, as
        while Stay Connected retries its connection, and presses made while
        a send was failing: sent once the BTR5 is back, they'd change the
        volume long after the press.
        """
        self._cancel_flush = None
        # Taken by an earlier flush, or dropped.
        if not self._pending:
            return
        waiting_since = time.monotonic()
        lock: asyncio.Lock = self._data["lock"]
        async with lock:
            pending, self._pending = self._pending, []
            now = time.monotonic()
            steps: list[str] = []
            late = 0
            start_by: float | None = None
            for press in pending:
                # Waiting since this flush began to wait for the lock, or
                # since it was pressed, if later.
                expires = max(press.pressed_at, waiting_since) + MAX_PRESS_DELAY_SECONDS
                if now <= expires:
                    steps += [press.direction] * press.steps
                    start_by = expires if start_by is None else min(start_by, expires)
                else:
                    late += press.steps
            if late:
                self._log_dropped_as_late(late)
            if not steps:
                return
            # Stay Connected may have been switched while waiting for the lock.
            persistent = self._data.get("persistent")
            try:
                await self._hass.async_add_executor_job(
                    gaia.send_volume_steps_persistent_aware,
                    persistent,
                    self._mac,
                    steps,
                    DEFAULT_TIMEOUT,
                    start_by,
                )
            except gaia.GaiaBusyError:
                # Behind a send from before an integration reload.
                self._log_dropped_as_late(len(steps))
            except gaia.GaiaError as exc:
                _LOGGER.error(
                    "Deferred BTR5 volume command (%s) failed: %s",
                    _describe(steps),
                    _error_message(exc),
                )
                # The BTR5 answered at once, so the link is fine.
                if isinstance(exc, gaia.GaiaAckError):
                    return
                if dropped := sum(press.steps for press in self._pending):
                    self._pending = []
                    _LOGGER.warning(
                        "Dropped %d BTR5 volume steps pressed while that command was failing",
                        dropped,
                    )

    @staticmethod
    def _log_dropped_as_late(steps: int) -> None:
        _LOGGER.warning(
            "Dropped %d BTR5 volume steps that waited over %g seconds for the BTR5",
            steps,
            MAX_PRESS_DELAY_SECONDS,
        )


class Btr5VolumeButton(ButtonEntity):
    def __init__(
        self,
        entry: ConfigEntry,
        mac: str,
        direction: str,
        name: str,
        presses: _Presses,
    ) -> None:
        self._direction = direction
        self._presses = presses
        self._attr_name = name
        self._attr_unique_id = f"{entry.entry_id}_{direction}"
        self._attr_icon = _ICONS[direction]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, mac)},
            name="FiiO BTR5",
            manufacturer="FiiO",
            model="BTR5 2021",
        )

    async def async_press(self) -> None:
        """Queue the press for the debounced flush.

        Must return near-instantly and must not take the entry's lock, which
        only guards the deferred hardware call.
        """
        self._presses.add(self._direction)
