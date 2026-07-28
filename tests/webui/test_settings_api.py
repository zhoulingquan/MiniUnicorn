from __future__ import annotations

import json

import pytest

from miniunicorn.config.loader import load_config, save_config
from miniunicorn.config.schema import Config, ModelPresetConfig
from miniunicorn.webui.settings_api import (
    WebUISettingsError,
    create_model_configuration,
    settings_payload,
    update_agent_settings,
    update_model_configuration,
    update_network_safety_settings,
    update_provider_settings,
)


def test_create_model_configuration_writes_label_and_selects(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    config = Config()
    config.agents.defaults.model = "deepseek/deepseek-chat"
    config.agents.defaults.provider = "deepseek"
    config.providers.deepseek.api_key = "sk-test"
    save_config(config, config_path)
    monkeypatch.setattr("miniunicorn.config.loader._current_config_path", config_path)
    # Mock synchronous context-window discovery (replaces former background thread).
    monkeypatch.setattr(
        "miniunicorn.webui.model_settings_api._resolve_context_window_for_save",
        lambda model, explicit: {"limit": 128_000, "status": "learned", "error": None},
    )

    payload = create_model_configuration(
        {
            "label": ["Fast writing"],
            "provider": ["deepseek"],
            "model": ["deepseek/deepseek-chat"],
        }
    )

    assert payload["agent"]["model_preset"] == "fast-writing"
    assert payload["agent"]["model"] == "deepseek/deepseek-chat"
    rows = {row["name"]: row for row in payload["model_presets"]}
    assert rows["fast-writing"]["label"] == "Fast writing"

    saved = load_config(config_path)
    assert saved.agents.defaults.model_preset == "fast-writing"
    assert saved.model_presets["fast-writing"].label == "Fast writing"
    assert saved.model_presets["fast-writing"].model == "deepseek/deepseek-chat"
    assert saved.model_presets["fast-writing"].provider == "deepseek"

    with pytest.raises(WebUISettingsError) as duplicate:
        create_model_configuration(
            {
                "label": ["Fast writing"],
                "provider": ["deepseek"],
                "model": ["deepseek/deepseek-chat"],
            }
        )
    assert duplicate.value.status == 409


def test_create_model_configuration_rejects_unconfigured_provider(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    save_config(Config(), config_path)
    monkeypatch.setattr("miniunicorn.config.loader._current_config_path", config_path)

    with pytest.raises(WebUISettingsError, match="provider is not configured"):
        create_model_configuration(
            {
                "label": ["Deep"],
                "provider": ["deepseek"],
                "model": ["deepseek/deepseek-chat"],
            }
        )


def test_update_agent_settings_accepts_context_window_options(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    config = Config()
    save_config(config, config_path)
    monkeypatch.setattr("miniunicorn.config.loader._current_config_path", config_path)

    payload = update_agent_settings({"context_window_tokens": ["262144"]})

    assert payload["agent"]["context_window_tokens"] == 262144
    saved = load_config(config_path)
    assert saved.agents.defaults.context_window_tokens == 262144


def test_update_model_configuration_accepts_context_window_options(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    config = Config()
    config.model_presets["codex"] = ModelPresetConfig(
        label="Codex",
        provider="deepseek",
        model="deepseek/deepseek-chat",
    )
    save_config(config, config_path)
    monkeypatch.setattr("miniunicorn.config.loader._current_config_path", config_path)

    payload = update_model_configuration(
        {
            "name": ["codex"],
            "context_window_tokens": ["262144"],
        }
    )

    assert payload["agent"]["context_window_tokens"] == 262144
    saved = load_config(config_path)
    assert saved.model_presets["codex"].context_window_tokens == 262144


def test_update_context_window_accepts_arbitrary_values(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Any positive int in [1024, 10_000_000] is accepted (no whitelist)."""
    config_path = tmp_path / "config.json"
    save_config(Config(), config_path)
    monkeypatch.setattr("miniunicorn.config.loader._current_config_path", config_path)

    payload = update_agent_settings({"context_window_tokens": ["128000"]})

    assert payload["agent"]["context_window_tokens"] == 128000
    saved = load_config(config_path)
    assert saved.agents.defaults.context_window_tokens == 128000


def test_update_context_window_rejects_out_of_range(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Values outside [1024, 10_000_000] are rejected."""
    config_path = tmp_path / "config.json"
    save_config(Config(), config_path)
    monkeypatch.setattr("miniunicorn.config.loader._current_config_path", config_path)

    with pytest.raises(WebUISettingsError, match="context_window_tokens must be between"):
        update_agent_settings({"context_window_tokens": ["100"]})

    with pytest.raises(WebUISettingsError, match="context_window_tokens must be between"):
        update_agent_settings({"context_window_tokens": ["20000000"]})


def test_update_model_configuration_rejects_default_preset(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    save_config(Config(), config_path)
    monkeypatch.setattr("miniunicorn.config.loader._current_config_path", config_path)

    with pytest.raises(WebUISettingsError, match="model configuration is required"):
        update_model_configuration({"name": ["default"], "model": ["deepseek/deepseek-chat"]})


def test_settings_payload_includes_network_safety_fields(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    config = Config()
    config.tools.webui_allow_local_service_access = False
    config.tools.ssrf_whitelist = ["100.64.0.0/10"]
    save_config(config, config_path)
    monkeypatch.setattr("miniunicorn.config.loader._current_config_path", config_path)
    monkeypatch.setattr("miniunicorn.webui.workspaces.get_webui_dir", lambda: tmp_path / "webui")

    payload = settings_payload()

    assert payload["advanced"]["webui_allow_local_service_access"] is False
    assert payload["advanced"]["allow_local_preview_access"] is False
    assert payload["advanced"]["webui_default_access_mode"] == "default"
    assert payload["advanced"]["private_service_protection_enabled"] is True
    assert payload["advanced"]["ssrf_whitelist_count"] == 1


def test_update_network_safety_settings_writes_local_service_flag(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    save_config(Config(), config_path)
    monkeypatch.setattr("miniunicorn.config.loader._current_config_path", config_path)
    monkeypatch.setattr("miniunicorn.webui.workspaces.get_webui_dir", lambda: tmp_path / "webui")

    payload = update_network_safety_settings(
        {
            "webui_allow_local_service_access": ["false"],
            "webui_default_access_mode": ["full"],
        }
    )

    saved = load_config(config_path)
    saved_raw = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved.tools.webui_allow_local_service_access is False
    assert saved_raw["tools"]["webuiAllowLocalServiceAccess"] is False
    assert "allowLocalPreviewAccess" not in saved_raw["tools"]
    assert payload["advanced"]["webui_allow_local_service_access"] is False
    assert payload["advanced"]["webui_default_access_mode"] == "full"
    assert payload["requires_restart"] is True


def test_update_network_safety_settings_accepts_legacy_restricted_default_access(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    save_config(Config(), config_path)
    monkeypatch.setattr("miniunicorn.config.loader._current_config_path", config_path)
    monkeypatch.setattr("miniunicorn.webui.workspaces.get_webui_dir", lambda: tmp_path / "webui")

    payload = update_network_safety_settings({"webui_default_access_mode": ["restricted"]})

    assert payload["advanced"]["webui_default_access_mode"] == "default"


def test_update_network_safety_settings_default_access_is_webui_only(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    save_config(Config(), config_path)
    before = config_path.read_text(encoding="utf-8")
    monkeypatch.setattr("miniunicorn.config.loader._current_config_path", config_path)
    monkeypatch.setattr("miniunicorn.webui.workspaces.get_webui_dir", lambda: tmp_path / "webui")

    payload = update_network_safety_settings({"webui_default_access_mode": ["full"]})

    saved = load_config(config_path)
    assert config_path.read_text(encoding="utf-8") == before
    assert saved.tools.restrict_to_workspace is True
    assert payload["advanced"]["webui_default_access_mode"] == "full"
    assert payload["requires_restart"] is False


def test_update_provider_settings_atomic_credentials_and_model(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provider credentials + active model are saved in one ``save_config()``."""
    config_path = tmp_path / "config.json"
    config = Config()
    config.agents.defaults.model = "deepseek/deepseek-chat"
    config.agents.defaults.provider = "deepseek"
    save_config(config, config_path)
    monkeypatch.setattr("miniunicorn.config.loader._current_config_path", config_path)
    save_count = {"n": 0}
    real_save_config = save_config

    def _counting_save(cfg, path=None):
        save_count["n"] += 1
        return real_save_config(cfg, path)

    monkeypatch.setattr("miniunicorn.webui.model_settings_api.save_config", _counting_save)
    monkeypatch.setattr(
        "miniunicorn.webui.model_settings_api._resolve_context_window_for_save",
        lambda model, explicit: {"limit": 128_000, "status": "learned", "error": None},
    )

    payload = update_provider_settings(
        {
            "provider": ["deepseek"],
            "api_key": ["sk-new"],
            "api_base": ["https://api.deepseek.com"],
            "model": ["deepseek/deepseek-chat-v3"],
        }
    )

    # Exactly one save call for the whole atomic operation.
    assert save_count["n"] == 1
    saved = load_config(config_path)
    assert saved.providers.deepseek.api_key == "sk-new"
    assert saved.providers.deepseek.api_base == "https://api.deepseek.com"
    assert saved.agents.defaults.provider == "deepseek"
    assert saved.agents.defaults.model == "deepseek/deepseek-chat-v3"
    # The returned payload reflects the new active model.
    assert payload["agent"]["model"] == "deepseek/deepseek-chat-v3"
    assert payload["agent"]["provider"] == "deepseek"


def test_update_provider_settings_without_model_keeps_legacy_behavior(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Omitting ``model`` preserves the historical credentials-only flow."""
    config_path = tmp_path / "config.json"
    config = Config()
    config.agents.defaults.model = "deepseek/deepseek-chat"
    config.agents.defaults.provider = "deepseek"
    save_config(config, config_path)
    monkeypatch.setattr("miniunicorn.config.loader._current_config_path", config_path)

    payload = update_provider_settings(
        {
            "provider": ["deepseek"],
            "api_key": ["sk-only"],
        }
    )

    saved = load_config(config_path)
    assert saved.providers.deepseek.api_key == "sk-only"
    # Model/provider must remain untouched when no model field is submitted.
    assert saved.agents.defaults.model == "deepseek/deepseek-chat"
    assert saved.agents.defaults.provider == "deepseek"
    assert payload["agent"]["model"] == "deepseek/deepseek-chat"


def test_update_provider_settings_rejects_empty_model_without_writing(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A validation failure (empty model) must not persist anything."""
    config_path = tmp_path / "config.json"
    config = Config()
    config.agents.defaults.model = "deepseek/deepseek-chat"
    config.agents.defaults.provider = "deepseek"
    config.providers.deepseek.api_key = "sk-existing"
    save_config(config, config_path)
    before = config_path.read_text(encoding="utf-8")
    monkeypatch.setattr("miniunicorn.config.loader._current_config_path", config_path)

    with pytest.raises(WebUISettingsError, match="model is required"):
        update_provider_settings(
            {
                "provider": ["deepseek"],
                "api_key": ["sk-new"],
                "model": ["  "],
            }
        )

    # Nothing was written.
    assert config_path.read_text(encoding="utf-8") == before
    saved = load_config(config_path)
    assert saved.providers.deepseek.api_key == "sk-existing"
