from datetime import timedelta

from homeassistant.const import EntityCategory
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed_exact,
)

from custom_components.btr5 import gaia
from custom_components.btr5.const import (
    DEFAULT_DEBOUNCE_SECONDS,
    DEFAULT_STEP_SIZE,
    DOMAIN,
)


async def _setup_entry(hass, options=None):
    entry = MockConfigEntry(domain=DOMAIN, data={"mac": "40:ED:98:1A:A2:C9"}, options=options or {})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _entity_id(hass, entry, domain, key):
    entity_id = er.async_get(hass).async_get_entity_id(domain, DOMAIN, f"{entry.entry_id}_{key}")
    assert entity_id is not None
    return entity_id


async def test_step_size_number_defaults_and_is_config_category(hass, monkeypatch):
    monkeypatch.setattr(gaia, "read_battery", lambda mac, timeout: 50)
    entry = await _setup_entry(hass)

    entity_id = _entity_id(hass, entry, "number", "step_size")
    state = hass.states.get(entity_id)
    assert state is not None
    assert float(state.state) == DEFAULT_STEP_SIZE

    registry_entry = er.async_get(hass).async_get(entity_id)
    assert registry_entry.entity_category == EntityCategory.CONFIG


async def test_debounce_seconds_number_defaults_and_is_config_category(hass, monkeypatch):
    monkeypatch.setattr(gaia, "read_battery", lambda mac, timeout: 50)
    entry = await _setup_entry(hass)

    entity_id = _entity_id(hass, entry, "number", "debounce_seconds")
    state = hass.states.get(entity_id)
    assert state is not None
    assert float(state.state) == DEFAULT_DEBOUNCE_SECONDS

    registry_entry = er.async_get(hass).async_get(entity_id)
    assert registry_entry.entity_category == EntityCategory.CONFIG


async def test_setting_step_size_persists_to_config_entry_options_as_int(hass, monkeypatch):
    monkeypatch.setattr(gaia, "read_battery", lambda mac, timeout: 50)
    entry = await _setup_entry(hass)
    entity_id = _entity_id(hass, entry, "number", "step_size")

    await hass.services.async_call(
        "number", "set_value", {"entity_id": entity_id, "value": 5}, blocking=True
    )

    assert entry.options["step_size"] == 5
    assert isinstance(entry.options["step_size"], int)
    assert float(hass.states.get(entity_id).state) == 5


async def test_setting_debounce_seconds_persists_to_config_entry_options_as_float(hass, monkeypatch):
    monkeypatch.setattr(gaia, "read_battery", lambda mac, timeout: 50)
    entry = await _setup_entry(hass)
    entity_id = _entity_id(hass, entry, "number", "debounce_seconds")

    await hass.services.async_call(
        "number", "set_value", {"entity_id": entity_id, "value": 1.5}, blocking=True
    )

    assert entry.options["debounce_seconds"] == 1.5
    assert float(hass.states.get(entity_id).state) == 1.5


async def test_button_press_uses_the_number_entities_current_values(hass, monkeypatch):
    calls = []
    monkeypatch.setattr(gaia, "read_battery", lambda mac, timeout: 50)
    monkeypatch.setattr(
        gaia,
        "send_volume_steps",
        lambda mac, direction, count, timeout: calls.append((direction, count)),
    )
    entry = await _setup_entry(hass)

    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": _entity_id(hass, entry, "number", "step_size"), "value": 4},
        blocking=True,
    )
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": _entity_id(hass, entry, "number", "debounce_seconds"), "value": 0.1},
        blocking=True,
    )

    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": _entity_id(hass, entry, "button", "up")},
        blocking=True,
    )
    async_fire_time_changed_exact(hass, dt_util.utcnow() + timedelta(seconds=0.2))
    await hass.async_block_till_done()

    assert calls == [("up", 4)]
