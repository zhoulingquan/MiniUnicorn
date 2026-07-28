"""Tests for the canonical chunked-header reassembly helper."""

from __future__ import annotations

from websockets.datastructures import Headers

from miniunicorn.channels.websocket._chunked_header import (
    collect_chunked_header,
    collect_chunked_header_parts,
)


class TestCollectChunkedHeaderParts:
    """``collect_chunked_header_parts`` is the canonical collector."""

    def test_base_only_header(self) -> None:
        headers = Headers([("X-Payload", "hello")])
        parts = collect_chunked_header_parts(headers, "X-Payload")
        assert parts == {0: "hello"}

    def test_base_plus_numbered_suffixes(self) -> None:
        headers = Headers(
            [
                ("X-Payload", "head"),
                ("X-Payload-1", "-mid"),
                ("X-Payload-2", "-tail"),
            ]
        )
        parts = collect_chunked_header_parts(headers, "X-Payload")
        assert parts == {0: "head", 1: "-mid", 2: "-tail"}

    def test_case_insensitive_names(self) -> None:
        headers = Headers([("x-PAYLOAD", "head"), ("X-payload-1", "-tail")])
        parts = collect_chunked_header_parts(headers, "X-Payload")
        assert parts == {0: "head", 1: "-tail"}

    def test_numeric_ordering_preserved(self) -> None:
        headers = Headers(
            [
                ("X-Payload-2", "-tail"),
                ("X-Payload", "head"),
                ("X-Payload-10", "-last"),
                ("X-Payload-1", "-mid"),
            ]
        )
        parts = collect_chunked_header_parts(headers, "X-Payload")
        assert sorted(parts) == [0, 1, 2, 10]

    def test_non_numeric_suffix_rejected(self) -> None:
        headers = Headers([("X-Payload", "head"), ("X-Payload-abc", "skip")])
        parts = collect_chunked_header_parts(headers, "X-Payload")
        assert parts == {0: "head"}

    def test_duplicate_index_last_value_wins(self) -> None:
        headers = Headers(
            [
                ("X-Payload-1", "first"),
                ("X-Payload-1", "second"),
            ]
        )
        parts = collect_chunked_header_parts(headers, "X-Payload")
        assert parts == {1: "second"}

    def test_no_matching_headers_returns_empty(self) -> None:
        headers = Headers([("Other-Header", "value")])
        parts = collect_chunked_header_parts(headers, "X-Payload")
        assert parts == {}


class TestCollectChunkedHeader:
    """``collect_chunked_header`` joins parts in numeric order."""

    def test_empty_returns_empty_string(self) -> None:
        headers = Headers([("Other", "x")])
        assert collect_chunked_header(headers, "X-Payload") == ""

    def test_base_only(self) -> None:
        headers = Headers([("X-Payload", "hello")])
        assert collect_chunked_header(headers, "X-Payload") == "hello"

    def test_concatenation_in_order(self) -> None:
        headers = Headers(
            [
                ("X-Payload-2", "C"),
                ("X-Payload", "A"),
                ("X-Payload-1", "B"),
            ]
        )
        assert collect_chunked_header(headers, "X-Payload") == "ABC"

    def test_negative_suffix_accepted_and_sorts_before_zero(self) -> None:
        """Negative integer suffixes (e.g. ``--1`` → suffix ``-1``) are valid
        and sort before index 0, preserving the historical behavior.
        """
        headers = Headers([("X-Payload", "A"), ("X-Payload--1", "B")])
        # index -1 sorts before index 0, so B comes first.
        assert collect_chunked_header(headers, "X-Payload") == "BA"

    def test_large_numeric_indices_ordered_numerically_not_lexically(self) -> None:
        headers = Headers(
            [
                ("X-Payload-10", "Z"),
                ("X-Payload-2", "B"),
                ("X-Payload", "A"),
            ]
        )
        assert collect_chunked_header(headers, "X-Payload") == "ABZ"
