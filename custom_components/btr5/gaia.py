from __future__ import annotations

import contextlib
import logging
import re
import socket
import subprocess
import threading
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
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
    PERSISTENT_RECONNECT_WINDOW_SECONDS,
)

_LOGGER = logging.getLogger(__name__)

_session_locks: dict[str, threading.RLock] = {}
_session_locks_guard = threading.Lock()


@contextlib.contextmanager
def _one_session_at_a_time(mac: str, start_by: float | None = None) -> Iterator[None]:
    """Held by the thread talking to this BTR5, for the whole action.

    The entry's asyncio lock orders actions, but a job can outlive it: an
    integration reload doesn't wait for a send under way, and a cancelled
    task doesn't stop its executor job. A second session meanwhile would
    contend for the BTR5's GAIA channel.

    With ``start_by`` (a time.monotonic() value), raises GaiaBusyError if
    another session hasn't ended by then.
    """
    with _session_locks_guard:
        lock = _session_locks.setdefault(mac.upper(), threading.RLock())
    timeout = -1 if start_by is None else max(0.0, start_by - time.monotonic())
    if not lock.acquire(timeout=timeout):
        raise GaiaBusyError("BTR5 was still busy with an earlier session")
    try:
        yield
    finally:
        lock.release()


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


class GaiaCancelledError(GaiaError):
    """Raised when the persistent connection has been stopped."""


class GaiaBusyError(GaiaError):
    """Raised when an earlier session with the BTR5 didn't end in time."""


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


class GaiaTransport(Protocol):
    def write(self, data: bytes, timeout: float) -> None: ...
    def read(self, timeout: float) -> bytes: ...
    def close(self) -> None: ...


class RfcommSocketTransport:
    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock

    def write(self, data: bytes, timeout: float) -> None:
        try:
            self._sock.settimeout(timeout)
            self._sock.sendall(data)
        except OSError as exc:
            raise GaiaConnectError(f"failed to write to BTR5: {exc}") from exc

    def read(self, timeout: float) -> bytes:
        try:
            self._sock.settimeout(timeout)
            data = self._sock.recv(256)
        except TimeoutError:
            return b""
        except OSError as exc:
            raise GaiaConnectError(f"failed to read from BTR5: {exc}") from exc
        if not data:
            raise GaiaConnectError("BTR5 closed the Bluetooth connection")
        return data

    def close(self) -> None:
        # The socket is released even so, and the error would replace the
        # one that led to closing it.
        with contextlib.suppress(OSError):
            self._sock.close()


def open_rfcomm_socket(mac: str, channel: int, connect_timeout: float = 10.0) -> RfcommSocketTransport:
    try:
        sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
    except (AttributeError, OSError) as exc:
        # AttributeError: a Python build without Bluetooth support.
        raise GaiaConnectError(f"could not create a Bluetooth socket: {exc}") from exc
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


def _write_volume_step(transport: GaiaTransport, direction: str, timeout: float) -> None:
    if direction not in ("up", "down"):
        raise ValueError(f"invalid direction: {direction!r}")
    payload = bytes([PAYLOAD_VOLUME_UP if direction == "up" else PAYLOAD_VOLUME_DOWN])
    frame = GaiaFrame(flags=0x00, vendor_id=GAIA_VENDOR_CSR, command_id=CMD_AV_REMOTE_CONTROL, payload=payload)
    transport.write(frame.encode(), timeout)


def _wait_for_volume_ack(transport: GaiaTransport, timeout: float) -> None:
    ack_payload = _wait_for_ack(transport, CMD_AV_REMOTE_CONTROL | GAIA_ACK_MASK, timeout)
    if not ack_payload or ack_payload[0] != 0x00:
        raise GaiaAckError(f"BTR5 rejected volume step: status {ack_payload!r}")


def send_volume_step_over_transport(transport: GaiaTransport, direction: str, timeout: float) -> None:
    _write_volume_step(transport, direction, timeout)
    _wait_for_volume_ack(transport, timeout)


def send_volume_steps(mac: str, steps: Sequence[str], timeout: float) -> None:
    """Send relative volume steps, in order, over a single RFCOMM connection.

    ``steps`` holds each step's direction, "up" or "down". Each step still
    blocks on its own acknowledgement before the next one is written; only
    the SDP discovery and the socket are shared.
    """
    with _one_session_at_a_time(mac):
        channel = discover_gaia_channel(mac, timeout)
        transport = open_rfcomm_socket(mac, channel, timeout)
        acked = 0
        try:
            for direction in steps:
                send_volume_step_over_transport(transport, direction, timeout)
                acked += 1
        except GaiaError:
            _log_steps_acknowledged(mac, acked, len(steps))
            raise
        finally:
            transport.close()


def _log_steps_acknowledged(mac: str, acked: int, count: int) -> None:
    """After a volume command failed: the volume moved by these steps."""
    if acked:
        _LOGGER.warning(
            "BTR5 %s volume command failed after %d of %d steps were acknowledged",
            mac,
            acked,
            count,
        )


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
    with _one_session_at_a_time(mac):
        channel = discover_gaia_channel(mac, timeout)
        transport = open_rfcomm_socket(mac, channel, timeout)
        try:
            return read_battery_over_transport(transport, timeout)
        finally:
            transport.close()


class PersistentConnection:
    """The RFCOMM link Stay Connected keeps open between actions.

    It saves the SDP discovery and RFCOMM handshake on each action but
    doesn't guarantee a connection: an action that fails over it reconnects
    and tries again, see _run_persistent_with_retries.

    ``status`` (disconnected/connecting/connected) is a plain attribute the
    connection status sensor polls, as this object has no event loop to push
    updates from.

    Only the holder of the entry's lock uses the link. ``stop()``, when Stay
    Connected is turned off, and ``resume()``, when it's turned back on
    before the link is closed, may be called from any thread. An action
    under way when it stops finishes without retries; later ones close the
    link and use one-off connections.
    """

    STATUS_DISCONNECTED = "disconnected"
    STATUS_CONNECTING = "connecting"
    STATUS_CONNECTED = "connected"

    def __init__(self, mac: str) -> None:
        self.mac = mac
        self.transport: GaiaTransport | None = None
        self.status = self.STATUS_DISCONNECTED
        self._stopped = threading.Event()

    @property
    def stopped(self) -> bool:
        return self._stopped.is_set()

    def stop(self) -> None:
        self._stopped.set()

    def resume(self) -> None:
        self._stopped.clear()

    def wait_for_stop(self, seconds: float) -> bool:
        """Wait up to ``seconds``; True if stopped by then."""
        self._stopped.wait(seconds)
        # Not wait()'s result: that's True once stop() woke it, even if
        # resume() came before it returned.
        return self.stopped

    def open(self, timeout: float, *, unless_stopped: bool = False) -> None:
        """Connect, unless connected already.

        With ``unless_stopped``, raise GaiaCancelledError instead once
        stopped, even if connected already or after the BTR5 was found.
        """
        if unless_stopped and self.stopped:
            raise GaiaCancelledError("Stay Connected was turned off")
        if self.transport is not None:
            return
        self.status = self.STATUS_CONNECTING
        try:
            channel = discover_gaia_channel(self.mac, timeout)
            if unless_stopped and self.stopped:
                raise GaiaCancelledError("Stay Connected was turned off")
            self.transport = open_rfcomm_socket(self.mac, channel, timeout)
        except Exception:
            self.status = self.STATUS_DISCONNECTED
            raise
        self.status = self.STATUS_CONNECTED

    def close(self) -> None:
        with _one_session_at_a_time(self.mac):
            transport, self.transport = self.transport, None
            if transport is not None:
                transport.close()
            self.status = self.STATUS_DISCONNECTED


def _in_use(persistent: PersistentConnection | None) -> PersistentConnection | None:
    """None once Stay Connected is off, for a one-off connection instead.

    The link may still be open then, holding the BTR5's channel: a second
    link to it would fail, so it's closed first.
    """
    if persistent is not None and persistent.stopped:
        persistent.close()
        return None
    return persistent


def _run_persistent_with_retries(
    persistent: PersistentConnection,
    timeout: float,
    action,
    attempts: int,
    finished=lambda: False,
    *,
    unless_stopped: bool = False,
):
    """Run ``action(transport)`` over the persistent connection.

    If the link fails, reconnects and runs the action again, so Stay
    Connected keeps working across a BTR5 power cycle: up to ``attempts``
    tries in all, a short pause apart, none starting later than
    PERSISTENT_RECONNECT_WINDOW_SECONDS after the first failure. There's no
    retry once ``finished()`` is true, when the BTR5 rejects the command
    (the link is fine), or once Stay Connected is turned off. Raises the
    last error if the action never succeeds, or with ``unless_stopped``,
    GaiaCancelledError once Stay Connected is turned off.
    """
    deadline = None
    for attempt in range(attempts):
        try:
            persistent.open(timeout, unless_stopped=unless_stopped)
            return action(persistent.transport)
        except (GaiaAckError, GaiaCancelledError):
            raise
        except GaiaError:
            persistent.close()
            if unless_stopped and persistent.stopped:
                raise GaiaCancelledError("Stay Connected was turned off") from None
            if finished():
                return None
            if deadline is None:
                deadline = time.monotonic() + PERSISTENT_RECONNECT_WINDOW_SECONDS
            if (
                attempt == attempts - 1
                or time.monotonic() + PERSISTENT_RECONNECT_DELAY_SECONDS >= deadline
            ):
                raise
            # Still reconnecting, for the Connection Status sensor.
            persistent.status = PersistentConnection.STATUS_CONNECTING
            # Turning Stay Connected off ends the pause, and the retries, at
            # once; with unless_stopped, the next open() raises instead.
            if persistent.wait_for_stop(PERSISTENT_RECONNECT_DELAY_SECONDS):
                persistent.status = PersistentConnection.STATUS_DISCONNECTED
                if not unless_stopped:
                    raise


def connect_persistent(persistent: PersistentConnection, timeout: float) -> None:
    """Open the Stay Connected link, retrying as an action would.

    The link is kept only once the BTR5 has answered a battery read on it,
    whatever the answer. Closed right after connecting, as turning Stay
    Connected off while connecting would, it can leave the BTR5 refusing
    every GAIA connection until it's reset. Closed 3 seconds or more after
    connecting, it hasn't done that in tests.

    Raises GaiaCancelledError once Stay Connected is turned off.
    """

    def _handshake(transport: GaiaTransport) -> None:
        try:
            read_battery_over_transport(transport, timeout)
        except GaiaAckError as exc:
            _LOGGER.debug("BTR5 %s answered the connection check with: %s", persistent.mac, exc)

    with _one_session_at_a_time(persistent.mac):
        _run_persistent_with_retries(
            persistent, timeout, _handshake, PERSISTENT_RECONNECT_ATTEMPTS, unless_stopped=True
        )


def send_volume_steps_persistent_aware(
    persistent: PersistentConnection | None,
    mac: str,
    steps: Sequence[str],
    timeout: float,
    start_by: float | None = None,
) -> None:
    """Send ``steps`` as send_volume_steps does, over the held link if any.

    With ``start_by``, raises GaiaBusyError instead if an earlier session
    with the BTR5 hasn't ended by then.
    """
    with _one_session_at_a_time(mac, start_by):
        _send_volume_steps_persistent_aware(persistent, mac, steps, timeout)


def _send_volume_steps_persistent_aware(
    persistent: PersistentConnection | None, mac: str, steps: Sequence[str], timeout: float
) -> None:
    persistent = _in_use(persistent)
    if persistent is None:
        send_volume_steps(mac, steps, timeout)
        return

    sent = acked = 0

    def _action(transport: GaiaTransport) -> None:
        nonlocal sent, acked
        while sent < len(steps):
            _write_volume_step(transport, steps[sent], timeout)
            sent += 1
            try:
                _wait_for_volume_ack(transport, timeout)
            except GaiaAckError:
                raise
            except GaiaError:
                # The step may have reached the BTR5 anyway, so a retry
                # doesn't send it again: the volume may fall a step short,
                # but never overshoots.
                _LOGGER.warning(
                    "BTR5 %s didn't acknowledge a volume step, which isn't sent again: "
                    "the volume may be a step short",
                    mac,
                )
                raise
            acked += 1

    try:
        _run_persistent_with_retries(
            persistent,
            timeout,
            _action,
            PERSISTENT_RECONNECT_ATTEMPTS,
            lambda: sent == len(steps),
        )
    except GaiaError:
        _log_steps_acknowledged(mac, acked, len(steps))
        raise


def read_battery_persistent_aware(
    persistent: PersistentConnection | None, mac: str, timeout: float
) -> int:
    with _one_session_at_a_time(mac):
        persistent = _in_use(persistent)
        if persistent is None:
            return read_battery(mac, timeout)

        # A single attempt: the next poll or button press reconnects, and a
        # retry loop would keep presses waiting while the BTR5 is off.
        return _run_persistent_with_retries(
            persistent, timeout, lambda transport: read_battery_over_transport(transport, timeout), 1
        )
