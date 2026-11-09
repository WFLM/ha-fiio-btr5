from datetime import timedelta

from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import async_update_entity
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.btr5 import gaia
from custom_components.btr5.const import (
    BATTERY_POLL_INTERVAL_MINUTES,
    CONF_STAY_CONNECTED,
    DOMAIN,
)


async def _setup_entry(hass, options=None):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"mac": "00:00:5E:00:53:01"},
        options={CONF_STAY_CONNECTED: False, **(options or {})},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    return entry


def _entity_id(hass, entry, key="connection_status"):
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_{key}")
    assert entity_id is not None
    return entity_id


async def test_battery_value_is_polled_but_not_exposed_as_an_entity(hass, monkeypatch):
    reads = []
    monkeypatch.setattr(gaia, "read_battery", lambda mac, timeout: reads.append(mac) or 75)
    entry = await _setup_entry(hass)
    # With Stay Connected on, a read at setup would open and close a link
    # right before the held one.
    assert reads == []

    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(minutes=BATTERY_POLL_INTERVAL_MINUTES, seconds=1)
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    coordinator = hass.data[DOMAIN][entry.entry_id]["battery_coordinator"]
    assert coordinator.data == 75
    assert (
        er.async_get(hass).async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_battery"
        )
        is None
    )


async def test_internal_battery_poll_uses_the_shared_per_entry_lock(hass, monkeypatch):
    # So a poll and a press never open two Bluetooth sessions at once.
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
    persistent = gaia.PersistentConnection("00:00:5E:00:53:01")
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
        data={"mac": "00:00:5E:00:53:01"},
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


# A retry loop would keep button presses waiting while the BTR5 is off.
async def test_battery_poll_over_the_persistent_connection_tries_once(hass, monkeypatch):
    monkeypatch.setattr(gaia, "read_battery", lambda mac, timeout: 50)
    monkeypatch.setattr(gaia, "PERSISTENT_RECONNECT_DELAY_SECONDS", 0)
    attempts = []

    def failing_discover(mac, timeout):
        attempts.append(1)
        raise gaia.GaiaDiscoveryError("BTR5 is off")

    monkeypatch.setattr(gaia, "discover_gaia_channel", failing_discover)
    entry = await _setup_entry(hass)
    data = hass.data[DOMAIN][entry.entry_id]
    data["persistent"] = gaia.PersistentConnection("00:00:5E:00:53:01")

    await data["battery_coordinator"].async_refresh()

    assert len(attempts) == 1
