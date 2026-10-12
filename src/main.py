"""
sinotrack-mqtt-bridge — main entry point.

Usage:
  python main.py                          # loads config.yaml
  python main.py --config /etc/bridge.yaml
  python main.py --debug
"""

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

import yaml

from h02_parser import ParsedPacket
from mqtt_publisher import MQTTPublisher
from tcp_server import TCPServer

logger = logging.getLogger(__name__)

DEFAULT_CONFIG = {
    "server": {
        "host": "0.0.0.0",
        "port": 8090,
        "ack_enabled": True,
        "max_connections": 100,
    },
    "mqtt": {
        "host": "localhost",
        "port": 1883,
        "username": None,
        "password": None,
        "topic_prefix": "sinotrack",
        "client_id": "sinotrack-mqtt-bridge",
        "retain": True,
        "qos": 1,
        "ha_discovery": True,
        "ha_discovery_prefix": "homeassistant",
    },
    "logging": {
        "level": "INFO",
    },
}

# Coerce string env values to the right type per config key
_INT_KEYS = {"port", "qos", "max_connections"}
_BOOL_KEYS = {"ack_enabled", "retain", "ha_discovery"}


def _coerce(key: str, value: str):
    if key in _INT_KEYS:
        return int(value)
    if key in _BOOL_KEYS:
        return value.strip().lower() in ("1", "true", "yes", "on")
    return value


def _deep_merge(base: dict, override: dict) -> dict:
    result = {}
    for k, v in base.items():
        # copy nested dicts so the source (e.g. DEFAULT_CONFIG) is never mutated
        result[k] = dict(v) if isinstance(v, dict) else v
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def load_config(path: str) -> dict:
    cfg = _deep_merge(DEFAULT_CONFIG, {})   # deep copy so DEFAULT_CONFIG is never mutated
    p = Path(path)
    if p.exists():
        with p.open() as f:
            user_cfg = yaml.safe_load(f) or {}
        cfg = _deep_merge(cfg, user_cfg)
        logger.info("Loaded config from %s", path)
    else:
        logger.warning("Config file %s not found, using defaults", path)
    # Allow env-var overrides for Docker deployments
    env_overrides = {
        "MQTT_HOST":       ("mqtt", "host"),
        "MQTT_PORT":       ("mqtt", "port"),
        "MQTT_USER":       ("mqtt", "username"),
        "MQTT_PASSWORD":   ("mqtt", "password"),
        "MQTT_PREFIX":     ("mqtt", "topic_prefix"),
        "MQTT_CLIENT_ID":  ("mqtt", "client_id"),
        "MQTT_RETAIN":     ("mqtt", "retain"),
        "MQTT_QOS":        ("mqtt", "qos"),
        "HA_DISCOVERY":    ("mqtt", "ha_discovery"),
        "SERVER_HOST":     ("server", "host"),
        "SERVER_PORT":     ("server", "port"),
        "ACK_ENABLED":     ("server", "ack_enabled"),
        "LOG_LEVEL":       ("logging", "level"),
    }
    for env_key, (section, key) in env_overrides.items():
        val = os.environ.get(env_key)
        if val is not None:
            try:
                coerced = _coerce(key, val)
            except (ValueError, TypeError):
                logger.warning("Ignoring invalid value for %s: %r", env_key, val)
            else:
                cfg[section][key] = coerced
    return cfg


async def run(config: dict) -> None:
    mqtt_cfg    = config["mqtt"]
    server_cfg  = config["server"]

    publisher = MQTTPublisher(mqtt_cfg)
    publisher.connect()

    def on_packet(pkt: ParsedPacket) -> None:
        try:
            publisher.publish_packet(pkt)
        except Exception as e:
            logger.error("Failed to publish packet: %s", e)

    server = TCPServer(
        host=server_cfg["host"],
        port=int(server_cfg["port"]),
        on_packet=on_packet,
        ack_enabled=server_cfg.get("ack_enabled", True),
        max_connections=int(server_cfg.get("max_connections", 100)),
    )
    await server.start()

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _shutdown(*_):
        logger.info("Shutdown signal received")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown)

    logger.info(
        "sinotrack-mqtt-bridge running — TCP %s:%s → MQTT %s:%s/%s",
        server_cfg["host"], server_cfg["port"],
        mqtt_cfg["host"], mqtt_cfg["port"], mqtt_cfg["topic_prefix"],
    )

    await stop_event.wait()
    await server.stop()
    publisher.disconnect()
    logger.info("Bridge stopped cleanly")


def main() -> None:
    parser = argparse.ArgumentParser(description="Sinotrack → MQTT bridge")
    parser.add_argument("--config", default="config/config.yaml", help="Path to config YAML")
    parser.add_argument("--debug",  action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    config   = load_config(args.config)
    log_level = "DEBUG" if args.debug else config["logging"].get("level", "INFO")
    logging.basicConfig(
        level=getattr(logging, log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    try:
        asyncio.run(run(config))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()