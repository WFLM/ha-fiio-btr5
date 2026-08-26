from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import async_update_entity
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.btr5 import gaia
from custom_components.btr5.const import CONF_STAY_CONNECTED, DOMAIN


async def _setup_entry(hass, options=None):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"mac": "40:ED:98:1A:A2:C9"},
        options={CONF_STAY_CONNECTED: False, **(options or {})},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _entity_id(hass, entry, key="connection_status"):
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_{key}")
    assert entity_id is not None
    return entity_id


async def test_battery_value_is_polled_but_not_exposed_as_an_entity(hass, monkeypatch):
    monkeypatch.setattr(gaia, "read_battery", lambda mac, timeout: 75)
    entry = await _setup_entry(hass)

    coordinator = hass.data[DOMAIN][entry.entry_id]["battery_coordinator"]
    assert coordinator.data == 75
    assert (
        er.async_get(hass).async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_battery"
        )
        is None
    )


async def test_internal_battery_poll_uses_the_shared_per_entry_lock(hass, monkeypatch):
    # The battery coordinator's update function must acquire the same lock
    # object buttons use, so a poll and a button press never open two
    # simultaneous Bluetooth sessions. Verified directly rather than by
    # racing timing-sensitive callbacks: the lock is briefly held (not yet
    # released) while gaia.read_battery is actually running.
    seen_locked = {"value": None}

    def probing_read_battery(mac, timeout):
        seen_locked["value"] = lock.locked()
        return 50

    entry = await _setup_entry(hass)
    lock = hass.data[DOMAIN][entry.entry_id]["lock"]
    monkeypatch.setattr(gaia, "read_battery", probing_read_battery)

    coordinator = hass.data[DOMAIN][entry.entry_id]["battery_coordinator"]
    await coordinator.async_request_refresh()

    assert seen_locked["value"] is True
    assert not lock.locked()


async def test_connection_status_is_one_time_when_stay_connected_is_off(hass, monkeypatch):
    monkeypatch.setattr(gaia, "read_battery", lambda mac, timeout: 50)
    entry = await _setup_entry(hass)

    state = hass.states.get(_entity_id(hass, entry))
    assert state is not None
    assert state.state == "one_time"
    assert state.attributes["device_class"] == "enum"


async def test_connection_status_reflects_the_persistent_connections_status(hass, monkeypatch):
    monkeypatch.setattr(gaia, "read_battery", lambda mac, timeout: 50)
    entry = await _setup_entry(hass)

    entity_id = _entity_id(hass, entry)
    persistent = gaia.PersistentConnection("40:ED:98:1A:A2:C9")
    hass.data[DOMAIN][entry.entry_id]["persistent"] = persistent

    persistent.status = gaia.PersistentConnection.STATUS_CONNECTING
    await async_update_entity(hass, entity_id)
    assert hass.states.get(entity_id).state == "connecting"

    persistent.status = gaia.PersistentConnection.STATUS_CONNECTED
    await async_update_entity(hass, entity_id)
    assert hass.states.get(entity_id).state == "connected"


async def test_legacy_battery_entity_is_removed_from_registry(hass, monkeypatch):
    monkeypatch.setattr(gaia, "read_battery", lambda mac, timeout: 50)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"mac": "40:ED:98:1A:A2:C9"},
        options={CONF_STAY_CONNECTED: False},
    )
    entry.add_to_hass(hass)
    entity_registry = er.async_get(hass)
    legacy_entry = entity_registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{entry.entry_id}_battery",
        suggested_object_id="btr5_battery",
        config_entry=entry,
    )

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entity_registry.async_get(legacy_entry.entity_id) is None
