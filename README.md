# FiiO BTR5 USB DAC Remote for Home Assistant

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
- Rapid presses are batched: presses on either button, each within the
  configured Debounce Window of the one before, are combined into a single
  Bluetooth session (one connection, one volume-step command per step, in
  the order pressed) instead of one connection per press. Each press
  returns immediately in the Home Assistant UI; the BTR5's volume actually
  changes shortly after your last press in a burst, not necessarily on
  each individual click. Batching
  mainly matters with "Stay Connected" off, where each new session costs a
  fresh SDP discovery + RFCOMM handshake. With "Stay Connected" on, that
  connection is already open, so you can set the Debounce Window down to
  its minimum (0.1s) and still get near-instant per-press response.
  Presses made while a volume command is failing, or that wait over 10
  seconds for the BTR5 (for example, while Stay Connected reconnects), are
  dropped rather than changing the volume long after. The presses already
  being sent when the connection breaks are the exception: they're retried
  as described under "Stay Connected" below, so they may change the volume
  up to about a minute late.
- Reads the battery internally every 10 minutes as a lightweight connection
  check, serialized against button presses. The value is deliberately not
  exposed as a Home Assistant entity.
- Adds a "Stay Connected" switch (under the device's Configuration
  section), on by default. Its value is stored in the config entry, so an
  integration reload or Home Assistant restart does not reset it. Turning
  it on, or starting Home Assistant with it on, opens one Bluetooth
  connection and keeps it open; if the BTR5 doesn't answer at first, the
  connection is retried for about 30 seconds. While on, button presses and
  battery polls reuse that connection instead of doing a fresh SDP
  discovery + RFCOMM handshake each time, so responses are near-instant.
  The held connection also keeps the BTR5 from turning itself off when
  idle, even when it isn't connected over USB, so on battery it keeps
  draining it. While the switch
  is on, any failure on the held connection (for example, the BTR5 was
  powered off and back on) triggers automatic reconnect attempts — a
  fresh SDP discovery + RFCOMM handshake, retried with a short pause
  between attempts; new attempts start for about 30 seconds after the
  connection breaks. Only after all of those attempts fail does the action
  surface an error. A retry sends only the volume steps not yet sent: a
  step the BTR5 didn't acknowledge isn't sent again, so the volume may
  fall a step short (a warning is logged) but never overshoots. A command
  the BTR5 rejects isn't retried. The internal battery read tries once and
  leaves reconnecting to the next press or poll. Turning the switch off,
  unloading the integration, or stopping Home Assistant ends the retries
  at once; an action already under way still finishes, without them.
  There's no background health-check between actions; a dead connection
  is only noticed (and then repaired) the next time something tries to
  use it.
- Adds a "Connection Status" diagnostic sensor reporting one of
  **One-time** (Stay Connected is off — every action uses its own
  fresh connection), **Disconnected**, **Connecting…**, or **Connected**
  (the latter three only apply while Stay Connected is on). It's
  polled every few seconds, so it may lag the real state briefly.

## What this does not do

- No volume control while the BTR5 plays from a Bluetooth source. It
  works only while the BTR5 is a USB DAC; see Requirements.
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
- The `sdptool` binary (from BlueZ) must be available **inside the same
  container/environment that runs Home Assistant** — the integration shells
  out to it on every connection to find the BTR5's current GAIA RFCOMM
  channel. The official Home Assistant image includes it (the
  `bluez-deprecated` package), so Home Assistant Container and Supervised
  setups have it. Home Assistant OS runs that same image with access to
  the host's Bluetooth, and its kernel supports RFCOMM, so the integration
  should work there too, but it hasn't been tested on Home Assistant OS
  yet.
- The integration works only while the BTR5 is used as a USB DAC:
  connected over USB and selected as the computer's audio output (nothing
  needs to be playing). It can't change the volume while the BTR5 plays
  from a Bluetooth source. The BTR5 must also be in
  Bluetooth range of the Home Assistant host, both when adding the
  integration and when pressing a button.

## Installation

1. In **Settings → Marketplace** (HACS before Home Assistant 2026.11), add
   this repository as a custom repository (category: Integration).
2. Install "FiiO BTR5 USB DAC Remote" from there.
3. Restart Home Assistant.
4. If Home Assistant's Bluetooth integration is set up, the BTR5 usually
   shows up by itself as discovered in Settings → Devices & Services: click
   **Add**. Otherwise, Add Integration → "FiiO BTR5 USB DAC Remote" lists
   the BTR5s found nearby.
5. If yours isn't found, choose "Enter the MAC address" and enter the
   BTR5's Bluetooth MAC address (format `AA:BB:CC:DD:EE:FF`). If you don't
   already know it, find it from the same host that will run this
   integration:
   ```bash
   bluetoothctl scan on
   # wait for a line naming your BTR5, e.g.:
   # [NEW] Device 00:00:5E:00:53:01 FiiO BTR5
   bluetoothctl scan off
   ```
   The address on that line is the MAC address to enter.
6. Home Assistant will discover the BTR5's GAIA RFCOMM channel and do a
   one-time battery-read connectivity check before saving.

## Troubleshooting

First make sure the BTR5 is used as a USB DAC: connected over USB and
selected as the computer's audio output, not playing from a Bluetooth
source.

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

**The BTR5 refuses every connection, even after a power cycle.** The log
shows "Connection refused" although nothing else is connected to the
BTR5, and the steps above don't help. Reset the BTR5:

1. If you paired the Home Assistant host with the BTR5 yourself, remove
   that pairing on the host: `bluetoothctl remove AA:BB:CC:DD:EE:FF`.
2. Hold the BTR5's Power button for at least 10 seconds. A normal power
   off and on isn't enough. This seems to reset its Bluetooth settings.
3. If it still refuses, reset it fully: with the BTR5 on, hold Volume +
   and Volume − together for about 10 seconds, until it turns off, then
   turn it back on. This clears its pairing records and settings, so your
   other devices need pairing again.
