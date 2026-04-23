"""Tests for content hashing utilities."""

from framework.hashing import content_changed, sha256_hex


def test_sha256_hex_is_deterministic() -> None:
    assert sha256_hex(b"hello") == sha256_hex(b"hello")


def test_sha256_hex_differs_for_different_content() -> None:
    assert sha256_hex(b"hello") != sha256_hex(b"world")


def test_sha256_hex_known_value() -> None:
    # echo -n "abc" | sha256sum
    assert sha256_hex(b"abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_content_changed_returns_true_when_different() -> None:
    old_hash = sha256_hex(b"old content")
    assert content_changed(old_hash, b"new content") is True


def test_content_changed_returns_false_when_same() -> None:
    content = b"same content"
    old_hash = sha256_hex(content)
    assert content_changed(old_hash, content) is False
