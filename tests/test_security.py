import io

import pytest
from openpyxl import Workbook

from app import security


def test_redact_known_and_patterns():
    s = "fail https://a/b?part=x&key=AIzaSyABCDEFGHIJKLMNOPQRSTUV&z=1 sk-proj-ABCDEFGHIJKLMNOP mykeyvalue123"
    out = security.redact(s, ["mykeyvalue123"])
    assert "AIza" not in out and "sk-proj" not in out and "mykeyvalue123" not in out
    assert "key=***" in out


def test_clean_key():
    assert security.clean_key("") is None and security.clean_key(None) is None
    assert security.clean_key("a" * 30) == "a" * 30
    for bad in ["short", "a" * 20 + " " + "b" * 5, "a" * 20 + "\nX-Evil: 1"]:
        with pytest.raises(ValueError):
            security.clean_key(bad)


def test_token_hash_and_uniqueness():
    a, b = security.new_token(), security.new_token()
    assert a != b and len(a) >= 40
    assert security.hash_token(a) != a and security.hash_token(a) == security.hash_token(a)


def test_image_ext_by_content():
    assert security.image_ext(b"\x89PNG\r\n\x1a\n" + b"0" * 10) == ".png"
    assert security.image_ext(b"\xff\xd8\xff\xe0abc") == ".jpg"
    assert security.image_ext(b"RIFF1234WEBPVP8 ") == ".webp"
    assert security.image_ext(b"<svg onload=alert(1)>") is None
    assert security.image_ext(b"\x89PNG\r\n\x1a\n" + b"0" * (security.MAX_IMG + 1)) is None


def test_check_xlsx():
    wb = Workbook()
    buf = io.BytesIO()
    wb.save(buf)
    security.check_xlsx(buf.getvalue())
    with pytest.raises(ValueError):
        security.check_xlsx(b"not a zip")
    with pytest.raises(ValueError):
        security.check_xlsx(b"PK" + b"x" * 100)


def test_fail_limiter():
    lim = security.FailLimiter(limit=3, window=60)
    for _ in range(3):
        assert not lim.blocked("ip")
        lim.fail("ip")
    assert lim.blocked("ip") and not lim.blocked("other")
