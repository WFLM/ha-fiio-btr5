from custom_components.btr5.gaia import GaiaFrame, GaiaStreamParser


def test_encode_volume_up_frame():
    frame = GaiaFrame(flags=0x00, vendor_id=0x000A, command_id=0x021F, payload=bytes([0x41]))
    assert frame.encode() == bytes.fromhex("ff010001000a021f41")


def test_parser_decodes_single_complete_frame():
    parser = GaiaStreamParser()
    frames = parser.feed(bytes.fromhex("ff010001000a821f00"))
    assert len(frames) == 1
    assert frames[0].command_id == 0x821F
    assert frames[0].payload == bytes([0x00])


def test_parser_handles_split_frame_across_two_feeds():
    data = bytes.fromhex("ff010001000a821f00")
    parser = GaiaStreamParser()
    assert parser.feed(data[:5]) == []
    frames = parser.feed(data[5:])
    assert len(frames) == 1
    assert frames[0].command_id == 0x821F


def test_parser_decodes_two_frames_fed_together():
    single = bytes.fromhex("ff010001000a821f00")
    parser = GaiaStreamParser()
    frames = parser.feed(single + single)
    assert len(frames) == 2


from custom_components.btr5 import gaia

_SAMPLE_SDP_OUTPUT = """Service Name: Headset
Service RecHandle: 0x10000
Service Class ID List:
  "Headset" (0x1108)
Protocol Descriptor List:
  "L2CAP" (0x0100)
  "RFCOMM" (0x0003)
    Channel: 6

Service Name: CSR GAIA™
Service RecHandle: 0x10001
Service Class ID List:
  "Serial Port" (0x1101)
Protocol Descriptor List:
  "L2CAP" (0x0100)
  "RFCOMM" (0x0003)
    Channel: 14
"""


def test_parse_sdp_gaia_channel_finds_csr_gaia_block():
    assert gaia.parse_sdp_gaia_channel(_SAMPLE_SDP_OUTPUT) == 14


def test_parse_sdp_gaia_channel_raises_when_missing():
    try:
        gaia.parse_sdp_gaia_channel("Service Name: Headset\nChannel: 6\n")
        assert False, "expected GaiaDiscoveryError"
    except gaia.GaiaDiscoveryError:
        pass


def test_discover_gaia_channel_uses_sdptool_and_parses_result(monkeypatch):
    captured_args = {}

    def fake_run(args, capture_output, text, timeout, check):
        captured_args["args"] = args
        return gaia.subprocess.CompletedProcess(args, 0, stdout=_SAMPLE_SDP_OUTPUT, stderr="")

    monkeypatch.setattr(gaia.subprocess, "run", fake_run)
    assert gaia.discover_gaia_channel("00:00:5E:00:53:01") == 14
    assert captured_args["args"] == ["sdptool", "search", "--bdaddr", "00:00:5E:00:53:01", "SP"]


def test_discover_gaia_channel_raises_on_nonzero_exit(monkeypatch):
    def fake_run(args, capture_output, text, timeout, check):
        return gaia.subprocess.CompletedProcess(args, 1, stdout="", stderr="no such device")

    monkeypatch.setattr(gaia.subprocess, "run", fake_run)
    try:
        gaia.discover_gaia_channel("00:00:5E:00:53:01")
        assert False, "expected GaiaDiscoveryError"
    except gaia.GaiaDiscoveryError:
        pass


from unittest.mock import MagicMock


class ScriptedTransport:
    def __init__(self, responses):
        self._responses = list(responses)
        self.written = []

    def write(self, data, timeout):
        self.written.append(data)

    def read(self, timeout):
        if not self._responses:
            return b""
        return self._responses.pop(0)

    def close(self):
        pass


def test_send_volume_step_over_transport_writes_up_payload_and_waits_for_ack():
    transport = ScriptedTransport([bytes.fromhex("ff010001000a821f00")])
    gaia.send_volume_step_over_transport(transport, "up", timeout=1.0)
    assert transport.written == [bytes.fromhex("ff010001000a021f41")]


def test_send_volume_step_over_transport_writes_down_payload():
    transport = ScriptedTransport([bytes.fromhex("ff010001000a821f00")])
    gaia.send_volume_step_over_transport(transport, "down", timeout=1.0)
    assert transport.written == [bytes.fromhex("ff010001000a021f42")]


def test_send_volume_step_over_transport_raises_on_timeout():
    transport = ScriptedTransport([])
    try:
        gaia.send_volume_step_over_transport(transport, "up", timeout=1.0)
        assert False, "expected GaiaAckTimeoutError"
    except gaia.GaiaAckTimeoutError:
        pass


def test_send_volume_step_over_transport_raises_on_bad_status():
    transport = ScriptedTransport([bytes.fromhex("ff010001000a821f01")])
    try:
        gaia.send_volume_step_over_transport(transport, "up", timeout=1.0)
        assert False, "expected GaiaAckError"
    except gaia.GaiaAckError:
        pass


def test_read_battery_over_transport_returns_the_battery_value():
    transport = ScriptedTransport([bytes.fromhex("ff010002000a8414004b")])
    battery = gaia.read_battery_over_transport(transport, timeout=1.0)
    assert battery == 0x4B
    assert transport.written == [bytes.fromhex("ff010000000a0414")]


def test_read_battery_over_transport_raises_on_bad_status():
    transport = ScriptedTransport([bytes.fromhex("ff010002000a84140105")])
    try:
        gaia.read_battery_over_transport(transport, timeout=1.0)
        assert False, "expected GaiaAckError"
    except gaia.GaiaAckError:
        pass


def test_read_battery_over_transport_accepts_100_as_the_upper_bound():
    transport = ScriptedTransport([bytes.fromhex("ff010002000a84140064")])
    assert gaia.read_battery_over_transport(transport, timeout=1.0) == 100


def test_read_battery_over_transport_raises_on_value_above_100():
    transport = ScriptedTransport([bytes.fromhex("ff010002000a841400a0")])
    try:
        gaia.read_battery_over_transport(transport, timeout=1.0)
        assert False, "expected GaiaAckError"
    except gaia.GaiaAckError:
        pass


def test_read_battery_over_transport_raises_when_ack_has_no_value_byte():
    transport = ScriptedTransport([bytes.fromhex("ff010001000a841400")])
    try:
        gaia.read_battery_over_transport(transport, timeout=1.0)
        assert False, "expected GaiaAckError"
    except gaia.GaiaAckError:
        pass


def test_open_rfcomm_socket_wraps_connect_failure(monkeypatch):
    mock_sock = MagicMock()
    mock_sock.connect.side_effect = OSError("Connection refused")
    monkeypatch.setattr(gaia.socket, "socket", lambda *a, **k: mock_sock)
    # Absent from Python builds without Bluetooth support.
    monkeypatch.setattr(gaia.socket, "AF_BLUETOOTH", 31, raising=False)
    monkeypatch.setattr(gaia.socket, "BTPROTO_RFCOMM", 3, raising=False)
    try:
        gaia.open_rfcomm_socket("00:00:5E:00:53:01", 14)
        assert False, "expected GaiaConnectError"
    except gaia.GaiaConnectError:
        pass
    mock_sock.close.assert_called_once()


class SplitAckTransport:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def write(self, data, timeout):
        pass

    def read(self, timeout):
        if not self._chunks:
            return b""
        return self._chunks.pop(0)

    def close(self):
        pass


def test_send_volume_step_over_transport_handles_ack_split_across_two_reads():
    full_ack = bytes.fromhex("ff010001000a821f00")
    transport = SplitAckTransport([full_ack[:5], full_ack[5:]])
    gaia.send_volume_step_over_transport(transport, "up", timeout=1.0)


def test_send_volume_step_over_transport_rejects_invalid_direction():
    transport = ScriptedTransport([])
    try:
        gaia.send_volume_step_over_transport(transport, "sideways", timeout=1.0)
        assert False, "expected ValueError"
    except ValueError:
        pass


class CountingScriptedTransport(ScriptedTransport):
    def __init__(self, responses):
        super().__init__(responses)
        self.closed = 0

    def close(self):
        self.closed += 1


def test_send_volume_steps_reuses_one_connection_for_every_step(monkeypatch):
    ack = bytes.fromhex("ff010001000a821f00")
    transport = CountingScriptedTransport([ack, ack, ack])
    calls = {"discover": 0, "open": 0}

    def fake_discover(mac, timeout):
        calls["discover"] += 1
        return 14

    def fake_open(mac, channel, connect_timeout):
        calls["open"] += 1
        return transport

    monkeypatch.setattr(gaia, "discover_gaia_channel", fake_discover)
    monkeypatch.setattr(gaia, "open_rfcomm_socket", fake_open)

    gaia.send_volume_steps("00:00:5E:00:53:01", ["up"] * 3, timeout=1.0)

    assert calls == {"discover": 1, "open": 1}
    assert transport.closed == 1
    assert transport.written == [bytes.fromhex("ff010001000a021f41")] * 3


def test_send_volume_steps_closes_transport_when_a_step_fails(monkeypatch):
    transport = CountingScriptedTransport([bytes.fromhex("ff010001000a821f01")])
    monkeypatch.setattr(gaia, "discover_gaia_channel", lambda mac, timeout: 14)
    monkeypatch.setattr(gaia, "open_rfcomm_socket", lambda mac, channel, connect_timeout: transport)

    try:
        gaia.send_volume_steps("00:00:5E:00:53:01", ["down"] * 3, timeout=1.0)
        assert False, "expected GaiaAckError"
    except gaia.GaiaAckError:
        pass
    assert transport.closed == 1
    assert len(transport.written) == 1


def test_rfcomm_socket_transport_raises_gaia_error_on_eof():
    mock_sock = MagicMock()
    mock_sock.recv.return_value = b""
    transport = gaia.RfcommSocketTransport(mock_sock)
    try:
        transport.read(1.0)
        assert False, "expected GaiaConnectError"
    except gaia.GaiaConnectError:
        pass


def test_rfcomm_socket_transport_returns_empty_on_socket_timeout():
    mock_sock = MagicMock()
    mock_sock.recv.side_effect = TimeoutError()
    transport = gaia.RfcommSocketTransport(mock_sock)
    assert transport.read(1.0) == b""


def test_rfcomm_socket_transport_wraps_read_oserror():
    mock_sock = MagicMock()
    mock_sock.recv.side_effect = ConnectionResetError("reset by peer")
    transport = gaia.RfcommSocketTransport(mock_sock)
    try:
        transport.read(1.0)
        assert False, "expected GaiaConnectError"
    except gaia.GaiaConnectError:
        pass


def test_rfcomm_socket_transport_wraps_write_oserror():
    mock_sock = MagicMock()
    mock_sock.sendall.side_effect = BrokenPipeError("broken pipe")
    transport = gaia.RfcommSocketTransport(mock_sock)
    try:
        transport.write(b"\xff\x01", 1.0)
        assert False, "expected GaiaConnectError"
    except gaia.GaiaConnectError:
        pass


def test_persistent_connection_starts_disconnected():
    persistent = gaia.PersistentConnection("00:00:5E:00:53:01")
    assert persistent.status == gaia.PersistentConnection.STATUS_DISCONNECTED


def test_persistent_connection_opens_once_and_reuses_transport(monkeypatch):
    opens = []
    fake_transport = ScriptedTransport([])
    monkeypatch.setattr(gaia, "discover_gaia_channel", lambda mac, timeout: 5)
    monkeypatch.setattr(
        gaia, "open_rfcomm_socket", lambda mac, channel, timeout: (opens.append(1), fake_transport)[1]
    )

    persistent = gaia.PersistentConnection("00:00:5E:00:53:01")
    persistent.open(timeout=1.0)
    persistent.open(timeout=1.0)  # second call must not reopen

    assert len(opens) == 1
    assert persistent.transport is fake_transport
    assert persistent.status == gaia.PersistentConnection.STATUS_CONNECTED


def test_persistent_connection_status_is_connecting_during_the_attempt(monkeypatch):
    seen_status = {}

    def discover_and_capture_status(mac, timeout):
        seen_status["value"] = persistent.status
        return 5

    monkeypatch.setattr(gaia, "discover_gaia_channel", discover_and_capture_status)
    monkeypatch.setattr(gaia, "open_rfcomm_socket", lambda mac, channel, timeout: ScriptedTransport([]))

    persistent = gaia.PersistentConnection("00:00:5E:00:53:01")
    persistent.open(timeout=1.0)

    assert seen_status["value"] == gaia.PersistentConnection.STATUS_CONNECTING


def test_persistent_connection_open_failure_leaves_status_disconnected(monkeypatch):
    def failing_discover(mac, timeout):
        raise gaia.GaiaDiscoveryError("no BTR5 found")

    monkeypatch.setattr(gaia, "discover_gaia_channel", failing_discover)
    persistent = gaia.PersistentConnection("00:00:5E:00:53:01")

    try:
        persistent.open(timeout=1.0)
        assert False, "expected GaiaDiscoveryError"
    except gaia.GaiaDiscoveryError:
        pass

    assert persistent.status == gaia.PersistentConnection.STATUS_DISCONNECTED


def test_persistent_connection_close_clears_transport():
    class FakeTransport:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    fake = FakeTransport()
    persistent = gaia.PersistentConnection("00:00:5E:00:53:01")
    persistent.transport = fake
    persistent.close()

    assert persistent.transport is None
    assert fake.closed is True
    assert persistent.status == gaia.PersistentConnection.STATUS_DISCONNECTED


def test_send_volume_steps_persistent_aware_reuses_open_transport():
    transport = ScriptedTransport([bytes.fromhex("ff010001000a821f00")] * 3)
    persistent = gaia.PersistentConnection("mac")
    persistent.transport = transport  # already open

    gaia.send_volume_steps_persistent_aware(persistent, "mac", ["up"] * 3, timeout=1.0)

    assert len(transport.written) == 3
    assert persistent.transport is transport  # left open for the next call


def test_send_volume_steps_persistent_aware_opens_when_not_yet_open(monkeypatch):
    transport = ScriptedTransport([bytes.fromhex("ff010001000a821f00")])
    monkeypatch.setattr(gaia, "discover_gaia_channel", lambda mac, timeout: 5)
    monkeypatch.setattr(gaia, "open_rfcomm_socket", lambda mac, channel, timeout: transport)

    persistent = gaia.PersistentConnection("mac")
    gaia.send_volume_steps_persistent_aware(persistent, "mac", ["up"] * 1, timeout=1.0)

    assert persistent.transport is transport


def test_send_volume_steps_persistent_aware_retries_until_reconnected(monkeypatch):
    class BrokenTransport:
        def write(self, data, timeout):
            raise gaia.GaiaConnectError("dead")

        def read(self, timeout):
            return b""

        def close(self):
            pass

    monkeypatch.setattr(gaia, "PERSISTENT_RECONNECT_DELAY_SECONDS", 0)

    good_ack = bytes.fromhex("ff010001000a821f00")
    reopened_transport = ScriptedTransport([good_ack, good_ack])
    discover_attempts = []

    def flaky_discover(mac, timeout):
        discover_attempts.append(1)
        if len(discover_attempts) < 3:
            raise gaia.GaiaDiscoveryError("not found yet")
        return 5

    monkeypatch.setattr(gaia, "discover_gaia_channel", flaky_discover)
    monkeypatch.setattr(gaia, "open_rfcomm_socket", lambda mac, channel, timeout: reopened_transport)

    persistent = gaia.PersistentConnection("mac")
    persistent.transport = BrokenTransport()

    gaia.send_volume_steps_persistent_aware(persistent, "mac", ["up"] * 2, timeout=1.0)

    assert len(discover_attempts) == 3  # two failures, then success
    assert len(reopened_transport.written) == 2
    assert persistent.transport is reopened_transport  # reconnected, still held open


def test_send_volume_steps_persistent_aware_raises_after_exhausting_all_reconnect_attempts(monkeypatch):
    monkeypatch.setattr(gaia, "PERSISTENT_RECONNECT_DELAY_SECONDS", 0)
    attempts = []

    def always_fail_discover(mac, timeout):
        attempts.append(1)
        raise gaia.GaiaDiscoveryError("gone")

    monkeypatch.setattr(gaia, "discover_gaia_channel", always_fail_discover)
    persistent = gaia.PersistentConnection("mac")  # never successfully opened

    try:
        gaia.send_volume_steps_persistent_aware(persistent, "mac", ["up"] * 1, timeout=1.0)
        assert False, "expected GaiaDiscoveryError"
    except gaia.GaiaDiscoveryError:
        pass

    assert len(attempts) == gaia.PERSISTENT_RECONNECT_ATTEMPTS


def test_send_volume_steps_persistent_aware_with_none_delegates_to_ephemeral(monkeypatch):
    calls = []
    monkeypatch.setattr(
        gaia,
        "send_volume_steps",
        lambda mac, steps, timeout: calls.append((mac, steps)),
    )

    gaia.send_volume_steps_persistent_aware(None, "mac", ["up"] * 4, timeout=1.0)

    assert calls == [("mac", ["up"] * 4)]


def test_read_battery_persistent_aware_reuses_open_transport():
    transport = ScriptedTransport([bytes.fromhex("ff010002000a8414004b")])
    persistent = gaia.PersistentConnection("mac")
    persistent.transport = transport

    battery = gaia.read_battery_persistent_aware(persistent, "mac", timeout=1.0)

    assert battery == 0x4B
    assert persistent.transport is transport


def test_read_battery_persistent_aware_reconnects_when_not_yet_open(monkeypatch):
    transport = ScriptedTransport([bytes.fromhex("ff010002000a8414004b")])
    monkeypatch.setattr(gaia, "discover_gaia_channel", lambda mac, timeout: 5)
    monkeypatch.setattr(gaia, "open_rfcomm_socket", lambda mac, channel, timeout: transport)
    persistent = gaia.PersistentConnection("mac")

    assert gaia.read_battery_persistent_aware(persistent, "mac", timeout=1.0) == 0x4B
    assert persistent.transport is transport


# The next poll or button press reconnects.
def test_a_failed_battery_read_drops_the_link_without_retrying(monkeypatch):
    class BrokenTransport:
        def write(self, data, timeout):
            raise gaia.GaiaConnectError("dead")

        def close(self):
            pass

    discover_calls = []
    monkeypatch.setattr(gaia, "discover_gaia_channel", lambda mac, timeout: discover_calls.append(1) or 5)
    persistent = gaia.PersistentConnection("mac")
    persistent.transport = BrokenTransport()

    with pytest.raises(gaia.GaiaConnectError):
        gaia.read_battery_persistent_aware(persistent, "mac", timeout=1.0)

    assert discover_calls == []
    assert persistent.transport is None


def test_read_battery_persistent_aware_with_none_delegates_to_ephemeral(monkeypatch):
    monkeypatch.setattr(gaia, "read_battery", lambda mac, timeout: 33)
    assert gaia.read_battery_persistent_aware(None, "mac", timeout=1.0) == 33


import threading
import time
from types import SimpleNamespace

import pytest

_VOLUME_ACK = bytes.fromhex("ff010001000a821f00")


def _reconnect_to(monkeypatch, transport):
    monkeypatch.setattr(gaia, "PERSISTENT_RECONNECT_DELAY_SECONDS", 0)
    monkeypatch.setattr(gaia, "discover_gaia_channel", lambda mac, timeout: 5)
    monkeypatch.setattr(gaia, "open_rfcomm_socket", lambda mac, channel, timeout: transport)


class LinkLostAfterWriteTransport(ScriptedTransport):
    """Acknowledges ``acked`` steps, then loses the link after the next write."""

    def __init__(self, acked):
        super().__init__([_VOLUME_ACK] * acked)
        self._acked = acked

    def read(self, timeout):
        if len(self.written) > self._acked:
            raise gaia.GaiaConnectError("link lost")
        return super().read(timeout)


# The step written before the link was lost may have changed the volume.
def test_a_retry_sends_only_the_steps_not_yet_written(monkeypatch):
    lost = LinkLostAfterWriteTransport(acked=2)
    reopened = ScriptedTransport([_VOLUME_ACK] * 5)
    _reconnect_to(monkeypatch, reopened)
    persistent = gaia.PersistentConnection("mac")
    persistent.transport = lost

    gaia.send_volume_steps_persistent_aware(persistent, "mac", ["up"] * 5, timeout=1.0)

    assert len(lost.written) == 3
    assert len(reopened.written) == 2


# The BTR5 answered, so the link is fine and reconnecting won't help.
def test_a_rejected_step_is_not_retried(monkeypatch):
    rejecting = ScriptedTransport([bytes.fromhex("ff010001000a821f01")])
    discover_calls = []
    monkeypatch.setattr(gaia, "discover_gaia_channel", lambda mac, timeout: discover_calls.append(1) or 5)
    persistent = gaia.PersistentConnection("mac")
    persistent.transport = rejecting

    with pytest.raises(gaia.GaiaAckError):
        gaia.send_volume_steps_persistent_aware(persistent, "mac", ["up"] * 3, timeout=1.0)

    assert len(rejecting.written) == 1
    assert discover_calls == []
    assert persistent.transport is rejecting


def test_stop_ends_the_retry_loop_without_waiting(monkeypatch):
    monkeypatch.setattr(gaia, "PERSISTENT_RECONNECT_DELAY_SECONDS", 60)
    monkeypatch.setattr(gaia, "PERSISTENT_RECONNECT_WINDOW_SECONDS", 600)
    persistent = gaia.PersistentConnection("mac")

    def discover_then_stop(mac, timeout):
        persistent.stop()
        raise gaia.GaiaDiscoveryError("gone")

    monkeypatch.setattr(gaia, "discover_gaia_channel", discover_then_stop)
    started = time.monotonic()

    with pytest.raises(gaia.GaiaDiscoveryError):
        gaia.send_volume_steps_persistent_aware(persistent, "mac", ["up"] * 1, timeout=1.0)

    assert time.monotonic() - started < 5


# Stay Connected was turned off and straight back on during the pause.
def test_a_pause_ended_by_stop_and_resume_carries_on_retrying(monkeypatch):
    monkeypatch.setattr(gaia, "PERSISTENT_RECONNECT_DELAY_SECONDS", 3)
    monkeypatch.setattr(gaia, "PERSISTENT_RECONNECT_WINDOW_SECONDS", 600)
    transport = ScriptedTransport([_VOLUME_ACK])
    failed = threading.Event()

    def discover_failing_once(mac, timeout):
        if failed.is_set():
            return 5
        failed.set()
        raise gaia.GaiaDiscoveryError("gone")

    monkeypatch.setattr(gaia, "discover_gaia_channel", discover_failing_once)
    monkeypatch.setattr(gaia, "open_rfcomm_socket", lambda mac, channel, timeout: transport)
    persistent = gaia.PersistentConnection("mac")
    errors = []

    def send():
        try:
            gaia.send_volume_steps_persistent_aware(persistent, "mac", ["up"] * 1, timeout=1.0)
        except gaia.GaiaError as exc:
            errors.append(exc)

    sender = threading.Thread(target=send)
    sender.start()
    assert failed.wait(5)
    while not persistent._stopped._cond._waiters:  # until it's in the pause
        time.sleep(0.001)
    persistent.stop()
    persistent.resume()
    sender.join(10)

    assert errors == []
    assert len(transport.written) == 1


# Stay Connected was turned off while the press was being sent.
def test_an_action_under_way_when_stopped_still_finishes(monkeypatch):
    transport = ScriptedTransport([_VOLUME_ACK])
    persistent = gaia.PersistentConnection("mac")

    def discover_while_stopping(mac, timeout):
        persistent.stop()
        return 5

    monkeypatch.setattr(gaia, "discover_gaia_channel", discover_while_stopping)
    monkeypatch.setattr(gaia, "open_rfcomm_socket", lambda mac, channel, timeout: transport)

    gaia.send_volume_steps_persistent_aware(persistent, "mac", ["up"] * 1, timeout=1.0)

    assert len(transport.written) == 1


# A second link to the BTR5's channel would fail while the first is open.
def test_once_stopped_the_link_is_closed_before_a_one_off_connection(monkeypatch):
    transport = MagicMock()
    closed_before_sending = []
    monkeypatch.setattr(
        gaia,
        "send_volume_steps",
        lambda mac, steps, timeout: closed_before_sending.append(transport.close.called),
    )
    persistent = gaia.PersistentConnection("mac")
    persistent.transport = transport
    persistent.stop()

    gaia.send_volume_steps_persistent_aware(persistent, "mac", ["up"] * 1, timeout=1.0)

    assert closed_before_sending == [True]
    assert persistent.transport is None


def test_the_switch_doesnt_connect_once_stopped_after_discovery(monkeypatch):
    persistent = gaia.PersistentConnection("mac")
    connects = []

    def discover_while_stopping(mac, timeout):
        persistent.stop()
        return 5

    monkeypatch.setattr(gaia, "discover_gaia_channel", discover_while_stopping)
    monkeypatch.setattr(gaia, "open_rfcomm_socket", lambda mac, channel, timeout: connects.append(1))

    with pytest.raises(gaia.GaiaCancelledError):
        persistent.open(timeout=1.0, unless_stopped=True)

    assert connects == []
    assert persistent.status == gaia.PersistentConnection.STATUS_DISCONNECTED


# Every step was sent, so there's nothing to reconnect for.
def test_no_reconnect_when_only_the_last_ack_is_lost(monkeypatch, caplog):
    discover_calls = []
    monkeypatch.setattr(gaia, "discover_gaia_channel", lambda mac, timeout: discover_calls.append(1) or 5)
    lost = LinkLostAfterWriteTransport(acked=2)
    persistent = gaia.PersistentConnection("mac")
    persistent.transport = lost

    gaia.send_volume_steps_persistent_aware(persistent, "mac", ["up"] * 3, timeout=1.0)

    assert len(lost.written) == 3
    assert discover_calls == []
    assert persistent.transport is None
    assert "the volume may be a step short" in caplog.text


def test_no_attempt_starts_after_the_reconnect_window(monkeypatch):
    monkeypatch.setattr(gaia, "PERSISTENT_RECONNECT_DELAY_SECONDS", 3)
    monkeypatch.setattr(gaia, "PERSISTENT_RECONNECT_WINDOW_SECONDS", 7)
    clock = [0.0]
    monkeypatch.setattr(gaia, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    def pause(self, seconds):
        clock[0] += seconds
        return False

    monkeypatch.setattr(gaia.PersistentConnection, "wait_for_stop", pause)
    started = []

    def always_fail_discover(mac, timeout):
        started.append(clock[0])
        raise gaia.GaiaDiscoveryError("gone")

    monkeypatch.setattr(gaia, "discover_gaia_channel", always_fail_discover)

    with pytest.raises(gaia.GaiaDiscoveryError):
        gaia.send_volume_steps_persistent_aware(
            gaia.PersistentConnection("mac"), "mac", ["up"] * 1, timeout=1.0
        )

    # A fourth would start at 9, after the window.
    assert started == [0, 3, 6]


# A long burst of steps doesn't use up the time for reconnecting.
def test_the_reconnect_window_starts_at_the_first_failure(monkeypatch):
    monkeypatch.setattr(gaia, "PERSISTENT_RECONNECT_WINDOW_SECONDS", 0.1)

    class SlowLinkLostTransport(LinkLostAfterWriteTransport):
        def read(self, timeout):
            time.sleep(0.05)
            return super().read(timeout)

    lost = SlowLinkLostTransport(acked=3)
    reopened = ScriptedTransport([_VOLUME_ACK] * 2)
    _reconnect_to(monkeypatch, reopened)
    persistent = gaia.PersistentConnection("mac")
    persistent.transport = lost

    gaia.send_volume_steps_persistent_aware(persistent, "mac", ["up"] * 6, timeout=1.0)

    assert len(reopened.written) == 2


def test_retries_end_with_the_reconnect_window(monkeypatch):
    monkeypatch.setattr(gaia, "PERSISTENT_RECONNECT_DELAY_SECONDS", 0)
    monkeypatch.setattr(gaia, "PERSISTENT_RECONNECT_WINDOW_SECONDS", 0)
    attempts = []

    def always_fail_discover(mac, timeout):
        attempts.append(1)
        raise gaia.GaiaDiscoveryError("gone")

    monkeypatch.setattr(gaia, "discover_gaia_channel", always_fail_discover)

    with pytest.raises(gaia.GaiaDiscoveryError):
        gaia.send_volume_steps_persistent_aware(
            gaia.PersistentConnection("mac"), "mac", ["up"] * 1, timeout=1.0
        )

    assert len(attempts) == 1


_BATTERY_ACK = bytes.fromhex("ff010002000a8414004b")


def test_connect_persistent_keeps_the_link_once_the_btr5_answered_on_it(monkeypatch):
    # Closed before the BTR5 has set a link up, as turning Stay Connected
    # off while connecting would, the BTR5 stops taking GAIA connections
    # until it's reset.
    transport = ScriptedTransport([_BATTERY_ACK])
    _reconnect_to(monkeypatch, transport)
    persistent = gaia.PersistentConnection("mac")

    gaia.connect_persistent(persistent, timeout=1.0)

    assert transport.written == [bytes.fromhex("ff010000000a0414")]
    assert transport._responses == []
    assert persistent.transport is transport


def test_connect_persistent_reconnects_if_the_btr5_doesnt_answer_on_the_link(monkeypatch):
    silent, answering = ScriptedTransport([]), ScriptedTransport([_BATTERY_ACK])
    links = [silent, answering]
    monkeypatch.setattr(gaia, "PERSISTENT_RECONNECT_DELAY_SECONDS", 0)
    monkeypatch.setattr(gaia, "discover_gaia_channel", lambda mac, timeout: 5)
    monkeypatch.setattr(gaia, "open_rfcomm_socket", lambda mac, channel, timeout: links.pop(0))
    persistent = gaia.PersistentConnection("mac")

    gaia.connect_persistent(persistent, timeout=0.1)

    assert persistent.transport is answering


def test_connect_persistent_retries_until_the_btr5_answers(monkeypatch):
    transport = ScriptedTransport([_BATTERY_ACK])
    _reconnect_to(monkeypatch, transport)
    answers = [gaia.GaiaDiscoveryError("host is down")] * 2

    def flaky_discover(mac, timeout):
        if answers:
            raise answers.pop()
        return 5

    monkeypatch.setattr(gaia, "discover_gaia_channel", flaky_discover)
    persistent = gaia.PersistentConnection("mac")

    gaia.connect_persistent(persistent, timeout=1.0)

    assert persistent.transport is transport
    assert persistent.status == gaia.PersistentConnection.STATUS_CONNECTED


def test_connect_persistent_stops_quietly_once_turned_off(monkeypatch):
    monkeypatch.setattr(gaia, "PERSISTENT_RECONNECT_DELAY_SECONDS", 60)
    monkeypatch.setattr(gaia, "PERSISTENT_RECONNECT_WINDOW_SECONDS", 600)
    persistent = gaia.PersistentConnection("mac")

    def discover_then_stop(mac, timeout):
        persistent.stop()
        raise gaia.GaiaDiscoveryError("gone")

    monkeypatch.setattr(gaia, "discover_gaia_channel", discover_then_stop)
    started = time.monotonic()

    with pytest.raises(gaia.GaiaCancelledError):
        gaia.connect_persistent(persistent, timeout=1.0)

    assert time.monotonic() - started < 5


def test_rfcomm_socket_transport_wraps_settimeout_oserror():
    mock_sock = MagicMock()
    mock_sock.settimeout.side_effect = OSError(9, "Bad file descriptor")
    transport = gaia.RfcommSocketTransport(mock_sock)

    with pytest.raises(gaia.GaiaConnectError):
        transport.read(1.0)
    with pytest.raises(gaia.GaiaConnectError):
        transport.write(b"\xff\x01", 1.0)


def test_open_rfcomm_socket_without_bluetooth_support_raises_gaia_error(monkeypatch):
    monkeypatch.delattr(gaia.socket, "AF_BLUETOOTH", raising=False)

    with pytest.raises(gaia.GaiaConnectError):
        gaia.open_rfcomm_socket("00:00:5E:00:53:01", 14)


# Not a failure to warn about: Stay Connected is off.
def test_connect_persistent_turned_off_during_its_last_attempt_is_cancelled(monkeypatch):
    monkeypatch.setattr(gaia, "PERSISTENT_RECONNECT_ATTEMPTS", 1)
    persistent = gaia.PersistentConnection("mac")

    def discover_then_stop(mac, timeout):
        persistent.stop()
        raise gaia.GaiaDiscoveryError("gone")

    monkeypatch.setattr(gaia, "discover_gaia_channel", discover_then_stop)

    with pytest.raises(gaia.GaiaCancelledError):
        gaia.connect_persistent(persistent, timeout=1.0)


# It answered, so the link is up.
def test_connect_persistent_keeps_the_link_if_the_btr5_rejects_the_battery_read(monkeypatch):
    transport = ScriptedTransport([bytes.fromhex("ff010001000a841401")])
    _reconnect_to(monkeypatch, transport)
    persistent = gaia.PersistentConnection("mac")

    gaia.connect_persistent(persistent, timeout=1.0)

    assert persistent.transport is transport
    assert persistent.status == gaia.PersistentConnection.STATUS_CONNECTED


# Turned off, on and off again: the queued connect finds the link still open.
def test_connect_persistent_once_stopped_doesnt_use_an_open_link():
    transport = ScriptedTransport([_BATTERY_ACK])
    persistent = gaia.PersistentConnection("mac")
    persistent.transport = transport
    persistent.stop()

    with pytest.raises(gaia.GaiaCancelledError):
        gaia.connect_persistent(persistent, timeout=1.0)

    assert transport.written == []


# The Connection Status sensor shows it reconnecting, not disconnected.
def test_the_status_is_connecting_between_attempts(monkeypatch):
    monkeypatch.setattr(gaia, "PERSISTENT_RECONNECT_ATTEMPTS", 3)
    monkeypatch.setattr(gaia, "PERSISTENT_RECONNECT_WINDOW_SECONDS", 600)
    paused_while = []

    def pause(self, seconds):
        paused_while.append(self.status)
        return False

    def always_fail_discover(mac, timeout):
        raise gaia.GaiaDiscoveryError("gone")

    monkeypatch.setattr(gaia.PersistentConnection, "wait_for_stop", pause)
    monkeypatch.setattr(gaia, "discover_gaia_channel", always_fail_discover)
    persistent = gaia.PersistentConnection("mac")

    with pytest.raises(gaia.GaiaDiscoveryError):
        gaia.send_volume_steps_persistent_aware(persistent, "mac", ["up"] * 1, timeout=1.0)

    assert paused_while == [gaia.PersistentConnection.STATUS_CONNECTING] * 2
    assert persistent.status == gaia.PersistentConnection.STATUS_DISCONNECTED


def test_the_status_is_disconnected_once_stopped_in_a_pause(monkeypatch):
    monkeypatch.setattr(gaia, "PERSISTENT_RECONNECT_WINDOW_SECONDS", 600)

    def stopping_pause(self, seconds):
        self.stop()
        return True

    def always_fail_discover(mac, timeout):
        raise gaia.GaiaDiscoveryError("gone")

    monkeypatch.setattr(gaia.PersistentConnection, "wait_for_stop", stopping_pause)
    monkeypatch.setattr(gaia, "discover_gaia_channel", always_fail_discover)
    persistent = gaia.PersistentConnection("mac")

    with pytest.raises(gaia.GaiaCancelledError):
        gaia.connect_persistent(persistent, timeout=1.0)

    assert persistent.status == gaia.PersistentConnection.STATUS_DISCONNECTED


# The volume moved by the steps acknowledged, though the command failed.
def test_a_failed_send_logs_how_many_steps_were_sent(monkeypatch, caplog):
    monkeypatch.setattr(gaia, "PERSISTENT_RECONNECT_ATTEMPTS", 1)
    persistent = gaia.PersistentConnection("mac")
    persistent.transport = LinkLostAfterWriteTransport(acked=4)

    with pytest.raises(gaia.GaiaConnectError):
        gaia.send_volume_steps_persistent_aware(persistent, "mac", ["up"] * 6, timeout=1.0)

    assert "failed after 4 of 6 steps were acknowledged" in caplog.text


def test_a_failed_one_off_send_logs_how_many_steps_were_sent(monkeypatch, caplog):
    monkeypatch.setattr(gaia, "discover_gaia_channel", lambda mac, timeout: 5)
    monkeypatch.setattr(
        gaia, "open_rfcomm_socket", lambda mac, channel, timeout: LinkLostAfterWriteTransport(acked=2)
    )

    with pytest.raises(gaia.GaiaConnectError):
        gaia.send_volume_steps("mac", ["down"] * 6, timeout=1.0)

    assert "failed after 2 of 6 steps were acknowledged" in caplog.text


# The socket is released anyway, and the error would hide the real one.
def test_an_error_closing_the_socket_is_ignored():
    class FailingCloseSocket:
        def close(self):
            raise OSError(5, "Input/output error")

    gaia.RfcommSocketTransport(FailingCloseSocket()).close()


def test_steps_go_out_in_the_order_given(monkeypatch):
    transport = ScriptedTransport([_VOLUME_ACK] * 3)
    monkeypatch.setattr(gaia, "discover_gaia_channel", lambda mac, timeout: 5)
    monkeypatch.setattr(gaia, "open_rfcomm_socket", lambda mac, channel, timeout: transport)

    gaia.send_volume_steps("mac", ["up", "down", "up"], timeout=1.0)

    assert [frame[8] for frame in transport.written] == [0x41, 0x42, 0x41]


def test_a_retry_sends_the_steps_left_in_their_order(monkeypatch):
    lost = LinkLostAfterWriteTransport(acked=1)
    reopened = ScriptedTransport([_VOLUME_ACK] * 2)
    _reconnect_to(monkeypatch, reopened)
    persistent = gaia.PersistentConnection("mac")
    persistent.transport = lost

    gaia.send_volume_steps_persistent_aware(persistent, "mac", ["up", "up", "down", "down"], timeout=1.0)

    assert [frame[8] for frame in lost.written] == [0x41, 0x41]
    assert [frame[8] for frame in reopened.written] == [0x42, 0x42]


# As when a job outlived its entry: a reload doesn't wait for a send.
def test_a_second_session_with_the_same_btr5_waits_for_the_first(monkeypatch):
    first_in, release = threading.Event(), threading.Event()
    discovered = []

    def discover(mac, timeout):
        discovered.append(mac)
        if len(discovered) == 1:
            first_in.set()
            release.wait(5)
        return 5

    monkeypatch.setattr(gaia, "discover_gaia_channel", discover)
    monkeypatch.setattr(
        gaia, "open_rfcomm_socket", lambda mac, channel, timeout: ScriptedTransport([_BATTERY_ACK])
    )
    sessions = [
        threading.Thread(target=gaia.read_battery, args=(mac, 1.0))
        for mac in ("00:00:5E:00:53:01", "00:00:5e:00:53:01", "00:00:5E:00:53:02")
    ]
    sessions[0].start()
    assert first_in.wait(5)
    sessions[1].start()
    sessions[2].start()
    sessions[2].join(5)  # another BTR5 doesn't wait
    time.sleep(0.1)
    try:
        assert discovered == ["00:00:5E:00:53:01", "00:00:5E:00:53:02"]
    finally:
        release.set()
        for session in sessions:
            session.join(5)
    assert discovered[-1] == "00:00:5e:00:53:01"


# Presses expire meanwhile: the button drops them rather than send them late.
def test_a_send_that_cant_start_in_time_gives_up(monkeypatch):
    first_in, release = threading.Event(), threading.Event()

    def discover(mac, timeout):
        first_in.set()
        release.wait(5)
        return 5

    sent = []
    monkeypatch.setattr(gaia, "discover_gaia_channel", discover)
    monkeypatch.setattr(
        gaia, "open_rfcomm_socket", lambda mac, channel, timeout: ScriptedTransport([_BATTERY_ACK])
    )
    monkeypatch.setattr(gaia, "send_volume_steps", lambda mac, steps, timeout: sent.append(steps))
    earlier = threading.Thread(target=gaia.read_battery, args=("mac", 1.0))
    earlier.start()
    assert first_in.wait(5)
    started = time.monotonic()
    try:
        with pytest.raises(gaia.GaiaBusyError):
            gaia.send_volume_steps_persistent_aware(
                None, "mac", ["up"], 1.0, time.monotonic() + 0.2
            )
        assert time.monotonic() - started < 2
    finally:
        release.set()
        earlier.join(5)
    assert sent == []
