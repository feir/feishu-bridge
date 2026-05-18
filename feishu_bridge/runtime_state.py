"""Runtime state persistence for FeishuBot.

Stores runtime overrides (agent_type, provider, model_override) that survive
bridge restarts. Separated from config.json which holds factory defaults.
"""

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class RuntimeState:
    agent_type: str | None = None
    provider: str | None = None
    model_override: str | None = None

    @classmethod
    def load(cls, path: Path) -> "RuntimeState":
        """Fail-open: corrupt/missing/malformed → empty state + log warning."""
        try:
            data = json.loads(path.read_text())
            if not isinstance(data, dict):
                log.warning("runtime-state at %s is not a JSON object; using defaults", path)
                return cls()

            def _str_or_none(key):
                v = data.get(key)
                if v is None:
                    return None
                if not isinstance(v, str) or not v:
                    log.warning("runtime-state: field %r has invalid value %r; ignoring", key, v)
                    return None
                return v

            return cls(
                agent_type=_str_or_none("agent_type"),
                provider=_str_or_none("provider"),
                model_override=_str_or_none("model_override"),
            )
        except FileNotFoundError:
            return cls()
        except (OSError, json.JSONDecodeError, TypeError):
            log.warning("runtime-state corrupt or unreadable at %s; using defaults", path, exc_info=True)
            return cls()

    def save(self, path: Path) -> None:
        """Atomic write: fsync + os.replace (aligned with SessionMap pattern).

        File format contract:
        - Key present with string value = active override
        - Key absent = no override (fallback to config default)
        - Never write null/None values; omit the key instead
        """
        data = {}
        if self.agent_type is not None:
            data["agent_type"] = self.agent_type
        if self.provider is not None:
            data["provider"] = self.provider
        if self.model_override is not None:
            data["model_override"] = self.model_override
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp), str(path))

    def validate(self, runner_classes: dict, provider_profiles: dict) -> "RuntimeState":
        """Validate each field independently; invalid → None + log warning."""
        validated = RuntimeState()
        if self.agent_type:
            if self.agent_type in runner_classes:
                validated.agent_type = self.agent_type
            else:
                log.warning("runtime-state: agent_type=%r not in runner_classes; ignoring", self.agent_type)
        if self.provider:
            if self.provider in provider_profiles:
                validated.provider = self.provider
            else:
                log.warning("runtime-state: provider=%r not in config; ignoring", self.provider)
        validated.model_override = self.model_override
        return validated
