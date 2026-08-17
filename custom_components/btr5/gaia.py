from __future__ import annotations

from dataclasses import dataclass


class GaiaError(Exception):
    """Base class for all btr5 GAIA protocol errors."""


class GaiaDiscoveryError(GaiaError):
    """Raised when SDP discovery finds no CSR GAIA service."""


class GaiaConnectError(GaiaError):
    """Raised when the RFCOMM connection to the BTR5 fails."""


class GaiaAckTimeoutError(GaiaError):
    """Raised when no matching acknowledgement frame arrives in time."""


class GaiaAckError(GaiaError):
    """Raised when an acknowledgement frame reports a non-success status."""


@dataclass
class GaiaFrame:
    flags: int
    vendor_id: int
    command_id: int
    payload: bytes

    def encode(self) -> bytes:
        return bytes(
            [
                0xFF,
                0x01,
                self.flags,
                len(self.payload),
                (self.vendor_id >> 8) & 0xFF,
                self.vendor_id & 0xFF,
                (self.command_id >> 8) & 0xFF,
                self.command_id & 0xFF,
            ]
        ) + self.payload


class GaiaStreamParser:
    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[GaiaFrame]:
        self._buffer.extend(data)
        frames = []
        while True:
            frame = self._try_parse_one()
            if frame is None:
                break
            frames.append(frame)
        return frames

    def _try_parse_one(self) -> GaiaFrame | None:
        buf = self._buffer
        if len(buf) < 8:
            return None
        if buf[0] != 0xFF or buf[1] != 0x01:
            raise GaiaError(f"bad GAIA frame sync bytes: {buf[0]:#x} {buf[1]:#x}")
        flags = buf[2]
        payload_len = buf[3]
        vendor_id = (buf[4] << 8) | buf[5]
        command_id = (buf[6] << 8) | buf[7]
        total_len = 8 + payload_len
        if len(buf) < total_len:
            return None
        payload = bytes(buf[8:total_len])
        del buf[:total_len]
        return GaiaFrame(flags=flags, vendor_id=vendor_id, command_id=command_id, payload=payload)


import re
import subprocess

_GAIA_SERVICE_MARKER = "CSR GAIA"
_CHANNEL_PATTERN = re.compile(r"Channel:\s*(\d+)")


def parse_sdp_gaia_channel(sdptool_output: str) -> int:
    for block in sdptool_output.split("\n\n"):
        if _GAIA_SERVICE_MARKER in block:
            match = _CHANNEL_PATTERN.search(block)
            if match:
                return int(match.group(1))
    raise GaiaDiscoveryError("no CSR GAIA service found in SDP records")


def discover_gaia_channel(mac: str, timeout: float = 10.0) -> int:
    try:
        result = subprocess.run(
            ["sdptool", "search", "--bdaddr", mac, "SP"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GaiaDiscoveryError(f"sdptool failed to run: {exc}") from exc
    if result.returncode != 0:
        raise GaiaDiscoveryError(f"sdptool exited with an error: {result.stderr.strip()}")
    return parse_sdp_gaia_channel(result.stdout)


import socket
import time
from typing import Protocol

from .const import (
    CMD_AV_REMOTE_CONTROL,
    CMD_BATTERY,
    GAIA_ACK_MASK,
    GAIA_VENDOR_CSR,
    MAX_BATTERY_PERCENT,
    PAYLOAD_VOLUME_DOWN,
    PAYLOAD_VOLUME_UP,
    PERSISTENT_RECONNECT_ATTEMPTS,
    PERSISTENT_RECONNECT_DELAY_SECONDS,
)


class GaiaTransport(Protocol):
    def write(self, data: bytes, timeout: float) -> None: ...
    def read(self, timeout: float) -> bytes: ...
    def close(self) -> None: ...


class RfcommSocketTransport:
    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock

    def write(self, data: bytes, timeout: float) -> None:
        self._sock.settimeout(timeout)
        try:
            self._sock.sendall(data)
        except OSError as exc:
            raise GaiaConnectError(f"failed to write to BTR5: {exc}") from exc

    def read(self, timeout: float) -> bytes:
        self._sock.settimeout(timeout)
        try:
            data = self._sock.recv(256)
        except TimeoutError:
            return b""
        except OSError as exc:
            raise GaiaConnectError(f"failed to read from BTR5: {exc}") from exc
        if not data:
            raise GaiaConnectError("BTR5 closed the Bluetooth connection")
        return data

    def close(self) -> None:
        self._sock.close()


def open_rfcomm_socket(mac: str, channel: int, connect_timeout: float = 10.0) -> RfcommSocketTransport:
    sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
    try:
        sock.settimeout(connect_timeout)
        sock.connect((mac, channel))
    except OSError as exc:
        sock.close()
        raise GaiaConnectError(f"could not connect to {mac} channel {channel}: {exc}") from exc
    return RfcommSocketTransport(sock)


def _wait_for_ack(transport: GaiaTransport, expected_command_id: int, timeout: float) -> bytes:
    parser = GaiaStreamParser()
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GaiaAckTimeoutError(
                f"no matching ack for command {expected_command_id:#06x} within {timeout}s"
            )
        chunk = transport.read(remaining)
        if chunk:
            for frame in parser.feed(chunk):
                if frame.command_id == expected_command_id:
                    return frame.payload


def send_volume_step_over_transport(transport: GaiaTransport, direction: str, timeout: float) -> None:
    if direction not in ("up", "down"):
        raise ValueError(f"invalid direction: {direction!r}")
    payload = bytes([PAYLOAD_VOLUME_UP if direction == "up" else PAYLOAD_VOLUME_DOWN])
    frame = GaiaFrame(flags=0x00, vendor_id=GAIA_VENDOR_CSR, command_id=CMD_AV_REMOTE_CONTROL, payload=payload)
    transport.write(frame.encode(), timeout)
    ack_payload = _wait_for_ack(transport, CMD_AV_REMOTE_CONTROL | GAIA_ACK_MASK, timeout)
    if not ack_payload or ack_payload[0] != 0x00:
        raise GaiaAckError(f"BTR5 rejected volume step: status {ack_payload!r}")


def send_volume_step(mac: str, direction: str, timeout: float) -> None:
    channel = discover_gaia_channel(mac, timeout)
    transport = open_rfcomm_socket(mac, channel, timeout)
    try:
        send_volume_step_over_transport(transport, direction, timeout)
    finally:
        transport.close()


def send_volume_steps(mac: str, direction: str, count: int, timeout: float) -> None:
    """Send ``count`` relative volume steps over a single RFCOMM connection.

    Each step still blocks on its own acknowledgement before the next one is
    written; only the SDP discovery and the socket are shared.
    """
    channel = discover_gaia_channel(mac, timeout)
    transport = open_rfcomm_socket(mac, channel, timeout)
    try:
        for _ in range(count):
            send_volume_step_over_transport(transport, direction, timeout)
    finally:
        transport.close()


def read_battery_over_transport(transport: GaiaTransport, timeout: float) -> int:
    frame = GaiaFrame(flags=0x00, vendor_id=GAIA_VENDOR_CSR, command_id=CMD_BATTERY, payload=b"")
    transport.write(frame.encode(), timeout)
    ack_payload = _wait_for_ack(transport, CMD_BATTERY | GAIA_ACK_MASK, timeout)
    if not ack_payload or ack_payload[0] != 0x00:
        raise GaiaAckError(f"BTR5 rejected battery read: status {ack_payload!r}")
    if len(ack_payload) < 2:
        raise GaiaAckError(f"BTR5 battery ack missing a value byte: {ack_payload!r}")
    value = ack_payload[1]
    if value > MAX_BATTERY_PERCENT:
        raise GaiaAckError(f"BTR5 battery ack reported an invalid value: {value}")
    return value


def read_battery(mac: str, timeout: float) -> int:
    channel = discover_gaia_channel(mac, timeout)
    transport = open_rfcomm_socket(mac, channel, timeout)
    try:
        return read_battery_over_transport(transport, timeout)
    finally:
        transport.close()


class PersistentConnection:
    """An optional, opt-in RFCOMM transport kept open across operations.

    This is purely a latency optimization (skip SDP discovery and the RFCOMM
    handshake on every action) — not a guarantee of connectivity. Any error
    while using the cached transport discards it; the caller falls back to a
    fresh one-off connection for that action, and the next action re-opens
    if needed.

    ``status`` tracks the connection's lifecycle (disconnected/connecting/
    connected) so a UI element can show it; it is a plain attribute polled
    by the caller rather than pushed, since this object has no event-loop
    access of its own.
    """

    STATUS_DISCONNECTED = "disconnected"
    STATUS_CONNECTING = "connecting"
    STATUS_CONNECTED = "connected"

    def __init__(self, mac: str) -> None:
        self.mac = mac
        self.transport: GaiaTransport | None = None
        self.status = self.STATUS_DISCONNECTED

    def open(self, timeout: float) -> None:
        if self.transport is not None:
            return
        self.status = self.STATUS_CONNECTING
        try:
            channel = discover_gaia_channel(self.mac, timeout)
            self.transport = open_rfcomm_socket(self.mac, channel, timeout)
        except GaiaError:
            self.status = self.STATUS_DISCONNECTED
            raise
        self.status = self.STATUS_CONNECTED

    def close(self) -> None:
        if self.transport is not None:
            self.transport.close()
            self.transport = None
        self.status = self.STATUS_DISCONNECTED


def _run_persistent_with_retries(persistent: PersistentConnection, timeout: float, action):
    """Run ``action(transport)`` against the persistent connection.

    On failure, discards the transport and retries by reconnecting, up to
    PERSISTENT_RECONNECT_ATTEMPTS total tries (with a short delay between
    attempts) — rather than falling back to a one-off connection. This is
    what lets "Stay Connected" keep working across a BTR5 power cycle
    without a fresh button press being required for every retry. Raises the
    last error if every attempt fails.
    """
    last_exc: GaiaError | None = None
    for attempt in range(PERSISTENT_RECONNECT_ATTEMPTS):
        try:
            persistent.open(timeout)
            return action(persistent.transport)
        except GaiaError as exc:
            last_exc = exc
            persistent.close()
            if attempt < PERSISTENT_RECONNECT_ATTEMPTS - 1:
                time.sleep(PERSISTENT_RECONNECT_DELAY_SECONDS)
    raise last_exc


def send_volume_steps_persistent_aware(
    persistent: PersistentConnection | None, mac: str, direction: str, count: int, timeout: float
) -> None:
    if persistent is None:
        send_volume_steps(mac, direction, count, timeout)
        return

    def _action(transport: GaiaTransport) -> None:
        for _ in range(count):
            send_volume_step_over_transport(transport, direction, timeout)

    _run_persistent_with_retries(persistent, timeout, _action)


def read_battery_persistent_aware(
    persistent: PersistentConnection | None, mac: str, timeout: float
) -> int:
    if persistent is None:
        return read_battery(mac, timeout)

    return _run_persistent_with_retries(
        persistent, timeout, lambda transport: read_battery_over_transport(transport, timeout)
    )
