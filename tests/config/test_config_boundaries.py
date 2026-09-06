import pytest
from pydantic import ValidationError

from erza.config.schema import AgentDefaults, ProviderConfig, ProvidersConfig


def test_agent_defaults_reject_unknown_setting() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        AgentDefaults.model_validate({"unsupportedFeature": True})


def test_custom_provider_requires_object_shape() -> None:
    with pytest.raises(ValidationError, match="must be an object"):
        ProvidersConfig.model_validate({"customScalar": "not-an-object"})


def test_custom_provider_object_remains_supported() -> None:
    providers = ProvidersConfig.model_validate(
        {"teamGateway": {"apiKey": "secret", "apiBase": "https://example.test/v1"}}
    )
    custom = providers.__pydantic_extra__["teamGateway"]
    assert isinstance(custom, ProviderConfig)
    assert custom.api_key == "secret"
