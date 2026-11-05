import io

import pytest
from openpyxl import Workbook, load_workbook

from app import intake


def wb_bytes(rows):
    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def std_rows(of_engine):
    """normalize 결과(표준 형식)를 [(날짜, 번호, 이름, 상품, 옵션, 링크)] 로"""
    ws = load_workbook(io.BytesIO(of_engine)).active
    return [tuple("" if c is None else c for c in r) for r in ws.iter_rows(min_row=3, values_only=True)]  # 빈 칸은 ''로


def test_template_is_standard_and_has_guide():
    data = intake.make_template()
    pv = intake.preview(data)
    assert pv["standard"] and pv["channels"] == 2 and pv["header_row"] == 2
    assert load_workbook(io.BytesIO(data)).sheetnames == ["시딩 리스트", "작성 방법"]


def test_guess_mapping_other_order_and_names():
    data = wb_bytes([["우리 협찬 목록"], ["유튜버", "채널 주소", "제품", "보낸 날짜", "순번"],
                     ["하늘로그", "https://www.youtube.com/@sky", "티셔츠", "2026-05-13", 7]])
    pv = intake.preview(data)
    assert pv["header_row"] == 2 and not pv["standard"]
    assert pv["map"] == {"date": 3, "no": 4, "name": 0, "product": 2, "option": None, "url": 1}
    out = std_rows(intake.normalize(data, pv["header_row"], pv["map"]))
    assert out == [("260513", 7, "하늘로그", "티셔츠", "", "https://www.youtube.com/@sky")]


def test_guess_without_header_finds_url_column():
    data = wb_bytes([["https://www.youtube.com/@a"], ["https://www.youtube.com/@b"]])
    pv = intake.preview(data)
    assert pv["header_row"] == 0 and pv["map"]["url"] == 0 and pv["channels"] == 2
    out = std_rows(intake.normalize(data, 0, pv["map"], "기본상품"))
    assert [(o[1], o[2], o[3]) for o in out] == [(1, "a", "기본상품"), (2, "b", "기본상품")]


def test_normalize_uses_sequential_numbers_when_duplicated_and_skips_blank_rows():
    data = wb_bytes([["이름", "링크", "번호"], ["가", "https://youtube.com/@g", 1], [None, None, None],
                     ["나", "https://youtube.com/@n", 1]])
    pv = intake.preview(data)
    out = std_rows(intake.normalize(data, pv["header_row"], pv["map"]))
    assert [o[1] for o in out] == [1, 2] and [o[2] for o in out] == ["가", "나"]


def test_from_text_variants():
    text = "@handle_a\n하늘 https://www.youtube.com/@sky\nyoutube.com/@plain\n\n 빛나는채널 | https://youtu.be/dQw4w9WgXcQ"
    out = std_rows(intake.from_text(text, "신상"))
    assert [o[5] for o in out] == ["https://www.youtube.com/@handle_a", "https://www.youtube.com/@sky",
                                   "https://youtube.com/@plain".replace("https://", "https://"), "https://youtu.be/dQw4w9WgXcQ"]
    assert [o[2] for o in out] == ["handle_a", "하늘", "plain", "빛나는채널"]
    assert all(o[3] == "신상" for o in out)


def test_from_text_rejects_line_without_address_and_empty():
    with pytest.raises(ValueError, match="주소를 찾지 못한"):
        intake.from_text("그냥 글자만 있는 줄")
    with pytest.raises(ValueError, match="한 곳도"):
        intake.from_text("   \n  ")


def test_channel_limit(monkeypatch):
    monkeypatch.setattr(intake, "MAX_CHANNELS", 2)
    with pytest.raises(ValueError, match="최대 2곳"):
        intake.from_text("@a\n@b\n@c")


def test_yymmdd_formats():
    assert intake._yymmdd("2026-05-13") == "260513" and intake._yymmdd("260513 커플") == "260513"
    assert intake._yymmdd("2026.5.3") == "260503" and intake._yymmdd("메모만") == ""
