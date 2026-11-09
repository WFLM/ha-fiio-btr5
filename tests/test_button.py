import asyncio
import threading
import time
from datetime import timedelta

from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed_exact,
)

from custom_components.btr5 import button, gaia
from custom_components.btr5.const import (
    CONF_STAY_CONNECTED,
    DEFAULT_DEBOUNCE_SECONDS,
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


async def _until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not met in time"
        await asyncio.sleep(0.01)


async def _press_during_a_blocked_send(hass, monkeypatch, entity_id, outcome, presses):
    """Press once, then ``presses`` more times while that send is blocked.

    Each later press gets its own debounce window, as when pressing slowly,
    and the send then ends with ``outcome()``. Returns the step counts sent.
    """
    started, release = threading.Event(), threading.Event()
    calls = []

    def send(mac, steps, timeout):
        calls.append(len(steps))
        if len(calls) == 1:
            started.set()
            release.wait(5)
            outcome()

    monkeypatch.setattr(gaia, "send_volume_steps", send)
    await hass.services.async_call("button", "press", {"entity_id": entity_id}, blocking=True)
    async_fire_time_changed_exact(hass, dt_util.utcnow() + timedelta(seconds=DEFAULT_DEBOUNCE_SECONDS + 0.1))
    await _until(started.is_set)

    for _ in range(presses):
        await hass.services.async_call("button", "press", {"entity_id": entity_id}, blocking=True)
        async_fire_time_changed_exact(hass, dt_util.utcnow() + timedelta(seconds=DEFAULT_DEBOUNCE_SECONDS + 0.1))
        await asyncio.sleep(0.05)

    release.set()
    await hass.async_block_till_done()
    return calls


async def test_presses_made_during_a_send_go_out_together_after_it(hass, monkeypatch):
    entry = await _setup_entry(hass)

    calls = await _press_during_a_blocked_send(
        hass, monkeypatch, _entity_id(hass, entry, "up"), lambda: None, presses=3
    )

    assert calls == [1, 3]


async def test_presses_made_while_a_send_fails_are_dropped(hass, monkeypatch, caplog):
    def refuse():
        raise gaia.GaiaConnectError("refused")

    entry = await _setup_entry(hass)

    calls = await _press_during_a_blocked_send(
        hass, monkeypatch, _entity_id(hass, entry, "up"), refuse, presses=3
    )

    # Sent once the BTR5 is back, they'd change the volume long after the press.
    assert calls == [1]
    assert "dropped 3" in caplog.text.lower()


# The BTR5 answered, so the link is fine and later presses go out.
async def test_presses_made_while_a_send_is_rejected_are_still_sent(hass, monkeypatch):
    def reject():
        raise gaia.GaiaAckError("rejected")

    entry = await _setup_entry(hass)

    calls = await _press_during_a_blocked_send(
        hass, monkeypatch, _entity_id(hass, entry, "up"), reject, presses=3
    )

    assert calls == [1, 3]


# As while Stay Connected keeps trying to reach a BTR5 that's off.
async def test_presses_that_waited_long_for_the_btr5_are_dropped(hass, monkeypatch, caplog):
    monkeypatch.setattr(button, "MAX_PRESS_DELAY_SECONDS", 0.2)
    calls = []
    monkeypatch.setattr(
        gaia, "send_volume_steps", lambda mac, steps, timeout: calls.append(len(steps))
    )
    entry = await _setup_entry(hass)
    entity_id = _entity_id(hass, entry, "up")
    lock = hass.data[DOMAIN][entry.entry_id]["lock"]

    await lock.acquire()
    try:
        for waiting in (1, 2):
            if waiting == 2:
                await asyncio.sleep(0.3)
            await hass.services.async_call("button", "press", {"entity_id": entity_id}, blocking=True)
            async_fire_time_changed_exact(
                hass, dt_util.utcnow() + timedelta(seconds=DEFAULT_DEBOUNCE_SECONDS + 0.1)
            )
            await _until(lambda waiting=waiting: len(lock._waiters or ()) == waiting)
    finally:
        lock.release()
    await hass.async_block_till_done()

    assert calls == [1]
    assert "dropped 1" in caplog.text.lower()


async def test_volume_up_button_sends_one_step_by_default(hass, monkeypatch):
    calls = []
    monkeypatch.setattr(
        gaia,
        "send_volume_steps",
        lambda mac, steps, timeout: calls.append(steps),
    )
    entry = await _setup_entry(hass)

    await hass.services.async_call(
        "button", "press", {"entity_id": _entity_id(hass, entry, "up")}, blocking=True
    )
    assert calls == []  # still inside the debounce window

    await _advance_past_debounce(hass)
    assert calls == [["up"]]


async def test_volume_down_button_respects_configured_step_size(hass, monkeypatch):
    calls = []
    monkeypatch.setattr(
        gaia,
        "send_volume_steps",
        lambda mac, steps, timeout: calls.append(steps),
    )
    entry = await _setup_entry(hass, options={"step_size": 3})

    await hass.services.async_call(
        "button", "press", {"entity_id": _entity_id(hass, entry, "down")}, blocking=True
    )
    await _advance_past_debounce(hass)
    assert calls == [["down"] * 3]


async def test_flush_failure_is_logged(hass, monkeypatch, caplog):
    def failing_send(mac, steps, timeout):
        raise gaia.GaiaConnectError("refused")

    monkeypatch.setattr(gaia, "send_volume_steps", failing_send)
    entry = await _setup_entry(hass)

    # Doesn't raise: the send comes later.
    await hass.services.async_call(
        "button", "press", {"entity_id": _entity_id(hass, entry, "up")}, blocking=True
    )
    await _advance_past_debounce(hass)

    assert "failed" in caplog.text.lower()
    assert "bluetooth connection" in caplog.text.lower()


# The same error is raised when the BTR5 drops a link it had accepted.
async def test_a_connection_failure_is_logged_with_its_cause(hass, monkeypatch, caplog):
    def dropped(mac, steps, timeout):
        raise gaia.GaiaConnectError("BTR5 closed the Bluetooth connection")

    monkeypatch.setattr(gaia, "send_volume_steps", dropped)
    entry = await _setup_entry(hass)

    await hass.services.async_call(
        "button", "press", {"entity_id": _entity_id(hass, entry, "up")}, blocking=True
    )
    await _advance_past_debounce(hass)

    assert "BTR5 closed the Bluetooth connection" in caplog.text
    assert "refused" not in caplog.text


# At the bottom of the volume range, down then up would end a step higher.
async def test_presses_on_both_buttons_go_out_in_order_in_one_send(hass, monkeypatch):
    calls = []
    monkeypatch.setattr(gaia, "send_volume_steps", lambda mac, steps, timeout: calls.append(steps))
    entry = await _setup_entry(hass)

    for direction in ("up", "down", "up"):
        await hass.services.async_call(
            "button", "press", {"entity_id": _entity_id(hass, entry, direction)}, blocking=True
        )
    await _advance_past_debounce(hass)

    assert calls == [["up", "down", "up"]]


async def test_a_press_on_the_other_button_restarts_the_debounce_window(hass, monkeypatch):
    calls = []
    monkeypatch.setattr(gaia, "send_volume_steps", lambda mac, steps, timeout: calls.append(steps))
    entry = await _setup_entry(hass)

    await hass.services.async_call(
        "button", "press", {"entity_id": _entity_id(hass, entry, "up")}, blocking=True
    )
    async_fire_time_changed_exact(hass, dt_util.utcnow() + timedelta(seconds=DEFAULT_DEBOUNCE_SECONDS - 0.1))
    await hass.async_block_till_done()
    await hass.services.async_call(
        "button", "press", {"entity_id": _entity_id(hass, entry, "down")}, blocking=True
    )
    async_fire_time_changed_exact(hass, dt_util.utcnow() + timedelta(seconds=DEFAULT_DEBOUNCE_SECONDS - 0.1))
    await hass.async_block_till_done()
    assert calls == []

    await _advance_past_debounce(hass)
    assert calls == [["up", "down"]]


# Behind a send from before a reload, a press can wait for the BTR5 too.
async def test_presses_held_up_by_a_send_from_before_a_reload_expire(hass, monkeypatch, caplog):
    monkeypatch.setattr(button, "MAX_PRESS_DELAY_SECONDS", 0.3)
    first, release = threading.Event(), threading.Event()
    calls = []

    def send(mac, steps, timeout):
        calls.append(steps)
        if len(calls) == 1:
            first.set()
            release.wait(5)

    monkeypatch.setattr(gaia, "send_volume_steps", send)
    entry = await _setup_entry(hass)

    async def press_up():
        await hass.services.async_call(
            "button", "press", {"entity_id": _entity_id(hass, entry, "up")}, blocking=True
        )
        async_fire_time_changed_exact(
            hass, dt_util.utcnow() + timedelta(seconds=DEFAULT_DEBOUNCE_SECONDS + 0.1)
        )

    await press_up()
    await _until(first.is_set)
    try:
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await press_up()
        await _until(lambda: "dropped 1" in caplog.text.lower())
    finally:
        release.set()
    await hass.async_block_till_done()

    assert calls == [["up"]]


async def test_removing_one_button_keeps_the_presses_on_the_other(hass, monkeypatch):
    calls = []
    monkeypatch.setattr(gaia, "send_volume_steps", lambda mac, steps, timeout: calls.append(steps))
    entry = await _setup_entry(hass)

    await hass.services.async_call(
        "button", "press", {"entity_id": _entity_id(hass, entry, "up")}, blocking=True
    )
    er.async_get(hass).async_update_entity(
        _entity_id(hass, entry, "down"), disabled_by=er.RegistryEntryDisabler.USER
    )
    await hass.async_block_till_done()
    await _advance_past_debounce(hass)

    assert calls == [["up"]]


# Reloaded while a send was under way with Stay Connected off: the unload
# doesn't wait for it.
async def test_a_send_from_before_a_reload_keeps_the_btr5_to_itself(hass, monkeypatch):
    first, release = threading.Event(), threading.Event()
    others_active = []
    active = []

    def send(mac, steps, timeout):
        others_active.append(len(active))
        active.append(steps)
        try:
            if len(others_active) == 1:
                first.set()
                release.wait(5)
        finally:
            active.pop()

    monkeypatch.setattr(gaia, "send_volume_steps", send)
    entry = await _setup_entry(hass)

    async def press_up():
        await hass.services.async_call(
            "button", "press", {"entity_id": _entity_id(hass, entry, "up")}, blocking=True
        )
        async_fire_time_changed_exact(
            hass, dt_util.utcnow() + timedelta(seconds=DEFAULT_DEBOUNCE_SECONDS + 0.1)
        )

    await press_up()
    await _until(first.is_set)
    try:
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await press_up()
        await asyncio.sleep(0.1)
        assert others_active == [0]
    finally:
        release.set()
    await hass.async_block_till_done()

    assert others_active == [0, 0]


async def test_rapid_presses_are_coalesced_into_one_send(hass, monkeypatch):
    calls = []
    monkeypatch.setattr(
        gaia,
        "send_volume_steps",
        lambda mac, steps, timeout: calls.append(steps),
    )
    entry = await _setup_entry(hass, options={"step_size": 2})
    entity_id = _entity_id(hass, entry, "up")

    for _ in range(3):
        await hass.services.async_call("button", "press", {"entity_id": entity_id}, blocking=True)

    assert calls == []  # still batching

    await _advance_past_debounce(hass)
    assert calls == [["up"] * 6]  # 3 presses * step_size 2, one Bluetooth session


async def test_new_press_resets_the_debounce_window(hass, monkeypatch):
    calls = []
    monkeypatch.setattr(
        gaia,
        "send_volume_steps",
        lambda mac, steps, timeout: calls.append(steps),
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

    assert calls == [["up"] * 2]  # both presses combined into one send


async def test_configured_debounce_window_is_honored(hass, monkeypatch):
    calls = []
    monkeypatch.setattr(
        gaia,
        "send_volume_steps",
        lambda mac, steps, timeout: calls.append(steps),
    )
    entry = await _setup_entry(hass, options={"debounce_seconds": 0.1})

    await hass.services.async_call(
        "button", "press", {"entity_id": _entity_id(hass, entry, "up")}, blocking=True
    )
    assert calls == []

    # Well before the default window ends.
    await _advance_past_debounce(hass, seconds=0.1)
    assert calls == [["up"]]


async def test_a_flush_with_nothing_left_to_send_doesnt_wait_for_the_lock(hass, monkeypatch):
    sending, release = threading.Event(), threading.Event()
    calls = []

    def send(mac, steps, timeout):
        calls.append(len(steps))
        sending.set()
        release.wait(5)

    monkeypatch.setattr(gaia, "send_volume_steps", send)
    entry = await _setup_entry(hass)
    entity_id = _entity_id(hass, entry, "up")
    lock = hass.data[DOMAIN][entry.entry_id]["lock"]

    await lock.acquire()
    try:
        await hass.services.async_call("button", "press", {"entity_id": entity_id}, blocking=True)
        async_fire_time_changed_exact(
            hass, dt_util.utcnow() + timedelta(seconds=DEFAULT_DEBOUNCE_SECONDS + 0.1)
        )
        await _until(lambda: lock._waiters)
        # Its debounce window is still open when the first flush takes it.
        await hass.services.async_call("button", "press", {"entity_id": entity_id}, blocking=True)
    finally:
        lock.release()
    await _until(sending.is_set)
    async_fire_time_changed_exact(
        hass, dt_util.utcnow() + timedelta(seconds=DEFAULT_DEBOUNCE_SECONDS + 0.1)
    )
    await asyncio.sleep(0.05)
    try:
        assert not lock._waiters
    finally:
        release.set()
        await hass.async_block_till_done()
    assert calls == [2]


# Pressed again and again because nothing happens: only the latest go out.
async def test_presses_that_keep_coming_while_waiting_drop_the_old_ones(hass, monkeypatch, caplog):
    monkeypatch.setattr(button, "MAX_PRESS_DELAY_SECONDS", 0.25)
    calls = []
    monkeypatch.setattr(
        gaia, "send_volume_steps", lambda mac, steps, timeout: calls.append(len(steps))
    )
    entry = await _setup_entry(hass)
    entity_id = _entity_id(hass, entry, "up")
    lock = hass.data[DOMAIN][entry.entry_id]["lock"]

    await lock.acquire()
    try:
        await hass.services.async_call("button", "press", {"entity_id": entity_id}, blocking=True)
        async_fire_time_changed_exact(
            hass, dt_util.utcnow() + timedelta(seconds=DEFAULT_DEBOUNCE_SECONDS + 0.1)
        )
        await _until(lambda: lock._waiters)
        # Each within the debounce window of the one before.
        for _ in range(5):
            await asyncio.sleep(0.15)
            await hass.services.async_call("button", "press", {"entity_id": entity_id}, blocking=True)
    finally:
        lock.release()
    await hass.async_block_till_done()

    assert calls == [2]
    assert "dropped 4" in caplog.text.lower()
