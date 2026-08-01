import pytest
from pydantic import ValidationError

from miniunicorn.config.schema import ApiConfig, is_loopback_bind_host


@pytest.mark.parametrize("host", ["localhost", "LOCALHOST", "127.0.0.1", "127.2.3.4", "::1", "[::1]"])
def test_loopback_hosts_are_accepted_without_api_key(host):
    assert is_loopback_bind_host(host)
    assert ApiConfig(host=host).api_key == ""


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10", "api.example.test"])
def test_public_host_without_key_is_rejected(host):
    with pytest.raises(ValidationError, match="api_key"):
        ApiConfig(host=host)


def test_public_host_with_key_is_accepted():
    config = ApiConfig(host="0.0.0.0", api_key="secret")
    assert config.host == "0.0.0.0"


def test_explicit_insecure_override_is_accepted(caplog):
    config = ApiConfig(host="0.0.0.0", allow_insecure_public_bind=True)
    assert config.allow_insecure_public_bind is True
    assert "unauthenticated public bind explicitly enabled" in caplog.text
