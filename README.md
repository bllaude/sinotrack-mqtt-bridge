# sinotrack-mqtt-bridge

A lightweight, self-hosted bridge that receives GPS data from **Sinotrack ST-90x** (and compatible H02-protocol) trackers over TCP and publishes it to an **MQTT broker** — with optional **Home Assistant** auto-discovery.

```
[Sinotrack ST-901/905/906/907] ──TCP H02──► [sinotrack-mqtt-bridge] ──MQTT──► [Home Assistant / Node-RED / etc.]
```

---

## Features

- **H02 protocol support** — text packets (V1, V5, V6, V8) and binary packets
- **MQTT publishing** — normalized JSON on `sinotrack/<imei>/location`, `/status`, `/alarm`, `/heartbeat`, `/raw`
- **Home Assistant MQTT Discovery** — device tracker, speed sensor, SOS/low-battery/ignition binary sensors auto-configured
- **ACK handling** — sends `*HQ,<imei>,R12,<time>#` so devices don't retransmit
- **Multi-device** — one bridge handles unlimited concurrent connections
- **Docker-ready** — single container, environment variable overrides
- **Alarm routing** — SOS, power cut, vibration, overspeed alerts on a dedicated topic (not retained)
- **Zero external dependencies** beyond `paho-mqtt` and `pyyaml`

---

## Quick Start

### 1. Clone and configure

```bash
git clone https://github.com/bllaude/sinotrack-mqtt-bridge.git
cd sinotrack-mqtt-bridge
cp config/config.yaml.example config/config.yaml
```

Edit `config/config.yaml`:
```yaml
server:
  port: 8090

mqtt:
  host: "your-broker-ip"
  username: "mqtt_user"   # if your broker requires auth
  password: "mqtt_pass"
```

### 2. Run with Docker Compose (recommended)

```bash
docker compose up -d
```

This starts **Mosquitto** + the bridge. Point your tracker at your host on port `8090`.

### 3. Point your Sinotrack at the bridge

Send this SMS to your tracker's SIM card (replace with your server's IP and password):

```
8040000 <YOUR_SERVER_IP> 8090
```

Default tracker password is `0000`. The tracker will begin sending position data to your bridge.

---

## MQTT Topics

| Topic | Payload | Notes |
|---|---|---|
| `sinotrack/<imei>/location` | `{latitude, longitude, speed, course, timestamp, valid}` | Retained |
| `sinotrack/<imei>/status` | `{flags, alarm, raw_status, timestamp}` | Retained |
| `sinotrack/<imei>/alarm` | `{type, timestamp}` | **Not retained** |
| `sinotrack/<imei>/heartbeat` | `{timestamp, online}` | On Link keepalive |
| `sinotrack/<imei>/raw` | Full `ParsedPacket` as JSON | Retained |
| `sinotrack/bridge/status` | `online` / `offline` | LWT |

### Example location payload

```json
{
  "latitude": 14.5995,
  "longitude": 120.9842,
  "speed": 42.3,
  "course": 180.0,
  "timestamp": "2025-06-27T10:30:45+00:00",
  "valid": true,
  "gps_accuracy": 5.0
}
```

### Example status payload

```json
{
  "alarm": null,
  "raw_status": "FFFFFFFF",
  "flags": {
    "sos": false,
    "power_cut": false,
    "vibration": false,
    "door_open": false,
    "acc_on": true,
    "charging": false,
    "engine_cut": false,
    "low_battery": false,
    "overspeed": false,
    "geofence": false
  },
  "timestamp": "2025-06-27T10:30:45+00:00"
}
```

---

## Home Assistant

When `ha_discovery: true` (default), the bridge auto-creates these entities for each device the first time it connects:

| Entity | Type | Description |
|---|---|---|
| `device_tracker.sinotrack_<imei>` | Device Tracker | GPS position on the map |
| `sensor.sinotrack_<imei>_speed` | Sensor | Speed in km/h |
| `binary_sensor.sinotrack_<imei>_sos` | Binary Sensor | SOS alarm |
| `binary_sensor.sinotrack_<imei>_low_battery` | Binary Sensor | Low battery warning |
| `binary_sensor.sinotrack_<imei>_acc` | Binary Sensor | Ignition / ACC state |

No YAML config needed in HA — just ensure your HA MQTT integration is connected to the same broker.

---

## Configuration Reference

```yaml
server:
  host: "0.0.0.0"      # Listen address
  port: 8090            # H02 TCP port
  ack_enabled: true     # Send ACK after each text packet

mqtt:
  host: "localhost"
  port: 1883            # Use 8883 for TLS
  username: null
  password: null
  topic_prefix: "sinotrack"
  client_id: "sinotrack-mqtt-bridge"
  retain: true
  qos: 1
  ha_discovery: true
  ha_discovery_prefix: "homeassistant"

logging:
  level: "INFO"         # DEBUG | INFO | WARNING | ERROR
```

### Environment variable overrides (for Docker)

| Variable | Maps to |
|---|---|
| `MQTT_HOST` | `mqtt.host` |
| `MQTT_PORT` | `mqtt.port` |
| `MQTT_USER` | `mqtt.username` |
| `MQTT_PASSWORD` | `mqtt.password` |
| `MQTT_PREFIX` | `mqtt.topic_prefix` |
| `SERVER_HOST` | `server.host` |
| `SERVER_PORT` | `server.port` |

---

## Running Locally (without Docker)

```bash
pip install -r requirements.txt
python src/main.py --config config/config.yaml
# or with debug logging:
python src/main.py --debug
```

### Running tests

```bash
pip install pytest
pytest tests/ -v
```

---

## Supported Devices

Tested / known-compatible devices using the H02 protocol:

- Sinotrack ST-901, ST-901L (2G/4G)
- Sinotrack ST-903, ST-905, ST-906, ST-907
- Any device sending H02 text (V1/V5/V6/V8) or binary `$` packets

Other H02-compatible devices (LK series, CC-series, etc.) may work but are untested.

---

## Tracker SMS Commands Reference

| Command | Example | Effect |
|---|---|---|
| Set server IP/port | `8040000 192.168.1.10 8090` | Point tracker at your bridge |
| Set APN | `8030000 internet` | Configure GPRS APN |
| Set update interval (ACC on) | `8090000 30` | Report every 30 seconds |
| Set update interval (ACC off) | `8110000 300` | Report every 5 min when parked |
| Get config | `RCONF` | Device replies with current settings |
| Reset | `RESET` | Restart tracker |

---

## Architecture

```
src/
├── main.py           # Entry point, config loading, wires everything together
├── tcp_server.py     # Async TCP server, one coroutine per device connection
├── h02_parser.py     # H02 protocol parser (text V1/V5/V6/V8 + binary)
└── mqtt_publisher.py # MQTT client, topic routing, HA discovery

config/
└── config.yaml.example

tests/
└── test_h02_parser.py   # 20 unit tests for the parser
```

---

## License

MIT — see [LICENSE](LICENSE).
