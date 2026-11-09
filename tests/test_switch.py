import asyncio
import threading
from datetime import timedelta

from homeassistant.const import EVENT_HOMEASSISTANT_STOP, EntityCategory
from homeassistant.core import CoreState
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import async_update_entity
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed_exact,
)

from custom_components.btr5 import gaia
from custom_components.btr5.const import (
    CONF_STAY_CONNECTED,
    DEFAULT_DEBOUNCE_SECONDS,
    DOMAIN,
)

_VOLUME_ACK = bytes.fromhex("ff010001000a821f00")
_BATTERY_ACK = bytes.fromhex("ff010002000a8414004b")


class FakeTransport:
    """Acknowledge every volume step and battery read for wiring tests."""

    def __init__(self):
        self.written = []
        self.closed = False

    def write(self, data, timeout):
        self.written.append(data)

    def read(self, timeout):
        return _BATTERY_ACK if self.written[-1][6:8] == b"\x04\x14" else _VOLUME_ACK

    def volume_steps(self):
        return [frame for frame in self.written if frame[6:8] == b"\x02\x1f"]

    def close(self):
        self.closed = True


async def _setup_entry(hass, options=None, *, stay_connected=False, wait_for_connection=True):
    entry_options = dict(options or {})
    if stay_connected is not None:
        entry_options[CONF_STAY_CONNECTED] = stay_connected
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"mac": "00:00:5E:00:53:01"},
        options=entry_options,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=wait_for_connection)
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


async def test_switch_is_on_by_default(hass, monkeypatch):
    monkeypatch.setattr(gaia, "read_battery", lambda mac, timeout: 50)
    transport = FakeTransport()
    monkeypatch.setattr(gaia, "discover_gaia_channel", lambda mac, timeout: 7)
    monkeypatch.setattr(gaia, "open_rfcomm_socket", lambda mac, channel, timeout: transport)
    entry = await _setup_entry(hass, stay_connected=None)

    entity_id = _switch_entity_id(hass, entry)
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "on"
    assert hass.data[DOMAIN][entry.entry_id]["persistent"].transport is transport
    assert entry.options[CONF_STAY_CONNECTED] is True

    registry_entry = er.async_get(hass).async_get(entity_id)
    assert registry_entry.entity_category == EntityCategory.CONFIG


async def test_switch_respects_persisted_off_preference(hass, monkeypatch):
    monkeypatch.setattr(gaia, "read_battery", lambda mac, timeout: 50)
    entry = await _setup_entry(hass)

    state = hass.states.get(_switch_entity_id(hass, entry))
    assert state is not None
    assert state.state == "off"
    assert hass.data[DOMAIN][entry.entry_id].get("persistent") is None


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
    await hass.async_block_till_done(wait_background_tasks=True)

    state = hass.states.get(_switch_entity_id(hass, entry))
    assert state.state == "on"
    assert len(opens) == 1
    persistent = hass.data[DOMAIN][entry.entry_id]["persistent"]
    assert persistent.transport is transport


async def test_turn_on_failure_keeps_preference_enabled(hass, monkeypatch):
    monkeypatch.setattr(gaia, "read_battery", lambda mac, timeout: 50)
    monkeypatch.setattr(gaia, "PERSISTENT_RECONNECT_DELAY_SECONDS", 0)

    def failing_discover(mac, timeout):
        raise gaia.GaiaDiscoveryError("no BTR5 found")

    monkeypatch.setattr(gaia, "discover_gaia_channel", failing_discover)
    entry = await _setup_entry(hass)

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": _switch_entity_id(hass, entry)}, blocking=True
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    state = hass.states.get(_switch_entity_id(hass, entry))
    assert state.state == "on"
    persistent = hass.data[DOMAIN][entry.entry_id]["persistent"]
    assert persistent.transport is None
    assert persistent.status == gaia.PersistentConnection.STATUS_DISCONNECTED
    assert entry.options[CONF_STAY_CONNECTED] is True


async def test_turn_off_closes_and_clears_the_persistent_connection(hass, monkeypatch):
    monkeypatch.setattr(gaia, "read_battery", lambda mac, timeout: 50)
    transport = FakeTransport()
    monkeypatch.setattr(gaia, "discover_gaia_channel", lambda mac, timeout: 7)
    monkeypatch.setattr(gaia, "open_rfcomm_socket", lambda mac, channel, timeout: transport)
    entry = await _setup_entry(hass)

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": _switch_entity_id(hass, entry)}, blocking=True
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": _switch_entity_id(hass, entry)}, blocking=True
    )

    state = hass.states.get(_switch_entity_id(hass, entry))
    assert state.state == "off"
    assert transport.closed is True
    assert hass.data[DOMAIN][entry.entry_id].get("persistent") is None
    assert entry.options[CONF_STAY_CONNECTED] is False


async def test_enabled_preference_survives_entry_reload(hass, monkeypatch):
    monkeypatch.setattr(gaia, "read_battery", lambda mac, timeout: 50)
    transports = []
    monkeypatch.setattr(gaia, "discover_gaia_channel", lambda mac, timeout: 7)

    def open_transport(mac, channel, timeout):
        transport = FakeTransport()
        transports.append(transport)
        return transport

    monkeypatch.setattr(gaia, "open_rfcomm_socket", open_transport)
    entry = await _setup_entry(hass)

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": _switch_entity_id(hass, entry)}, blocking=True
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.options[CONF_STAY_CONNECTED] is True

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert transports[0].closed is True
    assert entry.options[CONF_STAY_CONNECTED] is True

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(_switch_entity_id(hass, entry)).state == "on"
    assert len(transports) == 2


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
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(discover_calls) == 1  # only the switch's own open()

    await hass.services.async_call(
        "button", "press", {"entity_id": _button_entity_id(hass, entry, "up")}, blocking=True
    )
    async_fire_time_changed_exact(
        hass, dt_util.utcnow() + timedelta(seconds=DEFAULT_DEBOUNCE_SECONDS + 0.1)
    )
    await hass.async_block_till_done()

    assert len(discover_calls) == 1  # button press reused the cached transport
    assert len(transport.volume_steps()) == 1


async def _until(condition):
    for _ in range(100):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition not reached")


def _switch_service(hass, service, entity_id):
    return hass.async_create_task(
        hass.services.async_call("switch", service, {"entity_id": entity_id}, blocking=True)
    )


# Meanwhile a button press holds the lock, and a battery poll waits for it.
async def test_switching_off_and_on_quickly_keeps_the_same_link(hass, monkeypatch):
    monkeypatch.setattr(gaia, "read_battery", lambda mac, timeout: 50)
    opens = []
    transport = FakeTransport()
    monkeypatch.setattr(gaia, "discover_gaia_channel", lambda mac, timeout: 7)
    monkeypatch.setattr(
        gaia, "open_rfcomm_socket", lambda mac, channel, timeout: (opens.append(1), transport)[1]
    )
    entry = await _setup_entry(hass, stay_connected=True)
    entity_id = _switch_entity_id(hass, entry)
    data = hass.data[DOMAIN][entry.entry_id]
    persistent = data["persistent"]
    lock = data["lock"]

    await lock.acquire()
    try:
        poll = hass.async_create_task(data["battery_coordinator"].async_refresh())
        await _until(lambda: lock._waiters)
        turn_off = _switch_service(hass, "turn_off", entity_id)
        await _until(lambda: persistent.stopped)
        turn_on = _switch_service(hass, "turn_on", entity_id)
        await _until(lambda: not persistent.stopped)
    finally:
        lock.release()
    await asyncio.gather(poll, turn_off, turn_on)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert hass.states.get(entity_id).state == "on"
    assert entry.options[CONF_STAY_CONNECTED] is True
    assert data["persistent"] is persistent
    assert persistent.status == gaia.PersistentConnection.STATUS_CONNECTED
    assert len(opens) == 1
    assert not transport.closed


# A second link to the BTR5's channel would fail while the first is open.
async def test_a_poll_waiting_at_turn_off_closes_the_link_before_its_own(hass, monkeypatch):
    monkeypatch.setattr(gaia, "read_battery", lambda mac, timeout: 50)
    transport = FakeTransport()
    monkeypatch.setattr(gaia, "discover_gaia_channel", lambda mac, timeout: 7)
    monkeypatch.setattr(gaia, "open_rfcomm_socket", lambda mac, channel, timeout: transport)
    entry = await _setup_entry(hass, stay_connected=True)
    entity_id = _switch_entity_id(hass, entry)
    status_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_connection_status"
    )
    data = hass.data[DOMAIN][entry.entry_id]
    persistent = data["persistent"]
    lock = data["lock"]
    closed_before_reading = []

    def read_battery(mac, timeout):
        closed_before_reading.append(transport.closed)
        return 50

    monkeypatch.setattr(gaia, "read_battery", read_battery)

    await lock.acquire()
    try:
        poll = hass.async_create_task(data["battery_coordinator"].async_refresh())
        await _until(lambda: lock._waiters)
        turn_off = _switch_service(hass, "turn_off", entity_id)
        await _until(lambda: persistent.stopped)
        await async_update_entity(hass, status_id)
        assert hass.states.get(status_id).state == "one_time"
    finally:
        lock.release()
    await asyncio.gather(poll, turn_off)

    assert closed_before_reading == [True]
    assert "persistent" not in data


# After a restart the BTR5 may not answer for a few seconds.
async def test_the_first_connection_is_retried_until_the_btr5_answers(hass, monkeypatch):
    monkeypatch.setattr(gaia, "read_battery", lambda mac, timeout: 50)
    monkeypatch.setattr(gaia, "PERSISTENT_RECONNECT_DELAY_SECONDS", 0)
    transport = FakeTransport()
    failures = [gaia.GaiaDiscoveryError("host is down")] * 2

    def flaky_discover(mac, timeout):
        if failures:
            raise failures.pop()
        return 7

    monkeypatch.setattr(gaia, "discover_gaia_channel", flaky_discover)
    monkeypatch.setattr(gaia, "open_rfcomm_socket", lambda mac, channel, timeout: transport)

    entry = await _setup_entry(hass, stay_connected=True)

    persistent = hass.data[DOMAIN][entry.entry_id]["persistent"]
    assert persistent.transport is transport
    assert persistent.status == gaia.PersistentConnection.STATUS_CONNECTED


def _btr5_off(monkeypatch):
    """Make every connection attempt fail, with a long pause between them."""
    monkeypatch.setattr(gaia, "read_battery", lambda mac, timeout: 50)
    monkeypatch.setattr(gaia, "PERSISTENT_RECONNECT_DELAY_SECONDS", 60)
    monkeypatch.setattr(gaia, "PERSISTENT_RECONNECT_WINDOW_SECONDS", 600)
    attempts = []
    first_attempt = threading.Event()

    def failing_discover(mac, timeout):
        attempts.append(1)
        first_attempt.set()
        raise gaia.GaiaDiscoveryError("BTR5 is off")

    monkeypatch.setattr(gaia, "discover_gaia_channel", failing_discover)
    return attempts, first_attempt


async def test_stopping_home_assistant_ends_the_connection_retries(hass, monkeypatch, caplog):
    attempts, first_attempt = _btr5_off(monkeypatch)
    entry = await _setup_entry(hass, stay_connected=True, wait_for_connection=False)
    assert await hass.async_add_executor_job(first_attempt.wait, 5)

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await asyncio.wait_for(hass.async_block_till_done(wait_background_tasks=True), 10)

    assert len(attempts) == 1
    assert not hass.data[DOMAIN][entry.entry_id]["lock"].locked()
    assert "connecting failed" not in caplog.text


async def test_unloading_ends_the_connection_retries(hass, monkeypatch):
    attempts, first_attempt = _btr5_off(monkeypatch)
    entry = await _setup_entry(hass, stay_connected=True, wait_for_connection=False)
    assert await hass.async_add_executor_job(first_attempt.wait, 5)

    assert await asyncio.wait_for(hass.config_entries.async_unload(entry.entry_id), 10)
    await hass.async_block_till_done()

    assert len(attempts) == 1


async def test_setup_with_stay_connected_opens_only_the_held_link(hass, monkeypatch):
    one_off_reads = []
    monkeypatch.setattr(gaia, "read_battery", lambda mac, timeout: one_off_reads.append(mac) or 50)
    transport = FakeTransport()
    monkeypatch.setattr(gaia, "discover_gaia_channel", lambda mac, timeout: 7)
    monkeypatch.setattr(gaia, "open_rfcomm_socket", lambda mac, channel, timeout: transport)

    entry = await _setup_entry(hass, stay_connected=True)

    assert one_off_reads == []
    assert hass.data[DOMAIN][entry.entry_id]["persistent"].transport is transport


# Home Assistant calls turn_on even when the switch is on already.
async def test_turning_stay_connected_on_again_checks_a_live_link_without_reconnecting(
    hass, monkeypatch
):
    monkeypatch.setattr(gaia, "read_battery", lambda mac, timeout: 50)
    opens = []
    transport = FakeTransport()
    monkeypatch.setattr(gaia, "discover_gaia_channel", lambda mac, timeout: 7)
    monkeypatch.setattr(
        gaia, "open_rfcomm_socket", lambda mac, channel, timeout: (opens.append(1), transport)[1]
    )
    entry = await _setup_entry(hass, stay_connected=True)

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": _switch_entity_id(hass, entry)}, blocking=True
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert len(opens) == 1
    assert len([frame for frame in transport.written if frame[6:8] == b"\x04\x14"]) == 2


class DeadAfterSetupTransport(FakeTransport):
    dead = False

    def read(self, timeout):
        if self.dead:
            raise gaia.GaiaConnectError("BTR5 closed the Bluetooth connection")
        return super().read(timeout)


# As when the BTR5 was power-cycled with no press or poll since.
async def test_turning_stay_connected_on_again_reconnects_a_dead_link(hass, monkeypatch):
    monkeypatch.setattr(gaia, "read_battery", lambda mac, timeout: 50)
    monkeypatch.setattr(gaia, "PERSISTENT_RECONNECT_DELAY_SECONDS", 0)
    links = [DeadAfterSetupTransport(), FakeTransport()]
    monkeypatch.setattr(gaia, "discover_gaia_channel", lambda mac, timeout: 7)
    monkeypatch.setattr(gaia, "open_rfcomm_socket", lambda mac, channel, timeout: links.pop(0))
    entry = await _setup_entry(hass, stay_connected=True)
    persistent = hass.data[DOMAIN][entry.entry_id]["persistent"]
    dead = persistent.transport
    dead.dead = True

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": _switch_entity_id(hass, entry)}, blocking=True
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert dead.closed
    assert persistent.transport is not None and persistent.transport is not dead


# As from an automation once the BTR5 is plugged in.
async def test_turning_stay_connected_on_again_reconnects_a_link_that_is_down(hass, monkeypatch):
    monkeypatch.setattr(gaia, "read_battery", lambda mac, timeout: 50)
    monkeypatch.setattr(gaia, "PERSISTENT_RECONNECT_ATTEMPTS", 1)
    transport = FakeTransport()
    btr5_on = []

    def discover(mac, timeout):
        if not btr5_on:
            raise gaia.GaiaDiscoveryError("BTR5 is off")
        return 7

    monkeypatch.setattr(gaia, "discover_gaia_channel", discover)
    monkeypatch.setattr(gaia, "open_rfcomm_socket", lambda mac, channel, timeout: transport)
    entry = await _setup_entry(hass, stay_connected=True)
    persistent = hass.data[DOMAIN][entry.entry_id]["persistent"]
    assert persistent.transport is None

    btr5_on.append(True)
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": _switch_entity_id(hass, entry)}, blocking=True
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert persistent.transport is transport


async def test_turning_stay_connected_on_while_connecting_doesnt_queue_another(hass, monkeypatch):
    monkeypatch.setattr(gaia, "read_battery", lambda mac, timeout: 50)
    transport = FakeTransport()
    discovering, release = threading.Event(), threading.Event()

    def slow_discover(mac, timeout):
        discovering.set()
        release.wait(5)
        return 7

    monkeypatch.setattr(gaia, "discover_gaia_channel", slow_discover)
    monkeypatch.setattr(gaia, "open_rfcomm_socket", lambda mac, channel, timeout: transport)
    entry = await _setup_entry(hass, stay_connected=True, wait_for_connection=False)
    assert await hass.async_add_executor_job(discovering.wait, 5)

    for _ in range(2):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": _switch_entity_id(hass, entry)}, blocking=True
        )
    release.set()
    await hass.async_block_till_done(wait_background_tasks=True)

    assert len([frame for frame in transport.written if frame[6:8] == b"\x04\x14"]) == 1


# As when a script in restart mode is restarted meanwhile.
async def test_a_turn_off_cancelled_while_waiting_still_closes_the_link(hass, monkeypatch):
    monkeypatch.setattr(gaia, "read_battery", lambda mac, timeout: 50)
    transport = FakeTransport()
    monkeypatch.setattr(gaia, "discover_gaia_channel", lambda mac, timeout: 7)
    monkeypatch.setattr(gaia, "open_rfcomm_socket", lambda mac, channel, timeout: transport)
    entry = await _setup_entry(hass, stay_connected=True)
    data = hass.data[DOMAIN][entry.entry_id]
    persistent = data["persistent"]
    lock = data["lock"]

    await lock.acquire()
    try:
        turn_off = _switch_service(hass, "turn_off", _switch_entity_id(hass, entry))
        await _until(lambda: persistent.stopped)
        await _until(lambda: lock._waiters)
        turn_off.cancel()
        await asyncio.gather(turn_off, return_exceptions=True)
    finally:
        lock.release()
    await hass.async_block_till_done(wait_background_tasks=True)

    assert transport.closed
    assert "persistent" not in data


# Its stop event has come and gone, so nothing would end the retries.
async def test_no_connection_is_started_while_home_assistant_stops(hass, monkeypatch):
    monkeypatch.setattr(gaia, "read_battery", lambda mac, timeout: 50)
    discover_calls = []
    monkeypatch.setattr(
        gaia, "discover_gaia_channel", lambda mac, timeout: discover_calls.append(1) or 7
    )
    monkeypatch.setattr(gaia, "open_rfcomm_socket", lambda mac, channel, timeout: FakeTransport())
    entry = await _setup_entry(hass)

    hass.set_state(CoreState.stopping)
    try:
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": _switch_entity_id(hass, entry)}, blocking=True
        )
        await hass.async_block_till_done(wait_background_tasks=True)
    finally:
        hass.set_state(CoreState.running)

    assert discover_calls == []


async def test_a_failed_check_of_the_link_is_logged_as_such(hass, monkeypatch, caplog):
    monkeypatch.setattr(gaia, "read_battery", lambda mac, timeout: 50)
    monkeypatch.setattr(gaia, "PERSISTENT_RECONNECT_ATTEMPTS", 1)

    def btr5_off(mac, timeout):
        raise gaia.GaiaDiscoveryError("BTR5 is off")

    monkeypatch.setattr(gaia, "discover_gaia_channel", btr5_off)
    entry = await _setup_entry(hass, stay_connected=True)
    caplog.clear()

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": _switch_entity_id(hass, entry)}, blocking=True
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert "connecting failed" in caplog.text
    assert "initial" not in caplog.text


# The BTR5 is off, and a connect is under way when it's switched off and on
# twice: one more round of retries, not one per request.
async def test_switching_off_and_on_while_connecting_adds_at_most_one_round(
    hass, monkeypatch, caplog
):
    monkeypatch.setattr(gaia, "read_battery", lambda mac, timeout: 50)
    monkeypatch.setattr(gaia, "PERSISTENT_RECONNECT_ATTEMPTS", 3)
    monkeypatch.setattr(gaia, "PERSISTENT_RECONNECT_DELAY_SECONDS", 0)
    attempts = []
    discovering, release = threading.Event(), threading.Event()

    def btr5_off(mac, timeout):
        attempts.append(1)
        discovering.set()
        release.wait(5)
        raise gaia.GaiaDiscoveryError("BTR5 is off")

    monkeypatch.setattr(gaia, "discover_gaia_channel", btr5_off)
    entry = await _setup_entry(hass, stay_connected=True, wait_for_connection=False)
    entity_id = _switch_entity_id(hass, entry)
    persistent = hass.data[DOMAIN][entry.entry_id]["persistent"]
    assert await hass.async_add_executor_job(discovering.wait, 5)

    turn_offs = []
    for _ in range(2):
        turn_offs.append(_switch_service(hass, "turn_off", entity_id))
        await _until(lambda: persistent.stopped)
        await hass.services.async_call("switch", "turn_on", {"entity_id": entity_id}, blocking=True)
    release.set()
    await asyncio.gather(*turn_offs)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert hass.states.get(entity_id).state == "on"
    assert len(attempts) == 6
    assert caplog.text.count("connecting failed") == 1


async def test_turned_back_on_as_connecting_gives_up_it_connects_after_all(hass, monkeypatch):
    monkeypatch.setattr(gaia, "read_battery", lambda mac, timeout: 50)
    transport = FakeTransport()
    discovering, release = threading.Event(), threading.Event()

    def slow_discover(mac, timeout):
        discovering.set()
        release.wait(5)
        return 7

    monkeypatch.setattr(gaia, "discover_gaia_channel", slow_discover)
    monkeypatch.setattr(gaia, "open_rfcomm_socket", lambda mac, channel, timeout: transport)
    connect = gaia.connect_persistent
    gave_up, turned_on = threading.Event(), threading.Event()

    def connect_held_once_it_gives_up(persistent, timeout):
        try:
            connect(persistent, timeout)
        except gaia.GaiaCancelledError:
            gave_up.set()
            turned_on.wait(5)
            raise

    monkeypatch.setattr(gaia, "connect_persistent", connect_held_once_it_gives_up)
    entry = await _setup_entry(hass, stay_connected=True, wait_for_connection=False)
    entity_id = _switch_entity_id(hass, entry)
    persistent = hass.data[DOMAIN][entry.entry_id]["persistent"]
    assert await hass.async_add_executor_job(discovering.wait, 5)

    turn_off = _switch_service(hass, "turn_off", entity_id)
    await _until(lambda: persistent.stopped)
    release.set()
    assert await hass.async_add_executor_job(gave_up.wait, 5)
    await hass.services.async_call("switch", "turn_on", {"entity_id": entity_id}, blocking=True)
    turned_on.set()
    await turn_off
    await hass.async_block_till_done(wait_background_tasks=True)

    assert persistent.status == gaia.PersistentConnection.STATUS_CONNECTED
    assert not transport.closed


# An automation turning it on as the BTR5 comes back, during the last attempt.
async def test_turning_on_during_a_failing_connect_tries_again(hass, monkeypatch, caplog):
    monkeypatch.setattr(gaia, "read_battery", lambda mac, timeout: 50)
    monkeypatch.setattr(gaia, "PERSISTENT_RECONNECT_ATTEMPTS", 1)
    discovering, release = threading.Event(), threading.Event()
    attempts = []

    def btr5_back_after_the_first_attempt(mac, timeout):
        attempts.append(1)
        if len(attempts) > 1:
            return 7
        discovering.set()
        release.wait(5)
        raise gaia.GaiaDiscoveryError("BTR5 is off")

    monkeypatch.setattr(gaia, "discover_gaia_channel", btr5_back_after_the_first_attempt)
    monkeypatch.setattr(gaia, "open_rfcomm_socket", lambda mac, channel, timeout: FakeTransport())
    entry = await _setup_entry(hass, stay_connected=True, wait_for_connection=False)
    persistent = hass.data[DOMAIN][entry.entry_id]["persistent"]
    assert await hass.async_add_executor_job(discovering.wait, 5)

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": _switch_entity_id(hass, entry)}, blocking=True
    )
    release.set()
    await hass.async_block_till_done(wait_background_tasks=True)

    assert persistent.status == gaia.PersistentConnection.STATUS_CONNECTED
    assert "connecting failed" not in caplog.text
