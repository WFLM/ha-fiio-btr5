# FiiO BTR5 for Home Assistant

Volume Up / Volume Down buttons for a FiiO BTR5 2021, controlled over
Bluetooth GAIA/RFCOMM (the same protocol used while the BTR5 is a USB DAC —
this integration talks to it over its own Bluetooth radio, independently of
whatever machine has it plugged in over USB).

This is an independent, community reverse-engineering project. It is not
affiliated with, endorsed by, or supported by FiiO in any way; "FiiO" and
"BTR5" are used here only to identify the hardware this integration
targets.

## Why this exists

The BTR5 is used here as a USB DAC/amp for music playback on a computer.
The common recommendation for a setup like this is to leave the OS's
volume at 100% and do the actual volume control on the DAC/amp itself, to
avoid extra digital attenuation on the computer side. In practice, though,
the BTR5's own physical volume buttons are small and unresponsive — this
integration exists to control the BTR5's hardware volume remotely from
Home Assistant instead.

## What this does

- Adds two buttons, "Volume Up" and "Volume Down", that each send one or
  more relative volume-step commands to the BTR5.
- Adds two number entities under the device's Configuration section —
  "Step Size" (1–60, default 1) and "Debounce Window" (0.1–5s, default
  0.5s) — instead of a separate Options dialog, so they show up right on
  the BTR5 device page alongside the buttons/sensor/switch.
- Rapid presses are batched: consecutive presses on the same button within
  the configured Debounce Window are combined into a single Bluetooth
  session (one connection, N volume-step commands) instead of one
  connection per press. Each press returns immediately in the Home
  Assistant UI; the BTR5's volume actually changes shortly after your last
  press in a burst, not necessarily on each individual click. Batching
  mainly matters with "Stay Connected" off, where each new session costs a
  fresh SDP discovery + RFCOMM handshake. With "Stay Connected" on, that
  connection is already open, so you can set the Debounce Window down to
  its minimum (0.1s) and still get near-instant per-press response.
- Adds a battery sensor (percentage), polled every 10 minutes over the same
  Bluetooth connection mechanism, serialized against button presses so a
  poll and a press never open two simultaneous Bluetooth sessions.
- Adds a "Stay Connected" switch (under the device's Configuration
  section), off by default. Turning it on opens one Bluetooth connection
  and keeps it open; while on, button presses and battery polls reuse
  that connection instead of doing a fresh SDP discovery + RFCOMM
  handshake each time, so responses are near-instant. While the switch
  is on, any failure on the held connection (for example, the BTR5 was
  powered off and back on) triggers automatic reconnect attempts — a
  fresh SDP discovery + RFCOMM handshake, retried a bounded number of
  times with a short pause between attempts — instead of giving up after
  one retry. Only after all of those attempts fail does the action
  surface an error. There's no background health-check between actions;
  a dead connection is only noticed (and then repaired) the next time
  something tries to use it.
- Adds a "Connection Status" diagnostic sensor reporting one of
  **One-time** (Stay Connected is off — every action uses its own
  fresh connection), **Disconnected**, **Connecting…**, or **Connected**
  (the latter three only apply while Stay Connected is on). It's
  polled every few seconds, so it may lag the real state briefly.

## What this does not do

- No absolute volume readout or set. The BTR5 tracks two *separate* volume
  values — one for USB/DAC playback, one for Bluetooth/A2DP audio — and this
  integration only ever controls the USB/DAC one (the same one the volume
  buttons on the device itself change). No GAIA command was found that
  reads or sets that USB/DAC value as an absolute number; only relative
  up/down steps exist.

## Requirements

- Home Assistant with Bluetooth Classic (BR/EDR) access to the host it runs
  on — if HA runs in Docker, the container needs `network_mode: host` (or
  equivalent access to the host's Bluetooth adapter).
- The `sdptool` binary (from the `bluez` package) must be installed **inside
  the same container/environment that runs Home Assistant** — the integration
  shells out to it on every connection to find the BTR5's current GAIA RFCOMM
  channel. It is available in a standard Home Assistant Container or
  Home Assistant Supervised setup, but it is **not** present in Home Assistant
  OS and cannot be installed there, so **Home Assistant OS is not supported by
  this integration**.
- The BTR5 must be powered on and in Bluetooth range when adding the
  integration and when pressing a button.

## Installation

1. In HACS, add this repository as a custom repository (category:
   Integration).
2. Install "FiiO BTR5" from HACS.
3. Restart Home Assistant.
4. Settings → Devices & Services → Add Integration → "FiiO BTR5".
5. Enter the BTR5's Bluetooth MAC address (format `AA:BB:CC:DD:EE:FF`). If
   you don't already know it, find it from the same host that will run this
   integration:
   ```bash
   bluetoothctl scan on
   # wait for a line naming your BTR5, e.g.:
   # [NEW] Device 40:ED:98:1A:A2:C9 BTR5
   bluetoothctl scan off
   ```
   The address on that line is the MAC address to enter.
6. Home Assistant will discover the BTR5's GAIA RFCOMM channel and do a
   one-time battery-read connectivity check before saving.

## Troubleshooting

**Buttons show as available but do nothing after the BTR5 has been power
cycled (turned off and back on).** This does not need a Home Assistant
restart or an integration reload — fix it on the device itself instead:

1. Power the BTR5 back on.
2. Press its Pairing button (the long/oblong button) to put it into
   Bluetooth pairing mode.
3. Without waiting for pairing to actually complete, connect the BTR5 to
   USB.

That's enough to bring the BTR5's Bluetooth radio back into a state this
integration can talk to again — no action needed on the Home Assistant
side.
