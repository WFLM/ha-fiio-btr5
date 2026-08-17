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
    assert gaia.discover_gaia_channel("40:ED:98:1A:A2:C9") == 14
    assert captured_args["args"] == ["sdptool", "search", "--bdaddr", "40:ED:98:1A:A2:C9", "SP"]


def test_discover_gaia_channel_raises_on_nonzero_exit(monkeypatch):
    def fake_run(args, capture_output, text, timeout, check):
        return gaia.subprocess.CompletedProcess(args, 1, stdout="", stderr="no such device")

    monkeypatch.setattr(gaia.subprocess, "run", fake_run)
    try:
        gaia.discover_gaia_channel("40:ED:98:1A:A2:C9")
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
    # Some CPython builds (e.g. the portable python-build-standalone ones used
    # by uv) are compiled without Bluetooth support, so these constants may be
    # absent in the test interpreter even though they exist on a normal Linux
    # build. Provide them so the test exercises our error handling, not the
    # interpreter's feature set.
    monkeypatch.setattr(gaia.socket, "AF_BLUETOOTH", 31, raising=False)
    monkeypatch.setattr(gaia.socket, "BTPROTO_RFCOMM", 3, raising=False)
    try:
        gaia.open_rfcomm_socket("40:ED:98:1A:A2:C9", 14)
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

    gaia.send_volume_steps("40:ED:98:1A:A2:C9", "up", 3, timeout=1.0)

    assert calls == {"discover": 1, "open": 1}
    assert transport.closed == 1
    assert transport.written == [bytes.fromhex("ff010001000a021f41")] * 3


def test_send_volume_steps_closes_transport_when_a_step_fails(monkeypatch):
    transport = CountingScriptedTransport([bytes.fromhex("ff010001000a821f01")])
    monkeypatch.setattr(gaia, "discover_gaia_channel", lambda mac, timeout: 14)
    monkeypatch.setattr(gaia, "open_rfcomm_socket", lambda mac, channel, connect_timeout: transport)

    try:
        gaia.send_volume_steps("40:ED:98:1A:A2:C9", "down", 3, timeout=1.0)
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
    persistent = gaia.PersistentConnection("40:ED:98:1A:A2:C9")
    assert persistent.status == gaia.PersistentConnection.STATUS_DISCONNECTED


def test_persistent_connection_opens_once_and_reuses_transport(monkeypatch):
    opens = []
    fake_transport = ScriptedTransport([])
    monkeypatch.setattr(gaia, "discover_gaia_channel", lambda mac, timeout: 5)
    monkeypatch.setattr(
        gaia, "open_rfcomm_socket", lambda mac, channel, timeout: (opens.append(1), fake_transport)[1]
    )

    persistent = gaia.PersistentConnection("40:ED:98:1A:A2:C9")
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

    persistent = gaia.PersistentConnection("40:ED:98:1A:A2:C9")
    persistent.open(timeout=1.0)

    assert seen_status["value"] == gaia.PersistentConnection.STATUS_CONNECTING


def test_persistent_connection_open_failure_leaves_status_disconnected(monkeypatch):
    def failing_discover(mac, timeout):
        raise gaia.GaiaDiscoveryError("no BTR5 found")

    monkeypatch.setattr(gaia, "discover_gaia_channel", failing_discover)
    persistent = gaia.PersistentConnection("40:ED:98:1A:A2:C9")

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
    persistent = gaia.PersistentConnection("40:ED:98:1A:A2:C9")
    persistent.transport = fake
    persistent.close()

    assert persistent.transport is None
    assert fake.closed is True
    assert persistent.status == gaia.PersistentConnection.STATUS_DISCONNECTED


def test_send_volume_steps_persistent_aware_reuses_open_transport():
    transport = ScriptedTransport([bytes.fromhex("ff010001000a821f00")] * 3)
    persistent = gaia.PersistentConnection("mac")
    persistent.transport = transport  # already open

    gaia.send_volume_steps_persistent_aware(persistent, "mac", "up", 3, timeout=1.0)

    assert len(transport.written) == 3
    assert persistent.transport is transport  # left open for the next call


def test_send_volume_steps_persistent_aware_opens_when_not_yet_open(monkeypatch):
    transport = ScriptedTransport([bytes.fromhex("ff010001000a821f00")])
    monkeypatch.setattr(gaia, "discover_gaia_channel", lambda mac, timeout: 5)
    monkeypatch.setattr(gaia, "open_rfcomm_socket", lambda mac, channel, timeout: transport)

    persistent = gaia.PersistentConnection("mac")
    gaia.send_volume_steps_persistent_aware(persistent, "mac", "up", 1, timeout=1.0)

    assert persistent.transport is transport


def test_send_volume_steps_persistent_aware_retries_until_reconnected(monkeypatch):
    class BrokenTransport:
        def write(self, data, timeout):
            raise gaia.GaiaConnectError("dead")

        def read(self, timeout):
            return b""

        def close(self):
            pass

    monkeypatch.setattr(gaia.time, "sleep", lambda seconds: None)

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

    gaia.send_volume_steps_persistent_aware(persistent, "mac", "up", 2, timeout=1.0)

    assert len(discover_attempts) == 3  # two failures, then success
    assert len(reopened_transport.written) == 2
    assert persistent.transport is reopened_transport  # reconnected, still held open


def test_send_volume_steps_persistent_aware_raises_after_exhausting_all_reconnect_attempts(monkeypatch):
    monkeypatch.setattr(gaia.time, "sleep", lambda seconds: None)
    attempts = []

    def always_fail_discover(mac, timeout):
        attempts.append(1)
        raise gaia.GaiaDiscoveryError("gone")

    monkeypatch.setattr(gaia, "discover_gaia_channel", always_fail_discover)
    persistent = gaia.PersistentConnection("mac")  # never successfully opened

    try:
        gaia.send_volume_steps_persistent_aware(persistent, "mac", "up", 1, timeout=1.0)
        assert False, "expected GaiaDiscoveryError"
    except gaia.GaiaDiscoveryError:
        pass

    assert len(attempts) == gaia.PERSISTENT_RECONNECT_ATTEMPTS


def test_send_volume_steps_persistent_aware_with_none_delegates_to_ephemeral(monkeypatch):
    calls = []
    monkeypatch.setattr(
        gaia,
        "send_volume_steps",
        lambda mac, direction, count, timeout: calls.append((mac, direction, count)),
    )

    gaia.send_volume_steps_persistent_aware(None, "mac", "up", 4, timeout=1.0)

    assert calls == [("mac", "up", 4)]


def test_read_battery_persistent_aware_reuses_open_transport():
    transport = ScriptedTransport([bytes.fromhex("ff010002000a8414004b")])
    persistent = gaia.PersistentConnection("mac")
    persistent.transport = transport

    battery = gaia.read_battery_persistent_aware(persistent, "mac", timeout=1.0)

    assert battery == 0x4B
    assert persistent.transport is transport


def test_read_battery_persistent_aware_retries_until_reconnected(monkeypatch):
    class BrokenTransport:
        def write(self, data, timeout):
            raise gaia.GaiaConnectError("dead")

        def read(self, timeout):
            return b""

        def close(self):
            pass

    monkeypatch.setattr(gaia.time, "sleep", lambda seconds: None)

    reopened_transport = ScriptedTransport([bytes.fromhex("ff010002000a8414004b")])
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

    battery = gaia.read_battery_persistent_aware(persistent, "mac", timeout=1.0)

    assert battery == 0x4B
    assert len(discover_attempts) == 3
    assert persistent.transport is reopened_transport  # reconnected, still held open


def test_read_battery_persistent_aware_raises_after_exhausting_all_reconnect_attempts(monkeypatch):
    monkeypatch.setattr(gaia.time, "sleep", lambda seconds: None)
    attempts = []

    def always_fail_discover(mac, timeout):
        attempts.append(1)
        raise gaia.GaiaDiscoveryError("gone")

    monkeypatch.setattr(gaia, "discover_gaia_channel", always_fail_discover)
    persistent = gaia.PersistentConnection("mac")  # never successfully opened

    try:
        gaia.read_battery_persistent_aware(persistent, "mac", timeout=1.0)
        assert False, "expected GaiaDiscoveryError"
    except gaia.GaiaDiscoveryError:
        pass

    assert len(attempts) == gaia.PERSISTENT_RECONNECT_ATTEMPTS


def test_read_battery_persistent_aware_with_none_delegates_to_ephemeral(monkeypatch):
    monkeypatch.setattr(gaia, "read_battery", lambda mac, timeout: 33)
    assert gaia.read_battery_persistent_aware(None, "mac", timeout=1.0) == 33
