import pytest

from erza.config.schema import Config


def test_resolve_preset_returns_defaults_when_no_preset() -> None:
    config = Config()
    resolved = config.resolve_preset()
    assert resolved.model == config.agents.defaults.model
    assert resolved.provider == config.agents.defaults.provider
    assert resolved.max_tokens == config.agents.defaults.max_tokens
    assert resolved.context_window_tokens == config.agents.defaults.context_window_tokens
    assert resolved.temperature == config.agents.defaults.temperature
    assert resolved.reasoning_effort == config.agents.defaults.reasoning_effort


def test_legacy_defaults_config_without_presets_still_resolves() -> None:
    config = Config.model_validate(
        {
            "agents": {
                "defaults": {
                    "model": "deepseek/deepseek-chat",
                    "provider": "deepseek",
                    "maxTokens": 4096,
                    "contextWindowTokens": 128_000,
                    "temperature": 0.2,
                    "reasoningEffort": "low",
                }
            }
        }
    )

    resolved = config.resolve_preset()
    assert config.agents.defaults.model_preset is None
    assert config.model_presets == {}
    assert resolved.model == "deepseek/deepseek-chat"
    assert resolved.provider == "deepseek"
    assert resolved.max_tokens == 4096
    assert resolved.context_window_tokens == 128_000
    assert resolved.temperature == 0.2
    assert resolved.reasoning_effort == "low"


def test_resolve_preset_returns_active_preset() -> None:
    config = Config.model_validate(
        {
            "model_presets": {
                "fast": {
                    "model": "deepseek/deepseek-chat",
                    "provider": "deepseek",
                    "maxTokens": 4096,
                    "contextWindowTokens": 32_768,
                    "temperature": 0.5,
                    "reasoningEffort": "low",
                }
            },
            "agents": {
                "defaults": {
                    "modelPreset": "fast",
                }
            },
        }
    )
    resolved = config.resolve_preset()
    assert resolved.model == "deepseek/deepseek-chat"
    assert resolved.provider == "deepseek"
    assert resolved.max_tokens == 4096
    assert resolved.context_window_tokens == 32_768
    assert resolved.temperature == 0.5
    assert resolved.reasoning_effort == "low"


def test_default_preset_is_agents_defaults_even_when_named_preset_is_active() -> None:
    config = Config.model_validate(
        {
            "agents": {
                "defaults": {
                    "model": "deepseek/deepseek-chat",
                    "provider": "deepseek",
                    "modelPreset": "fast",
                }
            },
            "modelPresets": {
                "fast": {"model": "deepseek/deepseek-chat-v3", "provider": "deepseek"},
            },
        }
    )

    assert config.resolve_preset().model == "deepseek/deepseek-chat-v3"
    assert config.resolve_preset("default").model == "deepseek/deepseek-chat"


def test_model_presets_accepts_camel_case_root_key() -> None:
    config = Config.model_validate(
        {
            "modelPresets": {
                "fast": {
                    "model": "deepseek/deepseek-chat",
                    "provider": "deepseek",
                }
            },
        }
    )

    assert config.model_presets["fast"].model == "deepseek/deepseek-chat"
    assert config.model_presets["fast"].provider == "deepseek"


def test_resolve_preset_can_target_named_preset_without_activating() -> None:
    config = Config.model_validate(
        {
            "model_presets": {
                "fast": {"model": "deepseek/deepseek-chat", "provider": "deepseek"},
                "deep": {"model": "anthropic/claude-opus-4-5", "provider": "anthropic"},
            },
            "agents": {"defaults": {"modelPreset": "fast"}},
        }
    )

    resolved = config.resolve_preset("deep")
    assert resolved.model == "anthropic/claude-opus-4-5"
    assert resolved.provider == "anthropic"


def test_validator_rejects_unknown_preset() -> None:
    with pytest.raises(ValueError, match="model_preset 'unknown' not found in model_presets"):
        Config.model_validate(
            {
                "agents": {
                    "defaults": {
                        "modelPreset": "unknown",
                    }
                }
            }
        )


def test_model_preset_accepts_explicit_default_name() -> None:
    config = Config.model_validate(
        {
            "agents": {
                "defaults": {
                    "model": "deepseek/deepseek-chat",
                    "modelPreset": "default",
                }
            }
        }
    )

    assert config.resolve_preset().model == "deepseek/deepseek-chat"


def test_model_presets_rejects_reserved_default_name() -> None:

    with pytest.raises(ValueError, match="model_preset name 'default' is reserved"):
        Config.model_validate(
            {
                "modelPresets": {
                    "default": {"model": "custom-model"},
                },
            }
        )


def test_resolve_preset_rejects_unknown_named_preset() -> None:
    with pytest.raises(KeyError, match="model_preset 'missing' not found"):
        Config().resolve_preset("missing")


def test_match_provider_uses_preset_model() -> None:
    config = Config.model_validate(
        {
            "providers": {
                "custom": {"apiKey": "sk-test"},
            },
            "model_presets": {
                "fast": {
                    "model": "deepseek/deepseek-chat",
                    "provider": "custom",
                }
            },
            "agents": {
                "defaults": {
                    "modelPreset": "fast",
                }
            },
        }
    )
    name = config.get_provider_name()
    assert name == "custom"
