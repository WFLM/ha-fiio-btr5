from __future__ import annotations

import asyncio

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, callback

from .const import DOMAIN

PLATFORMS = ["button", "number", "sensor", "switch"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})
    data = hass.data[DOMAIN][entry.entry_id] = {"lock": asyncio.Lock()}

    # Home Assistant doesn't unload entries when it stops, so a retry loop
    # is ended here.
    @callback
    def _stop_reconnecting(_event: Event) -> None:
        if (persistent := data.get("persistent")) is not None:
            persistent.stop()

    entry.async_on_unload(hass.bus.async_listen(EVENT_HOMEASSISTANT_STOP, _stop_reconnecting))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        entry_data = hass.data[DOMAIN].pop(entry.entry_id)
        if remove_listener := entry_data.get("battery_poll_unsubscribe"):
            remove_listener()
    return unload_ok
