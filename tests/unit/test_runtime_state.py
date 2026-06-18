"""Unit tests for RuntimeState (load/save/validate)."""

import json
import logging

import pytest

from feishu_bridge.runtime_state import RuntimeState


class TestLoad:
    def test_missing_file(self, tmp_path):
        state = RuntimeState.load(tmp_path / "nonexistent.json")
        assert state.agent_type is None
        assert state.provider is None
        assert state.model_override is None

    def test_missing_file_no_warning(self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING):
            RuntimeState.load(tmp_path / "nonexistent.json")
        assert not caplog.records

    def test_normal_load(self, tmp_path):
        p = tmp_path / "state.json"
        p.write_text('{"agent_type": "codex", "provider": "omlx", "model_override": "claude-opus-4-6"}')
        state = RuntimeState.load(p)
        assert state.agent_type == "codex"
        assert state.provider == "omlx"
        assert state.model_override == "claude-opus-4-6"

    def test_partial_load(self, tmp_path):
        p = tmp_path / "state.json"
        p.write_text('{"agent_type": "claude"}')
        state = RuntimeState.load(p)
        assert state.agent_type == "claude"
        assert state.provider is None
        assert state.model_override is None

    def test_empty_object(self, tmp_path):
        p = tmp_path / "state.json"
        p.write_text("{}")
        state = RuntimeState.load(p)
        assert state.agent_type is None

    def test_corrupt_json(self, tmp_path, caplog):
        p = tmp_path / "state.json"
        p.write_text("{broken json!!!")
        with caplog.at_level(logging.WARNING):
            state = RuntimeState.load(p)
        assert state.agent_type is None
        assert any("corrupt or unreadable" in r.message for r in caplog.records)

    def test_non_dict_json_array(self, tmp_path, caplog):
        p = tmp_path / "state.json"
        p.write_text("[1, 2, 3]")
        with caplog.at_level(logging.WARNING):
            state = RuntimeState.load(p)
        assert state.agent_type is None
        assert any("not a JSON object" in r.message for r in caplog.records)

    def test_non_dict_json_string(self, tmp_path, caplog):
        p = tmp_path / "state.json"
        p.write_text('"just a string"')
        with caplog.at_level(logging.WARNING):
            state = RuntimeState.load(p)
        assert state.agent_type is None
        assert any("not a JSON object" in r.message for r in caplog.records)

    def test_non_dict_json_number(self, tmp_path, caplog):
        p = tmp_path / "state.json"
        p.write_text("42")
        with caplog.at_level(logging.WARNING):
            state = RuntimeState.load(p)
        assert state.agent_type is None
        assert any("not a JSON object" in r.message for r in caplog.records)

    def test_non_string_field_value_list(self, tmp_path, caplog):
        p = tmp_path / "state.json"
        p.write_text('{"provider": [], "agent_type": "claude"}')
        with caplog.at_level(logging.WARNING):
            state = RuntimeState.load(p)
        assert state.provider is None
        assert state.agent_type == "claude"
        assert any("invalid value" in r.message for r in caplog.records)

    def test_non_string_field_value_dict(self, tmp_path, caplog):
        p = tmp_path / "state.json"
        p.write_text('{"agent_type": {"nested": true}}')
        with caplog.at_level(logging.WARNING):
            state = RuntimeState.load(p)
        assert state.agent_type is None
        assert any("invalid value" in r.message for r in caplog.records)

    def test_non_string_field_value_int(self, tmp_path, caplog):
        p = tmp_path / "state.json"
        p.write_text('{"model_override": 123}')
        with caplog.at_level(logging.WARNING):
            state = RuntimeState.load(p)
        assert state.model_override is None
        assert any("invalid value" in r.message for r in caplog.records)

    def test_empty_string_field_ignored(self, tmp_path, caplog):
        p = tmp_path / "state.json"
        p.write_text('{"agent_type": ""}')
        with caplog.at_level(logging.WARNING):
            state = RuntimeState.load(p)
        assert state.agent_type is None
        assert any("invalid value" in r.message for r in caplog.records)


class TestSave:
    def test_round_trip(self, tmp_path):
        p = tmp_path / "state.json"
        original = RuntimeState(agent_type="codex", provider="omlx", model_override="claude-opus-4-6")
        original.save(p)
        loaded = RuntimeState.load(p)
        assert loaded.agent_type == "codex"
        assert loaded.provider == "omlx"
        assert loaded.model_override == "claude-opus-4-6"

    def test_omit_none_keys(self, tmp_path):
        p = tmp_path / "state.json"
        RuntimeState(agent_type="claude").save(p)
        data = json.loads(p.read_text())
        assert "agent_type" in data
        assert "provider" not in data
        assert "model_override" not in data

    def test_empty_state_produces_empty_object(self, tmp_path):
        p = tmp_path / "state.json"
        RuntimeState().save(p)
        data = json.loads(p.read_text())
        assert data == {}

    def test_creates_parent_dirs(self, tmp_path):
        p = tmp_path / "deep" / "nested" / "state.json"
        RuntimeState(provider="omlx").save(p)
        assert p.exists()
        assert json.loads(p.read_text())["provider"] == "omlx"

    def test_atomic_replace(self, tmp_path):
        p = tmp_path / "state.json"
        RuntimeState(agent_type="claude").save(p)
        RuntimeState(agent_type="codex").save(p)
        assert json.loads(p.read_text())["agent_type"] == "codex"
        assert not p.with_suffix(".tmp").exists()

    def test_trailing_newline(self, tmp_path):
        p = tmp_path / "state.json"
        RuntimeState(agent_type="claude").save(p)
        assert p.read_text().endswith("\n")


class TestValidate:
    RUNNER_CLASSES = {"claude": object, "codex": object}
    PROVIDER_PROFILES = {"default": {}, "omlx": {}, "pi-local": {}}

    def test_valid_all_fields(self):
        state = RuntimeState(agent_type="codex", provider="omlx", model_override="test-model")
        validated = state.validate(self.RUNNER_CLASSES, self.PROVIDER_PROFILES)
        assert validated.agent_type == "codex"
        assert validated.provider == "omlx"
        assert validated.model_override == "test-model"

    def test_unknown_agent_type(self, caplog):
        state = RuntimeState(agent_type="removed_type")
        with caplog.at_level(logging.WARNING):
            validated = state.validate(self.RUNNER_CLASSES, self.PROVIDER_PROFILES)
        assert validated.agent_type is None
        assert any("not in runner_classes" in r.message for r in caplog.records)

    def test_unknown_provider(self, caplog):
        state = RuntimeState(provider="deleted_provider")
        with caplog.at_level(logging.WARNING):
            validated = state.validate(self.RUNNER_CLASSES, self.PROVIDER_PROFILES)
        assert validated.provider is None
        assert any("not in config" in r.message for r in caplog.records)

    def test_model_override_passthrough(self):
        state = RuntimeState(model_override="any-unknown-model")
        validated = state.validate(self.RUNNER_CLASSES, self.PROVIDER_PROFILES)
        assert validated.model_override == "any-unknown-model"

    def test_partial_invalid(self, caplog):
        state = RuntimeState(agent_type="removed", provider="omlx", model_override="opus")
        with caplog.at_level(logging.WARNING):
            validated = state.validate(self.RUNNER_CLASSES, self.PROVIDER_PROFILES)
        assert validated.agent_type is None
        assert validated.provider == "omlx"
        assert validated.model_override == "opus"

    def test_empty_state(self):
        validated = RuntimeState().validate(self.RUNNER_CLASSES, self.PROVIDER_PROFILES)
        assert validated.agent_type is None
        assert validated.provider is None
        assert validated.model_override is None
