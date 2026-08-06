"""Offline tests for HF/ModelScope context-window queries in cli.models.

The root conftest short-circuits ``get_model_context_limit`` so AgentLoop
construction never touches the network; these tests exercise the real query
chain (candidate generation, endpoint fallback, response parsing, proxy
resolution) with respx routes and no internet access.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from miniunicorn.cli import models as m

# ---------------------------------------------------------------------------
# Pure parsers (no network, no respx)
# ---------------------------------------------------------------------------


class TestExtractContextFromConfigJson:
    def test_max_position_embeddings(self):
        assert m._extract_context_from_config_json({"max_position_embeddings": 32768}) == 32768

    def test_nested_text_config(self):
        payload = {"architectures": ["MiniMax"], "text_config": {"context_length": 1_000_000}}
        assert m._extract_context_from_config_json(payload) == 1_000_000

    def test_sliding_window(self):
        assert m._extract_context_from_config_json({"sliding_window": 4096}) == 4096

    def test_field_priority(self):
        payload = {"n_positions": 2048, "max_position_embeddings": 4096}
        assert m._extract_context_from_config_json(payload) == 4096

    def test_non_dict_returns_none(self):
        assert m._extract_context_from_config_json([1, 2]) is None
        assert m._extract_context_from_config_json(None) is None

    def test_zero_or_negative_ignored(self):
        assert m._extract_context_from_config_json({"context_length": 0}) is None
        assert m._extract_context_from_config_json({"context_length": -1}) is None


class TestExtractContextFromHfModelCard:
    def test_config_field(self):
        payload = {"config": {"max_position_embeddings": 65536}}
        assert m._extract_context_from_hf_model_card(payload) == 65536

    def test_tokenizer_model_max_length(self):
        payload = {"tokenizer_config": {"model_max_length": 128000}}
        assert m._extract_context_from_hf_model_card(payload) == 128000

    def test_tokenizer_absurd_value_filtered(self):
        payload = {"tokenizer_config": {"model_max_length": 40_960_000}}
        assert m._extract_context_from_hf_model_card(payload) is None

    def test_card_data_context_length(self):
        payload = {"cardData": {"context_length": 200_000}}
        assert m._extract_context_from_hf_model_card(payload) == 200_000

    def test_nested_text_config(self):
        payload = {"config": {"text_config": {"max_position_embeddings": 262144}}}
        assert m._extract_context_from_hf_model_card(payload) == 262144

    def test_empty_payload(self):
        assert m._extract_context_from_hf_model_card({}) is None


class TestExtractContextFromTokenizerConfig:
    def test_model_max_length(self):
        assert m._extract_context_from_tokenizer_config({"model_max_length": 32000}) == 32000

    def test_unbounded_sentinel_filtered(self):
        assert m._extract_context_from_tokenizer_config({"model_max_length": (1 << 31) - 1}) is None

    def test_absurd_value_filtered(self):
        assert m._extract_context_from_tokenizer_config({"model_max_length": 40_960_000}) is None

    def test_non_dict(self):
        assert m._extract_context_from_tokenizer_config("nope") is None


# ---------------------------------------------------------------------------
# Proxy resolution (no network: urllib.request mocked, client inspected)
# ---------------------------------------------------------------------------


class TestSystemProxyUrl:
    def test_reads_registry(self, monkeypatch):
        monkeypatch.setattr(
            "urllib.request.getproxies_registry",
            lambda: {"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"},
        )
        assert m._system_proxy_url() == "http://127.0.0.1:7897"

    def test_https_preferred(self, monkeypatch):
        monkeypatch.setattr(
            "urllib.request.getproxies_registry",
            lambda: {"http": "http://127.0.0.1:8080", "https": "http://proxy.corp:3128"},
        )
        assert m._system_proxy_url() == "http://proxy.corp:3128"

    def test_no_proxy_configured(self, monkeypatch):
        monkeypatch.setattr("urllib.request.getproxies_registry", lambda: {})
        assert m._system_proxy_url() is None


class TestHfHttpClient:
    def test_env_proxy_wins_without_explicit_proxy(self, monkeypatch):
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9999")
        client = m._hf_http_client(timeout=1.0)
        try:
            assert client.trust_env is True
            assert len(client._mounts) != 1
        finally:
            client.close()

    def test_system_proxy_fallback_applied(self, monkeypatch):
        monkeypatch.delenv("HTTP_PROXY", raising=False)
        monkeypatch.delenv("HTTPS_PROXY", raising=False)
        monkeypatch.delenv("ALL_PROXY", raising=False)
        monkeypatch.setattr(
            "urllib.request.getproxies_registry",
            lambda: {"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"},
        )
        client = m._hf_http_client(timeout=1.0)
        try:
            assert len(client._mounts) == 1
        finally:
            client.close()

    def test_no_proxy_anywhere_is_direct(self, monkeypatch):
        monkeypatch.delenv("HTTP_PROXY", raising=False)
        monkeypatch.delenv("HTTPS_PROXY", raising=False)
        monkeypatch.delenv("ALL_PROXY", raising=False)
        monkeypatch.setattr("urllib.request.getproxies_registry", lambda: {})
        client = m._hf_http_client(timeout=1.0)
        try:
            assert len(client._mounts) != 1
        finally:
            client.close()


class TestModelscopeHttpClient:
    def test_default_direct_no_trust_env(self, monkeypatch):
        monkeypatch.delenv(m.ENV_MODELSCOPE_TRUST_ENV, raising=False)
        client = m._modelscope_http_client(timeout=1.0)
        try:
            assert client.trust_env is False
        finally:
            client.close()

    def test_env_var_enables_trust_env(self, monkeypatch):
        monkeypatch.setenv(m.ENV_MODELSCOPE_TRUST_ENV, "1")
        client = m._modelscope_http_client(timeout=1.0)
        try:
            assert client.trust_env is True
        finally:
            client.close()

    def test_env_var_falsy_values_keep_direct(self, monkeypatch):
        for value in ("0", "false", "False"):
            monkeypatch.setenv(m.ENV_MODELSCOPE_TRUST_ENV, value)
            client = m._modelscope_http_client(timeout=1.0)
            try:
                assert client.trust_env is False
            finally:
                client.close()


# ---------------------------------------------------------------------------
# Endpoint queries (respx, offline)
# ---------------------------------------------------------------------------

_HF_CARD = "https://huggingface.co/api/models/Qwen/Qwen2.5-0.5B"
_HF_CONFIG = "https://huggingface.co/Qwen/Qwen2.5-0.5B/resolve/main/config.json"
_HF_TOKENIZER = "https://huggingface.co/Qwen/Qwen2.5-0.5B/resolve/main/tokenizer_config.json"


def _card_payload() -> dict:
    return {"modelId": "Qwen/Qwen2.5-0.5B", "config": {"max_position_embeddings": 32768}}


def _config_payload() -> dict:
    return {"model_type": "qwen2", "max_position_embeddings": 65536}


def _tokenizer_payload() -> dict:
    return {"model_max_length": 131072}


@respx.mock
def test_query_hf_card_and_configs_card_hit():
    respx.get(_HF_CARD).mock(return_value=httpx.Response(200, json=_card_payload()))
    respx.get(_HF_CONFIG).mock(return_value=httpx.Response(200, json=_config_payload()))
    respx.get(_HF_TOKENIZER).mock(return_value=httpx.Response(200, json=_tokenizer_payload()))
    result = m._query_hf_card_and_configs("Qwen/Qwen2.5-0.5B")
    assert result == (32768, "huggingface:card:Qwen/Qwen2.5-0.5B")


@respx.mock
def test_query_hf_card_and_configs_falls_back_to_config_json():
    respx.get(_HF_CARD).mock(return_value=httpx.Response(404))
    respx.get(_HF_CONFIG).mock(return_value=httpx.Response(200, json=_config_payload()))
    result = m._query_hf_card_and_configs("Qwen/Qwen2.5-0.5B")
    assert result == (65536, "huggingface:config:Qwen/Qwen2.5-0.5B")


@respx.mock
def test_query_hf_card_and_configs_falls_back_to_tokenizer():
    respx.get(_HF_CARD).mock(return_value=httpx.Response(404))
    respx.get(_HF_CONFIG).mock(return_value=httpx.Response(404))
    respx.get(_HF_TOKENIZER).mock(return_value=httpx.Response(200, json=_tokenizer_payload()))
    result = m._query_hf_card_and_configs("Qwen/Qwen2.5-0.5B")
    assert result == (131072, "huggingface:tokenizer:Qwen/Qwen2.5-0.5B")


@respx.mock
def test_query_hf_card_and_configs_all_fail_returns_none():
    respx.get(_HF_CARD).mock(return_value=httpx.Response(404))
    respx.get(_HF_CONFIG).mock(return_value=httpx.Response(404))
    respx.get(_HF_TOKENIZER).mock(return_value=httpx.Response(404))
    assert m._query_hf_card_and_configs("Qwen/Qwen2.5-0.5B") is None


@respx.mock
def test_search_hf_models_parses_results():
    respx.get(url__regex=r"https://huggingface\.co/api/models\?search=").mock(
        return_value=httpx.Response(200, json=[{"id": "Qwen/Qwen2.5-0.5B"}])
    )
    assert m._search_hf_models("qwen2.5-0.5b") == ["Qwen/Qwen2.5-0.5B"]


@respx.mock
def test_query_hf_context_direct_hit():
    respx.get(_HF_CARD).mock(return_value=httpx.Response(200, json=_card_payload()))
    respx.get(_HF_CONFIG).mock(return_value=httpx.Response(200, json=_config_payload()))
    respx.get(_HF_TOKENIZER).mock(return_value=httpx.Response(200, json=_tokenizer_payload()))
    limit, source, model_id = m._query_hf_context_limit_with_search(
        "Qwen/Qwen2.5-0.5B", "qwen2.5-0.5b"
    )
    assert limit == 32768
    assert source == "huggingface:card:Qwen/Qwen2.5-0.5B"
    assert model_id == "Qwen/Qwen2.5-0.5B"


@respx.mock
def test_query_hf_context_search_hit():
    respx.get(url__regex=r"https://huggingface\.co/api/models\?search=").mock(
        return_value=httpx.Response(200, json=[{"id": "Qwen/Qwen2.5-0.5B"}])
    )
    respx.get(_HF_CARD).mock(return_value=httpx.Response(200, json=_card_payload()))
    respx.get(_HF_CONFIG).mock(return_value=httpx.Response(200, json=_config_payload()))
    respx.get(_HF_TOKENIZER).mock(return_value=httpx.Response(200, json=_tokenizer_payload()))
    limit, source, model_id = m._query_hf_context_limit_with_search(
        "nonexistent/qwen2.5-0.5b", "qwen2.5-0.5b"
    )
    assert limit == 32768
    assert model_id == "Qwen/Qwen2.5-0.5B"


@respx.mock
def test_query_hf_context_all_fail_raises():
    respx.get(url__regex=r"https://huggingface\.co/api/models\?search=").mock(return_value=httpx.Response(200, json=[]))
    with pytest.raises(RuntimeError, match="无法从 HuggingFace 查询"):
        m._query_hf_context_limit_with_search("Qwen/Qwen2.5-0.5B", "qwen2.5-0.5b")


_MS_CONFIG = (
    "https://modelscope.cn/api/v1/models/Qwen/Qwen2.5-0.5B/repo"
    "?Revision=master&FilePath=config.json"
)


@respx.mock
def test_query_modelscope_config_hit():
    respx.get(url__startswith="https://modelscope.cn/api/v1/models/Qwen/Qwen2.5-0.5B/repo").mock(
        return_value=httpx.Response(200, json=_config_payload())
    )
    assert m._query_modelscope_config("Qwen/Qwen2.5-0.5B") == (
        65536,
        "modelscope:config:Qwen/Qwen2.5-0.5B",
    )


@respx.mock
def test_query_modelscope_config_miss():
    respx.get(url__startswith="https://modelscope.cn/api/v1/models/Qwen/Qwen2.5-0.5B/repo").mock(
        return_value=httpx.Response(404)
    )
    assert m._query_modelscope_config("Qwen/Qwen2.5-0.5B") is None


@respx.mock
def test_query_modelscope_with_search_direct_hit():
    respx.get(url__startswith="https://modelscope.cn/api/v1/models/Qwen/Qwen2.5-0.5B/repo").mock(
        return_value=httpx.Response(200, json=_config_payload())
    )
    limit, source, model_id = m._query_modelscope_with_search(
        "Qwen/Qwen2.5-0.5B", "qwen2.5-0.5b"
    )
    assert limit == 65536
    assert model_id == "Qwen/Qwen2.5-0.5B"


@respx.mock
def test_query_model_context_limit_hf_then_modelscope_fallback():
    respx.get(url__regex=r"https://huggingface\.co/api/models\?search=").mock(return_value=httpx.Response(200, json=[]))
    respx.get(url__startswith="https://modelscope.cn/api/v1/models/").mock(
        return_value=httpx.Response(200, json=_config_payload())
    )
    limit, source, model_id = m._query_model_context_limit(
        "Qwen/Qwen2.5-0.5B", "qwen2.5-0.5b"
    )
    assert limit == 65536
    assert source.startswith("modelscope:")


@respx.mock
def test_query_model_context_limit_both_fail_raises():
    respx.get(url__regex=r"https://huggingface\.co/api/models\?search=").mock(return_value=httpx.Response(200, json=[]))
    respx.put(url__startswith="https://modelscope.cn/api/v1/models").mock(return_value=httpx.Response(200, json={}))
    with pytest.raises(RuntimeError, match="HuggingFace 和 ModelScope 均查询失败"):
        m._query_model_context_limit("Qwen/Qwen2.5-0.5B", "qwen2.5-0.5b")

