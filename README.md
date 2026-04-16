# BlastIR

A self-hosted IR remote blaster for Raspberry Pi with a web UI, Home Assistant integration, and a built-in browser for the public [irdb](https://github.com/probonopd/irdb) code database.

Turn any Pi with a cheap IR LED and receiver into a universal remote that Home Assistant can drive.

---

## Features

- **Learn from any remote** — point your existing remote at the Pi's IR receiver, press record, captures raw IR timings.
- **Multi-capture "enhance"** — averages three pulses with median merging to clean up noisy receivers.
- **Auto-clean timings** — clusters similar pulse widths to compress codes and improve reliability.
- **Browse irdb** — search ~2,000 manufacturers of known remote codes and try them live before saving.
- **Protocol rendering** — synthesises raw waveforms for NEC, NEC x1/x2, Samsung32, and Sony SIRC 12/15/20 directly from device/subdevice/function numbers.
- **Home Assistant auto-discovery** — every saved code shows up as a button in HA via MQTT.
- **Per-code repeat count** — some AV kit needs 3–5 repeats before it reacts; set this per button.
- **Services panel** — status + one-click restart for the app, WiFi, and MQTT.

## Hardware

| Part | Notes |
|------|-------|
| Raspberry Pi | Any model with GPIO — tested on Pi 3B. Zero W/2W work. |
| IR transmitter | A bare IR LED on GPIO 17 via a NPN transistor + current-limit resistor, or a ready-made blaster board. Use a 940nm LED. |
| IR receiver | Vishay TSOP38238 or similar on GPIO 18, 3.3V, with a 100nF decoupling cap. |
| SD card | 8GB+. Raspberry Pi OS Bookworm (Lite is fine). |

You can change the GPIO pins by setting `IR_TX_PIN=` / `IR_RX_PIN=` before running the installer.

## Install

On a fresh Raspberry Pi OS install:

```bash
git clone https://github.com/danny2hats/BlastIR.git
cd BlastIR
sudo ./install.sh
```

The installer will:
1. Install `lirc`, `ir-keytable`, Python venv, and Git.
2. Add the `gpio-ir` and `gpio-ir-tx` overlays to `config.txt`.
3. Point `lircd` at `/dev/lirc1` (the receiver).
4. Create a Python venv under `~/blastir/venv/` with Flask + paho-mqtt.
5. Install a `blastir.service` systemd unit and enable it at boot.
6. Grant the service account passwordless `systemctl restart blastir` (used by the web UI).

Reboot once after install so the IR overlays load.

## Configuration

All config lives in `/etc/blastir.env` — the systemd unit loads it automatically. The installer drops a starter file; edit it with:

```bash
sudo nano /etc/blastir.env
```

```ini
BLASTIR_MQTT_BROKER=192.168.1.10   # Home Assistant / Mosquitto IP
BLASTIR_MQTT_PORT=1883
BLASTIR_MQTT_USER=blastir
BLASTIR_MQTT_PASS=change-me
BLASTIR_DEVICE_ID=blastir          # MQTT device identifier in HA
BLASTIR_HTTP_HOST=0.0.0.0          # Web UI bind address
BLASTIR_HTTP_PORT=5000
```

Leave `BLASTIR_MQTT_BROKER` empty to run the web UI standalone (no Home Assistant integration).

Create a matching MQTT user in Home Assistant → Settings → Add-ons → Mosquitto broker → Configuration.

Then restart the service:

```bash
sudo systemctl restart blastir
```

### Full env reference

| Variable | Default | Description |
|---|---|---|
| `BLASTIR_MQTT_BROKER` | *(empty)* | Broker IP. Empty = MQTT disabled. |
| `BLASTIR_MQTT_PORT` | `1883` | |
| `BLASTIR_MQTT_USER` | *(empty)* | Optional. |
| `BLASTIR_MQTT_PASS` | *(empty)* | Optional. |
| `BLASTIR_DEVICE_ID` | `blastir` | Prefix for MQTT discovery topics. |
| `BLASTIR_HTTP_HOST` | `0.0.0.0` | |
| `BLASTIR_HTTP_PORT` | `5000` | |
| `BLASTIR_LIRC_TX` | `/dev/lirc0` | |
| `BLASTIR_LIRC_RX` | `/dev/lirc1` | |
| `BLASTIR_CODES_FILE` | `<app dir>/codes.json` | Where learned codes are stored. |

## Using the web UI

Browse to `http://<pi>:5000`.

### Learn a code
1. Type a name (e.g. `TV Power`).
2. Click **Record** and press the button on your remote within 15 seconds.
3. Click **Enhance** to capture three more presses and merge them — recommended for anything you actually rely on.
4. Click **Send** to test it.
5. Use `+` / `-` on the row to set how many times the code repeats when triggered.

### Browse irdb
1. Pick a manufacturer and device type.
2. Hit **Try** on any row to fire it at the target — it streams the code without saving.
3. Hit **Save** to render the protocol → raw pulses and store it locally.

## Home Assistant integration

Once MQTT is connected, every saved code publishes to:

```
homeassistant/button/<device_id>_<slug>/config
```

and listens for presses on:

```
<device_id>/send/<slug>
```

The device appears under **Settings → Devices → BlastIR** with one button per code. Stick them in a dashboard, automate them, wire them to voice assistants — whatever you like.

## API

| Method | Path | Description |
|---|---|---|
| `GET`    | `/api/codes`                        | List all saved codes |
| `POST`   | `/api/learn`                        | Capture one IR code; body `{name}` |
| `POST`   | `/api/enhance/<name>`               | Re-capture & median-merge an existing code |
| `POST`   | `/api/clean/<name>`                 | Cluster-normalise timings on an existing code |
| `POST`   | `/api/send/<name>`                  | Send a saved code |
| `POST`   | `/api/repeats/<name>`               | Set repeat count; body `{repeats}` |
| `DELETE` | `/api/delete/<name>`                | Delete a saved code |
| `GET`    | `/api/irdb/manufacturers`           | irdb manufacturer list |
| `GET`    | `/api/irdb/devices/<mfg>`           | irdb device types for manufacturer |
| `GET`    | `/api/irdb/codes/<mfg>/<dev>`       | irdb function list |
| `POST`   | `/api/irdb/try`                     | Render + send an irdb code without saving |
| `POST`   | `/api/irdb/import`                  | Render + save an irdb code |
| `GET`    | `/api/services`                     | Service status (app, WiFi, MQTT) |
| `POST`   | `/api/services/<name>/restart`      | Restart a service |
| `GET`    | `/api/log`                          | Last ~200 log lines |

## How the IR pipeline works

- **Transmit**: `ir-ctl -d /dev/lirc0 -s /tmp/pulse.txt --carrier=<hz>` — carrier is auto-detected from the shortest pulse (RC5-ish timings get 36 kHz, everything else 38 kHz). The `--carrier` flag must be set on every call because the driver otherwise caches the last value.
- **Receive**: `ir-ctl -d /dev/lirc1 -r --one-shot` returns raw pulse/space pairs in microseconds.
- **Samsung quirk**: Samsung TV entries in irdb are tagged `NECx2` but use Samsung32 header timing — the renderer transparently remaps them.

## Troubleshooting

**Nothing blasts.** Check `sudo systemctl status blastir` and `journalctl -u blastir -f`. Verify `/dev/lirc0` and `/dev/lirc1` both exist after reboot (`ls /dev/lirc*`).

**Learning times out.** The receiver needs to be pointed at the remote. Confirm wiring: VCC to 3.3V, GND to GND, OUT to GPIO 18. Test with `ir-keytable -t -s rc0` and press a button — you should see scancodes.

**Codes learn but don't play back.** Your transmitter LED is wired backwards, or you're using a visible-light LED. 940nm only.

**Works for the amp but not the TV (or vice versa).** Different carrier frequencies (36 vs 38 kHz). BlastIR always passes `--carrier` explicitly to avoid the driver caching the wrong value — if you see this, update to the latest version.

**Buttons don't appear in Home Assistant.** Check that code names don't slugify to the same thing (e.g. `+` and `-` both become empty → collision). Rename to `Volume Up` / `Volume Down` and restart the service.

## Licence

MIT — see [LICENSE](LICENSE).
