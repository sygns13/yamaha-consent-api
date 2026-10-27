import hashlib

from app.utils.tokens import generate_link_token, hash_token


def test_generate_link_token_returns_url_safe_token_and_its_sha256() -> None:
    token, token_hash = generate_link_token()
    assert len(token) >= 43  # 32 bytes en base64 url-safe
    assert set(token) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
    assert token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert len(token_hash) == 64


def test_tokens_are_unique() -> None:
    assert len({generate_link_token()[0] for _ in range(1000)}) == 1000


def test_hash_token_is_deterministic() -> None:
    assert hash_token("abc") == hash_token("abc")
    assert hash_token("abc") != hash_token("abd")
