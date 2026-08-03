"""Known-answer tests for the Weixin AES-128-ECB crypto helpers (Task 18 Step 1).

These tests exercise the pure crypto functions extracted from
``miniunicorn.channels.weixin.channel`` into
``miniunicorn.channels.weixin.crypto``.  They cover:

* round-trip encrypt/decrypt with valid PKCS7 padding
* ``pkcs7_unpad_safe`` behaviour for valid padding, invalid padding, and
  empty/non-aligned inputs
* ``parse_aes_key`` for both encodings seen in the wild (raw 16-byte base64
  and hex-string-of-16-bytes base64) plus the invalid-length rejection
* binary-data round trip (bytes that contain NULs, high bytes, etc.)
"""

from __future__ import annotations

import base64

import pytest

from miniunicorn.channels.weixin.crypto import (
    decrypt_aes_ecb,
    encrypt_aes_ecb,
    parse_aes_key,
    pkcs7_unpad_safe,
)

# base64("0123456789abcdef") — 16 raw bytes
_RAW_KEY_B64 = "MDEyMzQ1Njc4OWFiY2RlZg=="
# base64(hex string of the same 16 bytes) — 32 ASCII hex chars
_HEX_KEY_B64 = base64.b64encode(b"0123456789abcdef".hex().encode()).decode()


# ---------------------------------------------------------------------------
# parse_aes_key
# ---------------------------------------------------------------------------


def test_parse_aes_key_accepts_raw_16_byte_base64() -> None:
    assert parse_aes_key(_RAW_KEY_B64) == b"0123456789abcdef"


def test_parse_aes_key_accepts_hex_string_of_16_bytes() -> None:
    # base64("30313233343536373839616263646566") — the hex-string form
    assert parse_aes_key(_HEX_KEY_B64) == b"0123456789abcdef"


def test_parse_aes_key_rejects_invalid_key_length() -> None:
    # base64 of 8 raw bytes — neither 16 raw nor 32 hex chars
    short_key = base64.b64encode(b"01234567").decode()
    with pytest.raises(ValueError):
        parse_aes_key(short_key)


def test_parse_aes_key_rejects_non_hex_32_byte_payload() -> None:
    # 32 raw bytes that are not hex characters
    non_hex = base64.b64encode(b"z" * 32).decode()
    with pytest.raises(ValueError):
        parse_aes_key(non_hex)


# ---------------------------------------------------------------------------
# pkcs7_unpad_safe
# ---------------------------------------------------------------------------


def test_pkcs7_unpad_safe_strips_valid_padding() -> None:
    # "hello" + 11 bytes of 0x0b
    padded = b"hello" + bytes([11]) * 11
    assert pkcs7_unpad_safe(padded) == b"hello"


def test_pkcs7_unpad_safe_keeps_data_when_padding_invalid() -> None:
    # Last byte is 0x05 but only the final byte matches — not valid PKCS7.
    bad = b"hello world\x05"
    assert pkcs7_unpad_safe(bad) == bad


def test_pkcs7_unpad_safe_keeps_data_when_pad_len_out_of_range() -> None:
    # pad_len = 0 is invalid (must be 1..16) — returned unchanged.
    assert pkcs7_unpad_safe(b"hello\x00") == b"hello\x00"
    # pad_len = 17 (> block_size) — returned unchanged.
    assert pkcs7_unpad_safe(b"hello" + bytes([17])) == b"hello" + bytes([17])


def test_pkcs7_unpad_safe_keeps_non_block_aligned_data() -> None:
    # Length not a multiple of 16 — returned unchanged.
    assert pkcs7_unpad_safe(b"hello") == b"hello"


def test_pkcs7_unpad_safe_empty_input_returns_empty() -> None:
    assert pkcs7_unpad_safe(b"") == b""


# ---------------------------------------------------------------------------
# encrypt_aes_ecb / decrypt_aes_ecb round trips
# ---------------------------------------------------------------------------


def test_encrypt_decrypt_round_trip_with_raw_key() -> None:
    plaintext = b"hello-weixin-padding"
    ciphertext = encrypt_aes_ecb(plaintext, _RAW_KEY_B64)
    assert ciphertext != plaintext
    assert decrypt_aes_ecb(ciphertext, _RAW_KEY_B64) == plaintext


def test_encrypt_decrypt_round_trip_with_hex_key() -> None:
    plaintext = b"hello-weixin-padding"
    ciphertext = encrypt_aes_ecb(plaintext, _HEX_KEY_B64)
    assert decrypt_aes_ecb(ciphertext, _HEX_KEY_B64) == plaintext


def test_encrypt_aes_ecb_produces_block_aligned_ciphertext() -> None:
    plaintext = b"hello"  # 5 bytes
    ciphertext = encrypt_aes_ecb(plaintext, _RAW_KEY_B64)
    # PKCS7 pads to next 16-byte boundary → ciphertext is a multiple of 16.
    assert len(ciphertext) % 16 == 0
    assert len(ciphertext) == 16  # 5 bytes → one block


def test_encrypt_decrypt_round_trip_binary_data() -> None:
    # Bytes that include NUL, high bytes, and a length that needs padding.
    plaintext = bytes(range(256)) * 3  # 768 bytes
    ciphertext = encrypt_aes_ecb(plaintext, _RAW_KEY_B64)
    assert decrypt_aes_ecb(ciphertext, _RAW_KEY_B64) == plaintext


def test_encrypt_aes_ecb_ciphertext_differs_for_different_inputs() -> None:
    a = encrypt_aes_ecb(b"first payload", _RAW_KEY_B64)
    b = encrypt_aes_ecb(b"second payload", _RAW_KEY_B64)
    assert a != b


def test_decrypt_aes_ecb_returns_raw_data_when_key_invalid() -> None:
    # An invalid key string should not raise from decrypt; it returns the
    # raw ciphertext (matches the channel's fail-open media-decrypt policy).
    ciphertext = encrypt_aes_ecb(b"payload", _RAW_KEY_B64)
    assert decrypt_aes_ecb(ciphertext, "not-valid-base64!!") == ciphertext
