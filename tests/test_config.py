"""
Tests for main.py config loading (defaults, YAML merge, env overrides).
Run: pytest tests/test_config.py -v
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from main import load_config, DEFAULT_CONFIG


def _clear_env(monkeypatch):
    for key in (
        "MQTT_HOST", "MQTT_PORT", "MQTT_USER", "MQTT_PASSWORD", "MQTT_PREFIX",
        "MQTT_CLIENT_ID", "MQTT_RETAIN", "MQTT_QOS", "HA_DISCOVERY",
        "SERVER_HOST", "SERVER_PORT", "ACK_ENABLED", "LOG_LEVEL",
    ):
        monkeypatch.delenv(key, raising=False)


class TestDefaults:
    def test_missing_file_uses_defaults(self, tmp_path, monkeypatch):
        _clear_env(monkeypatch)
        cfg = load_config(str(tmp_path / "nope.yaml"))
        assert cfg["server"]["port"] == 8090
        assert cfg["mqtt"]["host"] == "localhost"
        # defaults must not be mutated between calls
        assert cfg is not DEFAULT_CONFIG
        cfg["mqtt"]["host"] = "changed"
        assert DEFAULT_CONFIG["mqtt"]["host"] == "localhost"


class TestYamlMerge:
    def test_partial_yaml_deep_merges(self, tmp_path, monkeypatch):
        _clear_env(monkeypatch)
        p = tmp_path / "cfg.yaml"
        p.write_text("mqtt:\n  host: broker.local\n")
        cfg = load_config(str(p))
        assert cfg["mqtt"]["host"] == "broker.local"
        assert cfg["mqtt"]["port"] == 1883          # untouched default kept
        assert cfg["server"]["port"] == 8090


class TestEnvOverrides:
    def test_port_is_int(self, monkeypatch):
        _clear_env(monkeypatch)
        monkeypatch.setenv("MQTT_PORT", "8883")
        monkeypatch.setenv("SERVER_PORT", "9000")
        cfg = load_config("does-not-exist.yaml")
        assert cfg["mqtt"]["port"] == 8883
        assert cfg["server"]["port"] == 9000

    def test_bool_coercion(self, monkeypatch):
        _clear_env(monkeypatch)
        monkeypatch.setenv("ACK_ENABLED", "false")
        monkeypatch.setenv("HA_DISCOVERY", "true")
        monkeypatch.setenv("MQTT_RETAIN", "0")
        cfg = load_config("does-not-exist.yaml")
        assert cfg["server"]["ack_enabled"] is False
        assert cfg["mqtt"]["ha_discovery"] is True
        assert cfg["mqtt"]["retain"] is False

    def test_invalid_value_is_ignored(self, monkeypatch):
        _clear_env(monkeypatch)
        monkeypatch.setenv("MQTT_PORT", "not-a-number")
        cfg = load_config("does-not-exist.yaml")
        assert cfg["mqtt"]["port"] == 1883  # default kept

    def test_string_overrides(self, monkeypatch):
        _clear_env(monkeypatch)
        monkeypatch.setenv("MQTT_HOST", "10.0.0.5")
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")
        cfg = load_config("does-not-exist.yaml")
        assert cfg["mqtt"]["host"] == "10.0.0.5"
        assert cfg["logging"]["level"] == "DEBUG"