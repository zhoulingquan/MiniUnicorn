"""Tests for the model-context resolution boundary (Package A).

Verifies that runtime code (AgentLoop, provider snapshots, preset snapshots,
provider switching) cannot call Hugging Face / ModelScope lookup, and that
configuration-time resolution behaves correctly.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from miniunicorn.config.context_window import (
    UnresolvedModelContextError,
    require_context_window,
)
from miniunicorn.config.schema import Config, ModelPresetConfig


def _make_mock_provider(model: str = "test-model") -> MagicMock:
    """Create a properly configured LLM provider mock."""
    provider = MagicMock()
    provider.get_default_model.return_value = model
    provider.generation = SimpleNamespace(
        max_tokens=4096,
        temperature=0.1,
        reasoning_effort=None,
    )
    provider.estimate_prompt_tokens.return_value = (10_000, "test")
    return provider


# === Pure validator tests ===


class TestRequireContextWindow:
    def test_returns_value_when_positive_int(self) -> None:
        assert require_context_window("test-model", 128_000) == 128_000

    def test_returns_value_when_one(self) -> None:
        assert require_context_window("test-model", 1) == 1

    def test_raises_when_none(self) -> None:
        with pytest.raises(UnresolvedModelContextError, match="test-model"):
            require_context_window("test-model", None)

    def test_raises_when_zero(self) -> None:
        with pytest.raises(UnresolvedModelContextError, match="test-model"):
            require_context_window("test-model", 0)

    def test_raises_when_negative(self) -> None:
        with pytest.raises(UnresolvedModelContextError, match="test-model"):
            require_context_window("test-model", -1)

    def test_error_identifies_model(self) -> None:
        with pytest.raises(UnresolvedModelContextError) as exc_info:
            require_context_window("my-custom-model", None)
        assert "my-custom-model" in str(exc_info.value)
        assert exc_info.value.model == "my-custom-model"


# === Runtime lookup isolation tests ===


class TestRuntimeLookupIsolation:
    """Prove runtime code cannot call get_model_context_limit.

    A failure sentinel replaces the lookup function. If any runtime path
    calls it, the test fails with the sentinel exception.
    """

    def _failure_sentinel(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Replace get_model_context_limit with a function that always raises."""

        def _fail(*args: object, **kwargs: object) -> int:
            raise AssertionError(
                "Runtime code must not call get_model_context_limit. "
                "Configuration-time resolution should have persisted a concrete value."
            )

        # Patch in the source module so the import target is covered.
        monkeypatch.setattr("miniunicorn.cli.models.get_model_context_limit", _fail)

    def test_agent_loop_does_not_call_lookup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from miniunicorn.agent.loop import AgentLoop
        from miniunicorn.bus.queue import MessageBus

        self._failure_sentinel(monkeypatch)
        provider = _make_mock_provider("test-model")

        loop = AgentLoop(
            bus=MessageBus(),
            provider=provider,
            workspace=tmp_path,
            model="test-model",
            context_window_tokens=128_000,
        )
        assert loop.context_window_tokens == 128_000

    def test_build_provider_snapshot_does_not_call_lookup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from miniunicorn.providers.factory import build_provider_snapshot

        self._failure_sentinel(monkeypatch)
        config = Config()
        config.agents.defaults.model = "test-model"
        config.agents.defaults.provider = "deepseek"
        config.agents.defaults.context_window_tokens = 128_000
        config.providers.deepseek.api_key = "sk-test"

        snapshot = build_provider_snapshot(config)
        assert snapshot.context_window_tokens == 128_000

    def test_build_static_preset_snapshot_does_not_call_lookup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from miniunicorn.agent.model_presets import build_static_preset_snapshot
        from miniunicorn.providers.base import LLMProvider

        self._failure_sentinel(monkeypatch)
        provider = MagicMock(spec=LLMProvider)
        preset = ModelPresetConfig(
            model="test-model",
            provider="deepseek",
            context_window_tokens=128_000,
        )

        snapshot = build_static_preset_snapshot(provider, "test", preset)
        assert snapshot.context_window_tokens == 128_000

    def test_apply_provider_snapshot_does_not_call_lookup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from miniunicorn.agent.loop import AgentLoop
        from miniunicorn.bus.queue import MessageBus
        from miniunicorn.providers.factory import ProviderSnapshot

        self._failure_sentinel(monkeypatch)
        provider = _make_mock_provider("test-model")

        loop = AgentLoop(
            bus=MessageBus(),
            provider=provider,
            workspace=tmp_path,
            model="test-model",
            context_window_tokens=128_000,
        )

        new_provider = _make_mock_provider("new-model")
        snapshot = ProviderSnapshot(
            provider=new_provider,
            model="new-model",
            context_window_tokens=256_000,
            signature=("test",),
        )
        loop._apply_provider_snapshot(snapshot, publish_update=False)
        assert loop.context_window_tokens == 256_000


# === Configuration-time resolution tests ===


class TestConfigTimeResolution:
    """Verify save handlers resolve context window correctly."""

    def test_explicit_value_bypasses_discovery(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A valid manual value causes zero discovery requests."""
        from miniunicorn.config.loader import save_config
        from miniunicorn.webui.settings_api import update_agent_settings

        config_path = tmp_path / "config.json"
        save_config(Config(), config_path)
        monkeypatch.setattr("miniunicorn.config.loader._current_config_path", config_path)

        # Patch discovery to fail if called.
        def _fail_if_called(model: str, explicit: int | None) -> dict:
            raise AssertionError("Discovery should not be called when explicit value is provided")

        monkeypatch.setattr(
            "miniunicorn.webui.model_settings_api._resolve_context_window_for_save",
            _fail_if_called,
        )

        payload = update_agent_settings({"context_window_tokens": ["128000"]})
        assert payload["agent"]["context_window_tokens"] == 128_000

    def test_failed_new_model_lookup_produces_incomplete_model(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed new-model lookup saves as incomplete (inactive)."""
        from miniunicorn.config.loader import load_config, save_config
        from miniunicorn.webui.settings_api import create_model_configuration

        config_path = tmp_path / "config.json"
        config = Config()
        config.agents.defaults.model = "deepseek/deepseek-chat"
        config.agents.defaults.provider = "deepseek"
        config.providers.deepseek.api_key = "sk-test"
        save_config(config, config_path)
        monkeypatch.setattr("miniunicorn.config.loader._current_config_path", config_path)

        # Patch discovery to return failure.
        monkeypatch.setattr(
            "miniunicorn.webui.model_settings_api._resolve_context_window_for_save",
            lambda model, explicit: {"limit": None, "status": "failed", "error": "not found"},
        )

        create_model_configuration(
            {
                "label": ["Broken Model"],
                "provider": ["deepseek"],
                "model": ["nonexistent/model"],
            }
        )

        saved = load_config(config_path)
        preset = saved.model_presets.get("broken-model")
        assert preset is not None
        # Incomplete model is NOT activated.
        assert saved.agents.defaults.model_preset != "broken-model"

    def test_successful_discovery_persists_concrete_integer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Successful discovery persists the concrete integer in the config."""
        from miniunicorn.config.loader import load_config, save_config
        from miniunicorn.webui.settings_api import create_model_configuration

        config_path = tmp_path / "config.json"
        config = Config()
        config.agents.defaults.model = "deepseek/deepseek-chat"
        config.agents.defaults.provider = "deepseek"
        config.providers.deepseek.api_key = "sk-test"
        save_config(config, config_path)
        monkeypatch.setattr("miniunicorn.config.loader._current_config_path", config_path)

        monkeypatch.setattr(
            "miniunicorn.webui.model_settings_api._resolve_context_window_for_save",
            lambda model, explicit: {"limit": 128_000, "status": "learned", "error": None},
        )

        create_model_configuration(
            {
                "label": ["Real Model"],
                "provider": ["deepseek"],
                "model": ["deepseek/deepseek-chat"],
            }
        )

        saved = load_config(config_path)
        preset = saved.model_presets["real-model"]
        assert preset.context_window_tokens == 128_000
        # Successful discovery activates the preset.
        assert saved.agents.defaults.model_preset == "real-model"
