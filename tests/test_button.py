import asyncio
import time
from datetime import timedelta

from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed_exact,
)

from custom_components.btr5 import gaia
from custom_components.btr5.const import DEFAULT_DEBOUNCE_SECONDS, DOMAIN


async def _setup_entry(hass, options=None):
    entry = MockConfigEntry(domain=DOMAIN, data={"mac": "40:ED:98:1A:A2:C9"}, options=options or {})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _entity_id(hass, entry, direction):
    """Resolve a button's entity_id from its unique_id.

    Home Assistant derives the object_id from the device and entity names, and
    that derivation has changed across releases, so look it up rather than
    hardcoding it.
    """
    entity_id = er.async_get(hass).async_get_entity_id(
        "button", DOMAIN, f"{entry.entry_id}_{direction}"
    )
    assert entity_id is not None
    return entity_id


async def _advance_past_debounce(hass, seconds=DEFAULT_DEBOUNCE_SECONDS):
    async_fire_time_changed_exact(hass, dt_util.utcnow() + timedelta(seconds=seconds + 0.1))
    await hass.async_block_till_done()


async def test_volume_up_button_sends_one_step_by_default(hass, monkeypatch):
    calls = []
    monkeypatch.setattr(
        gaia,
        "send_volume_steps",
        lambda mac, direction, count, timeout: calls.append((direction, count)),
    )
    entry = await _setup_entry(hass)

    await hass.services.async_call(
        "button", "press", {"entity_id": _entity_id(hass, entry, "up")}, blocking=True
    )
    assert calls == []  # still inside the debounce window

    await _advance_past_debounce(hass)
    assert calls == [("up", 1)]


async def test_volume_down_button_respects_configured_step_size(hass, monkeypatch):
    calls = []
    monkeypatch.setattr(
        gaia,
        "send_volume_steps",
        lambda mac, direction, count, timeout: calls.append((direction, count)),
    )
    entry = await _setup_entry(hass, options={"step_size": 3})

    await hass.services.async_call(
        "button", "press", {"entity_id": _entity_id(hass, entry, "down")}, blocking=True
    )
    await _advance_past_debounce(hass)
    assert calls == [("down", 3)]


async def test_flush_failure_is_logged(hass, monkeypatch, caplog):
    def failing_send(mac, direction, count, timeout):
        raise gaia.GaiaConnectError("refused")

    monkeypatch.setattr(gaia, "send_volume_steps", failing_send)
    entry = await _setup_entry(hass)

    # The service call itself no longer raises: the failure only happens
    # later, inside the deferred flush.
    await hass.services.async_call(
        "button", "press", {"entity_id": _entity_id(hass, entry, "up")}, blocking=True
    )
    await _advance_past_debounce(hass)

    assert "failed" in caplog.text.lower()
    assert "bluetooth connection" in caplog.text.lower()


async def test_concurrent_presses_are_serialized_by_the_entry_lock(hass, monkeypatch):
    state = {"active": 0, "max_concurrent": 0}

    def slow_send(mac, direction, count, timeout):
        # gaia.send_volume_steps runs via hass.async_add_executor_job, so the
        # patched replacement must be a plain blocking callable.
        state["active"] += 1
        state["max_concurrent"] = max(state["max_concurrent"], state["active"])
        time.sleep(0.05)
        state["active"] -= 1

    monkeypatch.setattr(gaia, "send_volume_steps", slow_send)
    entry = await _setup_entry(hass)

    await asyncio.gather(
        hass.services.async_call(
            "button", "press", {"entity_id": _entity_id(hass, entry, "up")}, blocking=True
        ),
        hass.services.async_call(
            "button", "press", {"entity_id": _entity_id(hass, entry, "down")}, blocking=True
        ),
    )
    await _advance_past_debounce(hass)

    assert state["max_concurrent"] == 1


async def test_rapid_presses_are_coalesced_into_one_send(hass, monkeypatch):
    calls = []
    monkeypatch.setattr(
        gaia,
        "send_volume_steps",
        lambda mac, direction, count, timeout: calls.append((direction, count)),
    )
    entry = await _setup_entry(hass, options={"step_size": 2})
    entity_id = _entity_id(hass, entry, "up")

    for _ in range(3):
        await hass.services.async_call("button", "press", {"entity_id": entity_id}, blocking=True)

    assert calls == []  # still batching

    await _advance_past_debounce(hass)
    assert calls == [("up", 6)]  # 3 presses * step_size 2, one Bluetooth session


async def test_new_press_resets_the_debounce_window(hass, monkeypatch):
    calls = []
    monkeypatch.setattr(
        gaia,
        "send_volume_steps",
        lambda mac, direction, count, timeout: calls.append((direction, count)),
    )
    entry = await _setup_entry(hass)
    entity_id = _entity_id(hass, entry, "up")

    await hass.services.async_call("button", "press", {"entity_id": entity_id}, blocking=True)

    # Advance to just short of the window, then press again: this must push
    # the flush out rather than let the first press's original timer fire.
    async_fire_time_changed_exact(hass, dt_util.utcnow() + timedelta(seconds=DEFAULT_DEBOUNCE_SECONDS - 0.1))
    await hass.async_block_till_done()
    assert calls == []

    await hass.services.async_call("button", "press", {"entity_id": entity_id}, blocking=True)
    await _advance_past_debounce(hass)

    assert calls == [("up", 2)]  # both presses combined into one send


async def test_configured_debounce_window_is_honored(hass, monkeypatch):
    calls = []
    monkeypatch.setattr(
        gaia,
        "send_volume_steps",
        lambda mac, direction, count, timeout: calls.append((direction, count)),
    )
    entry = await _setup_entry(hass, options={"debounce_seconds": 0.1})

    await hass.services.async_call(
        "button", "press", {"entity_id": _entity_id(hass, entry, "up")}, blocking=True
    )
    # Not sent yet after the (longer) default window's worth of slack would
    # have passed for a 0.1s window, this proves the option is actually read.
    assert calls == []

    await _advance_past_debounce(hass, seconds=0.1)
    assert calls == [("up", 1)]
