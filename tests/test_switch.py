from datetime import timedelta

import pytest
from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed_exact,
)

from custom_components.btr5 import gaia
from custom_components.btr5.const import DEFAULT_DEBOUNCE_SECONDS, DOMAIN


class FakeTransport:
    """Always acks an AV_REMOTE_CONTROL frame; good enough for switch/button
    wiring tests, which don't care about battery-frame framing."""

    def __init__(self):
        self.written = []
        self.closed = False

    def write(self, data, timeout):
        self.written.append(data)

    def read(self, timeout):
        return bytes.fromhex("ff010001000a821f00")

    def close(self):
        self.closed = True


async def _setup_entry(hass, options=None):
    entry = MockConfigEntry(domain=DOMAIN, data={"mac": "40:ED:98:1A:A2:C9"}, options=options or {})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _switch_entity_id(hass, entry):
    entity_id = er.async_get(hass).async_get_entity_id(
        "switch", DOMAIN, f"{entry.entry_id}_stay_connected"
    )
    assert entity_id is not None
    return entity_id


def _button_entity_id(hass, entry, direction):
    entity_id = er.async_get(hass).async_get_entity_id("button", DOMAIN, f"{entry.entry_id}_{direction}")
    assert entity_id is not None
    return entity_id


async def test_switch_is_off_by_default(hass, monkeypatch):
    monkeypatch.setattr(gaia, "read_battery", lambda mac, timeout: 50)
    entry = await _setup_entry(hass)

    entity_id = _switch_entity_id(hass, entry)
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "off"
    assert hass.data[DOMAIN][entry.entry_id].get("persistent") is None

    registry_entry = er.async_get(hass).async_get(entity_id)
    assert registry_entry.entity_category == EntityCategory.CONFIG


async def test_turn_on_opens_and_caches_a_persistent_connection(hass, monkeypatch):
    monkeypatch.setattr(gaia, "read_battery", lambda mac, timeout: 50)
    opens = []
    transport = FakeTransport()
    monkeypatch.setattr(gaia, "discover_gaia_channel", lambda mac, timeout: 7)
    monkeypatch.setattr(
        gaia, "open_rfcomm_socket", lambda mac, channel, timeout: (opens.append(1), transport)[1]
    )
    entry = await _setup_entry(hass)

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": _switch_entity_id(hass, entry)}, blocking=True
    )

    state = hass.states.get(_switch_entity_id(hass, entry))
    assert state.state == "on"
    assert len(opens) == 1
    persistent = hass.data[DOMAIN][entry.entry_id]["persistent"]
    assert persistent.transport is transport


async def test_turn_on_failure_raises_and_does_not_cache_a_connection(hass, monkeypatch):
    monkeypatch.setattr(gaia, "read_battery", lambda mac, timeout: 50)

    def failing_discover(mac, timeout):
        raise gaia.GaiaDiscoveryError("no BTR5 found")

    monkeypatch.setattr(gaia, "discover_gaia_channel", failing_discover)
    entry = await _setup_entry(hass)

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": _switch_entity_id(hass, entry)}, blocking=True
        )

    state = hass.states.get(_switch_entity_id(hass, entry))
    assert state.state == "off"
    assert hass.data[DOMAIN][entry.entry_id].get("persistent") is None


async def test_turn_off_closes_and_clears_the_persistent_connection(hass, monkeypatch):
    monkeypatch.setattr(gaia, "read_battery", lambda mac, timeout: 50)
    transport = FakeTransport()
    monkeypatch.setattr(gaia, "discover_gaia_channel", lambda mac, timeout: 7)
    monkeypatch.setattr(gaia, "open_rfcomm_socket", lambda mac, channel, timeout: transport)
    entry = await _setup_entry(hass)

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": _switch_entity_id(hass, entry)}, blocking=True
    )
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": _switch_entity_id(hass, entry)}, blocking=True
    )

    state = hass.states.get(_switch_entity_id(hass, entry))
    assert state.state == "off"
    assert transport.closed is True
    assert hass.data[DOMAIN][entry.entry_id].get("persistent") is None


async def test_button_press_reuses_the_persistent_connection_without_rediscovering(hass, monkeypatch):
    monkeypatch.setattr(gaia, "read_battery", lambda mac, timeout: 50)
    discover_calls = []
    transport = FakeTransport()
    monkeypatch.setattr(
        gaia, "discover_gaia_channel", lambda mac, timeout: discover_calls.append(1) or 7
    )
    monkeypatch.setattr(gaia, "open_rfcomm_socket", lambda mac, channel, timeout: transport)
    entry = await _setup_entry(hass)

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": _switch_entity_id(hass, entry)}, blocking=True
    )
    assert len(discover_calls) == 1  # only the switch's own open()

    await hass.services.async_call(
        "button", "press", {"entity_id": _button_entity_id(hass, entry, "up")}, blocking=True
    )
    async_fire_time_changed_exact(
        hass, dt_util.utcnow() + timedelta(seconds=DEFAULT_DEBOUNCE_SECONDS + 0.1)
    )
    await hass.async_block_till_done()

    assert len(discover_calls) == 1  # button press reused the cached transport
    assert len(transport.written) == 1
