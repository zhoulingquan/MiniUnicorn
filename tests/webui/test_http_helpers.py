"""Tests for the canonical WebUI HTTP response and header helpers."""

from __future__ import annotations

from websockets.datastructures import Headers

from miniunicorn.webui._http import (
    _case_insensitive_header,
    _http_error,
    _http_json_response,
    case_insensitive_header,
    http_error,
    http_json_response,
    http_response,
)


class TestHttpResponse:
    """``http_response`` builds a response with the standard header set."""

    def test_default_status_and_content_type(self) -> None:
        resp = http_response(b"hello")
        assert resp.status_code == 200
        assert resp.reason_phrase == "OK"
        assert resp.body == b"hello"
        assert resp.headers.get("Connection") == "close"
        assert resp.headers.get("Content-Length") == "5"
        assert resp.headers.get("Content-Type") == "text/plain; charset=utf-8"
        assert resp.headers.get("Date") is not None

    def test_custom_status_and_content_type(self) -> None:
        resp = http_response(b"<ok/>", status=201, content_type="application/xml")
        assert resp.status_code == 201
        assert resp.reason_phrase == "Created"
        assert resp.headers.get("Content-Type") == "application/xml"

    def test_extra_headers_appended_after_standard_headers(self) -> None:
        resp = http_response(
            b"data",
            extra_headers=[("X-Custom", "yes"), ("Cache-Control", "no-store")],
        )
        assert resp.headers.get("X-Custom") == "yes"
        assert resp.headers.get("Cache-Control") == "no-store"

    def test_header_order_date_first(self) -> None:
        resp = http_response(b"x")
        # The first header should be Date.
        first_key = list(resp.headers)[0]
        assert first_key.lower() == "date"

    def test_utf8_body_content_length(self) -> None:
        body = "héllo".encode("utf-8")
        resp = http_response(body)
        assert resp.headers.get("Content-Length") == str(len(body))


class TestHttpError:
    """``http_error`` builds a plain-text error response."""

    def test_default_message_uses_status_phrase(self) -> None:
        resp = http_error(404)
        assert resp.status_code == 404
        assert resp.reason_phrase == "Not Found"
        assert resp.body == b"Not Found"

    def test_custom_message(self) -> None:
        resp = http_error(400, "bad input")
        assert resp.body == b"bad input"

    def test_underscore_alias_matches_public(self) -> None:
        assert _http_error is http_error


class TestHttpJsonResponse:
    """``http_json_response`` builds a JSON response with UTF-8 encoding."""

    def test_json_body_encoded_utf8(self) -> None:
        resp = http_json_response({"msg": "héllo"})
        assert resp.status_code == 200
        assert resp.headers.get("Content-Type") == "application/json; charset=utf-8"
        import json

        assert json.loads(resp.body) == {"msg": "héllo"}

    def test_custom_status(self) -> None:
        resp = http_json_response({"error": "nope"}, status=500)
        assert resp.status_code == 500
        assert resp.reason_phrase == "Internal Server Error"

    def test_underscore_alias_matches_public(self) -> None:
        assert _http_json_response is http_json_response


class TestCaseInsensitiveHeader:
    """``case_insensitive_header`` reads headers without assuming casing."""

    def test_exact_key_match(self) -> None:
        headers = Headers([("X-Token", "abc")])
        assert case_insensitive_header(headers, "X-Token") == "abc"

    def test_lowercase_fallback(self) -> None:
        headers = Headers([("x-token", "abc")])
        assert case_insensitive_header(headers, "X-Token") == "abc"

    def test_missing_header_returns_empty_string(self) -> None:
        headers = Headers([])
        assert case_insensitive_header(headers, "X-Token") == ""

    def test_strips_whitespace(self) -> None:
        headers = Headers([("X-Token", "  abc  ")])
        assert case_insensitive_header(headers, "X-Token") == "abc"

    def test_underscore_alias_matches_public(self) -> None:
        assert _case_insensitive_header is case_insensitive_header


class TestQueryFirstReexport:
    """``_query_first`` is reused from ``webui._query`` via re-export."""

    def test_query_first_reexported_from_http_routes(self) -> None:
        from miniunicorn.channels.websocket._http_routes import _query_first as routes_qf
        from miniunicorn.webui._query import _query_first as query_qf

        assert routes_qf is query_qf

    def test_query_first_returns_first_value(self) -> None:
        from miniunicorn.channels.websocket._http_routes import _query_first

        assert _query_first({"k": ["a", "b"]}, "k") == "a"

    def test_query_first_returns_none_for_missing(self) -> None:
        from miniunicorn.channels.websocket._http_routes import _query_first

        assert _query_first({}, "k") is None
