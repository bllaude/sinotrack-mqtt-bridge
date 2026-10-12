"""
MQTT Publisher for sinotrack-mqtt-bridge.

Topic structure (configurable prefix, default: sinotrack):
  sinotrack/<imei>/location        → {lat, lon, speed, course, timestamp, valid}
  sinotrack/<imei>/status          → {flags, alarm, raw_status}
  sinotrack/<imei>/heartbeat       → {timestamp}
  sinotrack/<imei>/raw             → raw decoded JSON (full ParsedPacket)
  sinotrack/<imei>/lwt             → Last Will: "offline"

Home Assistant MQTT Discovery topics:
  homeassistant/device_tracker/<imei>/config
  homeassistant/sensor/<imei>_speed/config
  homeassistant/sensor/<imei>_battery/config
  homeassistant/binary_sensor/<imei>_sos/config
"""

import json
import logging
import time
from typing import Optional

try:
    import paho.mqtt.client as mqtt
except ImportError:  # Allow importing this module (e.g. for tests) without paho installed
    mqtt = None

from h02_parser import ParsedPacket

logger = logging.getLogger(__name__)


class MQTTPublisher:
    def __init__(self, config: dict):
        self.host      = config.get("host", "localhost")
        self.port      = int(config.get("port", 1883))
        self.username  = config.get("username")
        self.password  = config.get("password")
        self.prefix    = config.get("topic_prefix", "sinotrack").rstrip("/")
        self.ha_discovery = config.get("ha_discovery", True)
        self.ha_prefix    = config.get("ha_discovery_prefix", "homeassistant")
        self.retain    = config.get("retain", True)
        self.qos       = int(config.get("qos", 1))
        self.client_id = config.get("client_id", "sinotrack-mqtt-bridge")
        self._discovered: set[str] = set()
        self._client: Optional[mqtt.Client] = None

    # ── Connection ──────────────────────────────────────────────────────────

    def connect(self) -> None:
        if mqtt is None:
            raise RuntimeError(
                "paho-mqtt is not installed; run 'pip install -r requirements.txt'"
            )
        self._client = mqtt.Client(
            client_id=self.client_id,
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        )
        if self.username:
            self._client.username_pw_set(self.username, self.password)

        self._client.on_connect    = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_publish    = self._on_publish

        # LWT — bridge itself going offline
        self._client.will_set(
            f"{self.prefix}/bridge/status",
            payload="offline",
            qos=self.qos,
            retain=True,
        )

        self._client.connect(self.host, self.port, keepalive=60)
        self._client.loop_start()

    def disconnect(self) -> None:
        if self._client:
            self._publish(f"{self.prefix}/bridge/status", "offline")
            self._client.loop_stop()
            self._client.disconnect()

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            logger.info("MQTT connected to %s:%s", self.host, self.port)
            self._publish(f"{self.prefix}/bridge/status", "online")
        else:
            logger.error("MQTT connection failed, reason: %s", reason_code)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        logger.warning("MQTT disconnected (code %s), reconnecting…", reason_code)

    def _on_publish(self, client, userdata, mid, reason_code, properties):
        logger.debug("MQTT publish confirmed mid=%s", mid)

    # ── Publishing ──────────────────────────────────────────────────────────

    def _publish(self, topic: str, payload, retain: Optional[bool] = None) -> None:
        if self._client is None:
            logger.warning("MQTT client not connected, dropping message")
            return
        if isinstance(payload, dict):
            payload = json.dumps(payload)
        r = self.retain if retain is None else retain
        self._client.publish(topic, payload=payload, qos=self.qos, retain=r)
        logger.debug("→ MQTT %s : %s", topic, str(payload)[:120])

    def publish_packet(self, pkt: ParsedPacket) -> None:
        """Main entry point — routes a parsed packet to the right topics."""
        imei = pkt.imei
        base = f"{self.prefix}/{imei}"

        if pkt.packet_type == "heartbeat":
            self._publish(f"{base}/heartbeat", {
                "timestamp": pkt.timestamp.isoformat() if pkt.timestamp else None,
                "online": True,
            })
            return

        # Full raw payload
        self._publish(f"{base}/raw", pkt.to_dict())

        # Location payload
        if pkt.latitude is not None and pkt.longitude is not None:
            location = {
                "latitude":   pkt.latitude,
                "longitude":  pkt.longitude,
                "speed":      pkt.speed_kmh,
                "course":     pkt.course_deg,
                "timestamp":  pkt.timestamp.isoformat() if pkt.timestamp else None,
                "valid":      pkt.valid_fix,
                "gps_accuracy": 5.0,   # meters, conservative estimate
            }
            self._publish(f"{base}/location", location)

        # Status / alarm payload
        status_payload = {
            "alarm":      pkt.alarm,
            "raw_status": pkt.raw_status,
            "flags":      pkt.extra.get("flags", {}),
            "timestamp":  pkt.timestamp.isoformat() if pkt.timestamp else None,
        }
        self._publish(f"{base}/status", status_payload)

        # Dedicated alarm topic if there's an active alarm
        if pkt.alarm:
            self._publish(f"{base}/alarm", {
                "type":      pkt.alarm,
                "timestamp": pkt.timestamp.isoformat() if pkt.timestamp else None,
            }, retain=False)   # don't retain alarms
            logger.warning("ALARM [%s] on device %s: %s", pkt.alarm, imei, pkt.raw_status)

        # HA Discovery (once per device per session)
        if self.ha_discovery and imei not in self._discovered:
            self._publish_ha_discovery(imei)
            self._discovered.add(imei)

    # ── Home Assistant MQTT Discovery ───────────────────────────────────────

    def _ha_device(self, imei: str) -> dict:
        return {
            "identifiers": [f"sinotrack_{imei}"],
            "name":        f"Sinotrack {imei}",
            "model":       "ST-90x",
            "manufacturer": "Sinotrack / Auto Leaders",
            "via_device":  "sinotrack-mqtt-bridge",
        }

    def _publish_ha_discovery(self, imei: str) -> None:
        base_state = f"{self.prefix}/{imei}"
        device     = self._ha_device(imei)

        # device_tracker
        self._publish(
            f"{self.ha_prefix}/device_tracker/{imei}/config",
            {
                "name":            f"Sinotrack {imei}",
                "unique_id":       f"sinotrack_{imei}_tracker",
                "state_topic":     f"{base_state}/location",
                "json_attributes_topic": f"{base_state}/location",
                "payload_home":    "home",
                "payload_not_home": "not_home",
                "source_type":     "gps",
                "latitude_template":  "{{ value_json.latitude }}",
                "longitude_template": "{{ value_json.longitude }}",
                "accuracy_template":  "{{ value_json.gps_accuracy }}",
                "device":          device,
            },
        )

        # speed sensor
        self._publish(
            f"{self.ha_prefix}/sensor/{imei}_speed/config",
            {
                "name":            f"Sinotrack {imei} Speed",
                "unique_id":       f"sinotrack_{imei}_speed",
                "state_topic":     f"{base_state}/location",
                "value_template":  "{{ value_json.speed }}",
                "unit_of_measurement": "km/h",
                "device_class":    "speed",
                "device":          device,
            },
        )

        # SOS binary sensor
        self._publish(
            f"{self.ha_prefix}/binary_sensor/{imei}_sos/config",
            {
                "name":            f"Sinotrack {imei} SOS",
                "unique_id":       f"sinotrack_{imei}_sos",
                "state_topic":     f"{base_state}/status",
                "value_template":  "{{ value_json.flags.sos }}",
                "payload_on":      "True",
                "payload_off":     "False",
                "device_class":    "safety",
                "device":          device,
            },
        )

        # low battery binary sensor
        self._publish(
            f"{self.ha_prefix}/binary_sensor/{imei}_low_battery/config",
            {
                "name":            f"Sinotrack {imei} Low Battery",
                "unique_id":       f"sinotrack_{imei}_low_battery",
                "state_topic":     f"{base_state}/status",
                "value_template":  "{{ value_json.flags.low_battery }}",
                "payload_on":      "True",
                "payload_off":     "False",
                "device_class":    "battery",
                "device":          device,
            },
        )

        # ACC (ignition) binary sensor
        self._publish(
            f"{self.ha_prefix}/binary_sensor/{imei}_acc/config",
            {
                "name":            f"Sinotrack {imei} Ignition",
                "unique_id":       f"sinotrack_{imei}_acc",
                "state_topic":     f"{base_state}/status",
                "value_template":  "{{ value_json.flags.acc_on }}",
                "payload_on":      "True",
                "payload_off":     "False",
                "device_class":    "running",
                "icon":            "mdi:car-key",
                "device":          device,
            },
        )

        logger.info("HA discovery published for device %s", imei)