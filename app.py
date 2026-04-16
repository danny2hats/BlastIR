import collections
import csv
import io
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request

from flask import Flask, jsonify, render_template, request
import paho.mqtt.client as mqtt

app = Flask(__name__)

CODES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "codes.json")
LIRC_TX = "/dev/lirc0"
LIRC_RX = "/dev/lirc1"
MQTT_BROKER = "192.168.1.55"
MQTT_PORT = 1883
MQTT_USER = "irblaster"
MQTT_PASS = "irblaster123"
DEVICE_ID = "ir_blaster"

IRDB_API = "https://api.github.com/repos/probonopd/irdb/contents/codes"
IRDB_CDN = "https://cdn.jsdelivr.net/gh/probonopd/irdb@master/codes"

codes = {}
mqtt_client = None
_irdb_cache = {}
_log = collections.deque(maxlen=200)


def log(msg, level="INFO"):
    ts = time.strftime("%H:%M:%S")
    entry = {"ts": ts, "level": level, "msg": msg}
    _log.appendleft(entry)
    print(f"[{ts}] {level}: {msg}")

# --- Protocol definitions ---
# Timing in microseconds: (pulse, space) for header/one/zero, pulse for trail
PROTOCOLS = {
    "NEC":       {"header": (9000, 4500), "one": (560, 1690), "zero": (560, 560), "trail": 560},
    "NECx1":     {"header": (9000, 4500), "one": (560, 1690), "zero": (560, 560), "trail": 560},
    "NECx2":     {"header": (9000, 4500), "one": (560, 1690), "zero": (560, 560), "trail": 560},
    "Samsung32": {"header": (4500, 4500), "one": (560, 1690), "zero": (560, 560), "trail": 560},
    "Sony12":    {"header": (2400, 600), "one": (1200, 600), "zero": (600, 600), "trail": 0},
    "Sony15":    {"header": (2400, 600), "one": (1200, 600), "zero": (600, 600), "trail": 0},
    "Sony20":    {"header": (2400, 600), "one": (1200, 600), "zero": (600, 600), "trail": 0},
}

# Samsung TVs in irdb are listed as NECx2 but use Samsung32 header timing
PROTOCOL_OVERRIDES = {
    ("Samsung", "NECx2"): "Samsung32",
}


def render_protocol(protocol_name, device, subdevice, function, manufacturer=None):
    """Convert protocol notation to ir-ctl raw pulse/space format."""
    if manufacturer:
        key = (manufacturer, protocol_name)
        if key in PROTOCOL_OVERRIDES:
            protocol_name = PROTOCOL_OVERRIDES[key]

    proto = PROTOCOLS.get(protocol_name)
    if not proto:
        return None

    d = device & 0xFF
    s = subdevice & 0xFF
    f = function & 0xFF

    if protocol_name.startswith("Sony"):
        return _render_sony(proto, protocol_name, d, s, f)

    # NEC-family and Samsung32: D(8) + S(8) + F(8) + ~F(8), all LSB-first
    nf = (~f) & 0xFF
    bits = []
    for byte_val in [d, s, f, nf]:
        for i in range(8):
            bits.append((byte_val >> i) & 1)

    parts = [f"+{proto['header'][0]}", f"-{proto['header'][1]}"]
    for bit in bits:
        if bit:
            parts.extend([f"+{proto['one'][0]}", f"-{proto['one'][1]}"])
        else:
            parts.extend([f"+{proto['zero'][0]}", f"-{proto['zero'][1]}"])
    parts.append(f"+{proto['trail']}")
    return " ".join(parts)


def _render_sony(proto, protocol_name, device, subdevice, function):
    """Sony SIRC: command bits first, then device, LSB-first."""
    bits = []
    # Function: 7 bits LSB-first
    for i in range(7):
        bits.append((function >> i) & 1)
    if protocol_name == "Sony12":
        for i in range(5):
            bits.append((device >> i) & 1)
    elif protocol_name == "Sony15":
        for i in range(8):
            bits.append((device >> i) & 1)
    elif protocol_name == "Sony20":
        for i in range(5):
            bits.append((device >> i) & 1)
        for i in range(8):
            bits.append((subdevice >> i) & 1)

    parts = [f"+{proto['header'][0]}", f"-{proto['header'][1]}"]
    for bit in bits:
        if bit:
            parts.extend([f"+{proto['one'][0]}", f"-{proto['one'][1]}"])
        else:
            parts.extend([f"+{proto['zero'][0]}", f"-{proto['zero'][1]}"])
    return " ".join(parts)


def detect_carrier(raw):
    """Guess carrier frequency from timing patterns."""
    parsed = parse_raw(raw)
    if not parsed:
        return 38000
    values = [v for _, v in parsed]
    avg_short = min(values)
    # RC5/RC6 uses ~889us half-bit (36kHz carrier)
    # NEC/Samsung uses ~560us pulse (38kHz carrier)
    if 800 <= avg_short <= 950:
        return 36000
    return 38000


def send_raw(raw, repeats=1, gap_ms=90):
    """Send a raw IR code string via ir-ctl, with optional repeats."""
    carrier = detect_carrier(raw)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write(raw)
        f.flush()
        tmp = f.name
    try:
        ok_count = 0
        for i in range(repeats):
            cmd = ["ir-ctl", "-d", LIRC_TX, "-s", tmp, f"--carrier={carrier}"]
            cmd.append(f"--carrier={carrier}")
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if result.returncode != 0:
                log(f"ir-ctl error: {result.stderr.strip()}", "ERROR")
                return False, result.stderr.strip()
            ok_count += 1
            if i < repeats - 1:
                time.sleep(gap_ms / 1000.0)
        detail = f"Sent ({carrier // 1000}kHz"
        if repeats > 1:
            detail += f", {repeats}x"
        detail += ")"
        return True, detail
    except subprocess.TimeoutExpired:
        log("ir-ctl timed out", "ERROR")
        return False, "Timeout sending IR"
    finally:
        os.unlink(tmp)


# --- IR code cleanup ---

def parse_raw(raw):
    """Parse raw IR string into list of (sign, value) tuples."""
    tokens = raw.split()
    result = []
    for i, t in enumerate(tokens):
        if t.startswith('+'):
            result.append(('+', int(t[1:])))
        elif t.startswith('-'):
            result.append(('-', int(t[1:])))
        else:
            sign = '+' if i % 2 == 0 else '-'
            result.append((sign, int(t)))
    return result


def normalize_raw(raw):
    """Normalize raw IR timings by clustering similar values."""
    parsed = parse_raw(raw)
    if not parsed:
        return raw
    values = [v for _, v in parsed]

    # Build clusters: group values within 15% or 100us of each other
    clusters = []
    for v in sorted(set(values)):
        placed = False
        for c in clusters:
            if abs(v - c['center']) < max(100, c['center'] * 0.15):
                c['members'].append(v)
                c['center'] = sum(c['members']) // len(c['members'])
                placed = True
                break
        if not placed:
            clusters.append({'center': v, 'members': [v]})

    # Map each original value to its cluster center
    snap = {}
    for c in clusters:
        for v in c['members']:
            snap[v] = c['center']

    return " ".join(f"{s}{snap[v]}" for s, v in parsed)


def capture_ir_signal(timeout_secs=15):
    """Capture one real IR signal, filtering out noise. Returns raw string or None."""
    deadline = time.monotonic() + timeout_secs
    while time.monotonic() < deadline:
        remaining = max(1, int(deadline - time.monotonic()))
        try:
            result = subprocess.run(
                ["ir-ctl", "-d", LIRC_RX, "-r", "--one-shot"],
                capture_output=True, text=True, timeout=remaining,
            )
            if result.returncode != 0:
                return None
            raw = result.stdout.strip()
            if len(raw.split()) >= 10:
                return raw
        except subprocess.TimeoutExpired:
            return None
    return None


def median_merge(samples):
    """Merge multiple raw IR captures by taking the median of each position."""
    parsed = [parse_raw(s) for s in samples]
    # Use the shortest sample length (they should all be similar)
    min_len = min(len(p) for p in parsed)
    result = []
    for i in range(min_len):
        sign = parsed[0][i][0]
        vals = sorted(p[i][1] for p in parsed if i < len(p))
        median = vals[len(vals) // 2]
        result.append(f"{sign}{median}")
    return " ".join(result)


# --- Core code management ---

def load_codes():
    global codes
    if os.path.exists(CODES_FILE):
        with open(CODES_FILE) as f:
            codes = json.load(f)
    else:
        codes = {}


def save_codes():
    with open(CODES_FILE, "w") as f:
        json.dump(codes, f, indent=2)


def slugify(name):
    return re.sub(r"[^a-z0-9_]", "_", name.lower().strip()).strip("_")


def send_ir(code_name):
    if code_name not in codes:
        return False, f"Unknown code: {code_name}"
    code = codes[code_name]
    repeats = code.get("repeats", 3)
    return send_raw(code["raw"], repeats=repeats)


def publish_discovery():
    if mqtt_client is None or not mqtt_client.is_connected():
        return
    for name, data in codes.items():
        slug = slugify(name)
        config = {
            "name": name,
            "unique_id": f"{DEVICE_ID}_{slug}",
            "command_topic": f"{DEVICE_ID}/send/{slug}",
            "device": {
                "identifiers": [DEVICE_ID],
                "name": "IR Blaster",
                "model": "Raspberry Pi IR",
                "manufacturer": "DIY",
            },
        }
        topic = f"homeassistant/button/{DEVICE_ID}_{slug}/config"
        mqtt_client.publish(topic, json.dumps(config), retain=True)


def remove_discovery(name):
    if mqtt_client is None or not mqtt_client.is_connected():
        return
    slug = slugify(name)
    topic = f"homeassistant/button/{DEVICE_ID}_{slug}/config"
    mqtt_client.publish(topic, "", retain=True)


# --- MQTT ---

def on_mqtt_connect(client, userdata, flags, reason_code, properties=None):
    log(f"MQTT connected: {reason_code}")
    client.subscribe(f"{DEVICE_ID}/send/+")
    publish_discovery()


def on_mqtt_message(client, userdata, msg):
    parts = msg.topic.split("/")
    if len(parts) == 3 and parts[0] == DEVICE_ID and parts[1] == "send":
        slug = parts[2]
        for name in codes:
            if slugify(name) == slug:
                ok, detail = send_ir(name)
                log(f"MQTT send '{name}': {detail}", "OK" if ok else "ERROR")
                break


def start_mqtt():
    global mqtt_client
    try:
        mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)
        mqtt_client.on_connect = on_mqtt_connect
        mqtt_client.on_message = on_mqtt_message
        mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
        mqtt_client.loop_start()
    except Exception as e:
        log(f"MQTT connection failed: {e}", "ERROR")


# --- irdb browsing ---

def irdb_fetch(url):
    """Fetch a URL with caching."""
    if url in _irdb_cache:
        return _irdb_cache[url]
    req = urllib.request.Request(url, headers={"User-Agent": "ir-blaster/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = resp.read().decode()
        _irdb_cache[url] = data
        return data
    except Exception as e:
        raise RuntimeError(f"Failed to fetch {url}: {e}")


@app.route("/api/irdb/manufacturers")
def irdb_manufacturers():
    try:
        data = json.loads(irdb_fetch(IRDB_API))
        names = sorted([item["name"] for item in data if item["type"] == "dir"])
        return jsonify(names)
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/irdb/devices/<manufacturer>")
def irdb_devices(manufacturer):
    try:
        url = f"{IRDB_API}/{urllib.parse.quote(manufacturer, safe='')}"
        data = json.loads(irdb_fetch(url))
        names = sorted([item["name"] for item in data if item["type"] == "dir"])
        return jsonify(names)
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/irdb/codes/<manufacturer>/<device>")
def irdb_codes(manufacturer, device):
    try:
        url = f"{IRDB_API}/{urllib.parse.quote(manufacturer, safe='')}/{urllib.parse.quote(device, safe='')}"
        data = json.loads(irdb_fetch(url))
        csv_files = [item["name"] for item in data if item["name"].endswith(".csv")]
        # Limit to 15 files to avoid hammering the CDN
        csv_files = csv_files[:15]

        all_funcs = []
        for csv_file in csv_files:
            csv_url = f"{IRDB_CDN}/{urllib.parse.quote(manufacturer, safe='')}/{urllib.parse.quote(device, safe='')}/{urllib.parse.quote(csv_file, safe='')}"
            try:
                csv_data = irdb_fetch(csv_url)
                reader = csv.DictReader(io.StringIO(csv_data))
                for row in reader:
                    all_funcs.append({
                        "name": row.get("functionname", "Unknown"),
                        "protocol": row.get("protocol", ""),
                        "device": int(row.get("device", 0)),
                        "subdevice": int(row.get("subdevice", 0)),
                        "function": int(row.get("function", 0)),
                        "file": csv_file,
                    })
            except Exception:
                continue

        return jsonify(all_funcs)
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/irdb/try", methods=["POST"])
def irdb_try():
    """Render a protocol code and send it without saving."""
    data = request.json
    proto_name = data.get("protocol", "")
    raw = render_protocol(
        proto_name,
        data.get("device", 0),
        data.get("subdevice", 0),
        data.get("function", 0),
        manufacturer=data.get("manufacturer", ""),
    )
    if raw is None:
        log(f"Unsupported protocol: {proto_name}", "ERROR")
        return jsonify({"ok": False, "error": f"Unsupported protocol: {proto_name}"}), 400
    ok, detail = send_raw(raw, repeats=3)
    log(f"Try irdb code: {data.get('manufacturer','')}/{data.get('name','?')} - {detail}", "OK" if ok else "ERROR")
    return jsonify({"ok": ok, "detail": detail}), 200 if ok else 500


@app.route("/api/irdb/import", methods=["POST"])
def irdb_import():
    """Render a protocol code, save it, and publish MQTT discovery."""
    data = request.json
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"ok": False, "error": "Name required"}), 400
    if name in codes:
        return jsonify({"ok": False, "error": "Name already exists"}), 400

    raw = render_protocol(
        data.get("protocol", ""),
        data.get("device", 0),
        data.get("subdevice", 0),
        data.get("function", 0),
        manufacturer=data.get("manufacturer", ""),
    )
    if raw is None:
        return jsonify({"ok": False, "error": f"Unsupported protocol: {data.get('protocol')}"}), 400

    codes[name] = {"raw": raw}
    save_codes()
    publish_discovery()
    return jsonify({"ok": True, "name": name})


# --- Core routes ---

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/codes")
def api_codes():
    return jsonify(codes)


@app.route("/api/learn", methods=["POST"])
def api_learn():
    name = request.json.get("name", "").strip()
    if not name:
        return jsonify({"ok": False, "error": "Name required"}), 400
    if name in codes:
        return jsonify({"ok": False, "error": "Name already exists"}), 400
    raw = capture_ir_signal(15)
    if raw is None:
        return jsonify({"ok": False, "error": "Timeout - no IR signal detected"}), 408
    codes[name] = {"raw": normalize_raw(raw)}
    save_codes()
    publish_discovery()
    log(f"Learned '{name}' ({len(raw.split())} tokens)", "OK")
    return jsonify({"ok": True, "name": name})


@app.route("/api/clean/<name>", methods=["POST"])
def api_clean(name):
    if name not in codes:
        return jsonify({"ok": False, "error": "Not found"}), 404
    old = codes[name]["raw"]
    codes[name]["raw"] = normalize_raw(old)
    save_codes()
    old_tokens = len(set(t.lstrip('+-') for t in old.split()))
    new_tokens = len(set(t.lstrip('+-') for t in codes[name]["raw"].split()))
    log(f"Cleaned '{name}': {old_tokens} unique timings -> {new_tokens}", "OK")
    return jsonify({"ok": True, "unique_before": old_tokens, "unique_after": new_tokens})


@app.route("/api/enhance/<name>", methods=["POST"])
def api_enhance(name):
    if name not in codes:
        return jsonify({"ok": False, "error": "Not found"}), 404
    count = request.json.get("count", 3) if request.json else 3
    samples = []
    for i in range(count):
        raw = capture_ir_signal(15)
        if raw is None:
            return jsonify({"ok": False, "error": f"Timeout on capture {i+1} of {count}"}), 408
        samples.append(raw)
    merged = median_merge(samples)
    codes[name]["raw"] = normalize_raw(merged)
    save_codes()
    log(f"Enhanced '{name}' from {count} captures ({len(merged.split())} tokens)", "OK")
    return jsonify({"ok": True, "captures": count, "tokens": len(merged.split())})


@app.route("/api/log")
def api_log():
    return jsonify(list(_log))


@app.route("/api/send/<name>", methods=["POST"])
def api_send(name):
    ok, detail = send_ir(name)
    log(f"Send '{name}': {detail}", "OK" if ok else "ERROR")
    return jsonify({"ok": ok, "detail": detail}), 200 if ok else 500


@app.route("/api/repeats/<name>", methods=["POST"])
def api_repeats(name):
    if name not in codes:
        return jsonify({"ok": False, "error": "Not found"}), 404
    r = request.json.get("repeats", 3)
    r = max(1, min(10, int(r)))
    codes[name]["repeats"] = r
    save_codes()
    log(f"Set '{name}' repeats to {r}", "OK")
    return jsonify({"ok": True, "repeats": r})


@app.route("/api/delete/<name>", methods=["DELETE"])
def api_delete(name):
    if name not in codes:
        return jsonify({"ok": False, "error": "Not found"}), 404
    remove_discovery(name)
    del codes[name]
    save_codes()
    return jsonify({"ok": True})


SERVICES = ["ir-blaster", "wpa_supplicant@wlan0", "static-wlan0"]


@app.route("/api/services")
def api_services():
    results = []
    for svc in SERVICES:
        try:
            r = subprocess.run(
                ["systemctl", "is-active", svc],
                capture_output=True, text=True, timeout=5,
            )
            status = r.stdout.strip()
        except Exception:
            status = "unknown"
        results.append({"name": svc, "status": status})
    # MQTT status
    mqtt_ok = mqtt_client is not None and mqtt_client.is_connected()
    results.append({"name": "mqtt", "status": "connected" if mqtt_ok else "disconnected"})
    return jsonify(results)


@app.route("/api/services/<path:name>/restart", methods=["POST"])
def api_service_restart(name):
    if name == "mqtt":
        try:
            if mqtt_client:
                mqtt_client.disconnect()
            start_mqtt()
            log("MQTT reconnected", "OK")
            return jsonify({"ok": True})
        except Exception as e:
            log(f"MQTT reconnect failed: {e}", "ERROR")
            return jsonify({"ok": False, "error": str(e)}), 500
    if name not in SERVICES:
        return jsonify({"ok": False, "error": "Unknown service"}), 400
    if name == "ir-blaster":
        # Schedule restart after response is sent
        log("Restarting ir-blaster service...", "INFO")
        threading.Timer(1.0, lambda: subprocess.run(
            ["sudo", "systemctl", "restart", "ir-blaster"],
            timeout=10,
        )).start()
        return jsonify({"ok": True, "detail": "Restarting..."})
    try:
        r = subprocess.run(
            ["sudo", "systemctl", "restart", name],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode != 0:
            log(f"Restart {name} failed: {r.stderr.strip()}", "ERROR")
            return jsonify({"ok": False, "error": r.stderr.strip()}), 500
        log(f"Restarted {name}", "OK")
        return jsonify({"ok": True})
    except Exception as e:
        log(f"Restart {name} failed: {e}", "ERROR")
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    load_codes()
    start_mqtt()
    app.run(host="0.0.0.0", port=5000)
