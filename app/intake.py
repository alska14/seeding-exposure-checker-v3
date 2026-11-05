"""입력 간소화: 템플릿, 주소 붙여넣기, 열 자동 인식·매핑. 결과는 항상 엔진이 읽는 표준 형식(3행부터, A~F열)으로 바꾼다."""
import io
import re
from datetime import date, datetime
from urllib.parse import unquote

from openpyxl import Workbook, load_workbook

MAX_CHANNELS = 300
HEADER = ["발송일(YYMMDD, 비워도 됨)", "번호", "유튜브 아이디(채널 이름)", "상품명[색상]", "옵션", "채널 링크"]
STD_MAP = {"date": 0, "no": 1, "name": 2, "product": 3, "option": 4, "url": 5}
KEYWORDS = {  # 찾는 순서가 중요하다: '상품 이름'이 name 으로 잡히지 않게 상품·링크를 먼저 찾는다
    "url": ["링크", "url", "주소", "link"],
    "date": ["발송", "날짜", "일자", "date"],
    "no": ["번호", "순번", "no", "#"],
    "option": ["옵션", "option", "사이즈", "size"],
    "product": ["상품", "제품", "product", "item"],
    "name": ["아이디", "유튜버", "채널명", "채널 이름", "이름", "name"],
}
URL_RE = re.compile(r"(https?://\S+|(?:www\.)?youtube\.com/\S+|youtu\.be/\S+|@[\w.\-가-힣]+)")


def make_template() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "시딩 리스트"
    ws.append(["시딩 리스트 (예시 — 지우고 내 데이터로 채우세요)"])
    ws.append(HEADER)
    ws.append(["260513", 1, "예시 채널 A", "예시 상품[블랙]", "L", "https://www.youtube.com/@example_a"])
    ws.append(["260514", 2, "예시 채널 B", "예시 상품[화이트]", "M", "https://www.youtube.com/@example_b"])
    for col, w in zip("ABCDEF", (22, 8, 26, 22, 8, 44)):
        ws.column_dimensions[col].width = w
    guide = wb.create_sheet("작성 방법")
    for line in ["3행부터 한 줄에 유튜버 한 명을 적습니다.",
                 "채널 링크는 https://www.youtube.com/@핸들 형태를 권장합니다. 영상·쇼츠 링크도 되고, 영상에서 채널을 찾습니다.",
                 "발송일(A열)은 260513처럼 연월일 6자리입니다. 비워두면 화면에서 정한 시작일부터 찾습니다.",
                 "열 순서가 다른 엑셀은 그대로 올려도 됩니다. 화면에서 열을 골라 연결할 수 있습니다.",
                 "한 유튜버당 한 줄로 적어주세요. 상품이 여러 개면 상품 칸에 ' / '로 이어 적습니다."]:
        guide.append([line])
    guide.column_dimensions["A"].width = 110
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _cell(v):
    if v is None:
        return ""
    if isinstance(v, (datetime, date)):
        return v.strftime("%Y-%m-%d")
    return " ".join(str(v).split())


def read_rows(data: bytes, limit=None):
    ws = load_workbook(io.BytesIO(data), read_only=True, data_only=True).active
    rows = []
    for i, r in enumerate(ws.iter_rows(values_only=True)):
        rows.append([_cell(x) for x in r])
        if limit and i + 1 >= limit:
            break
    return rows


def guess_mapping(rows):
    """첫 10줄에서 머리글 줄을 찾아 열 이름으로 맞춘다. 못 찾으면 링크가 들어 있는 열만 추정한다."""
    for hi, row in enumerate(rows[:10]):
        used, mapping = set(), {}
        for field, words in KEYWORDS.items():
            for ci, cell in enumerate(row):
                if ci in used or not cell:
                    continue
                low = cell.lower()
                if any(w in low for w in words):
                    mapping[field] = ci
                    used.add(ci)
                    break
        if len(mapping) >= 2 and ("url" in mapping or "name" in mapping):
            return hi + 1, {k: mapping.get(k) for k in STD_MAP}
    mapping = {k: None for k in STD_MAP}
    for ci in range(max((len(r) for r in rows[:10]), default=0)):
        cells = [r[ci] for r in rows[:10] if ci < len(r) and r[ci]]
        if cells and sum(1 for c in cells if re.search(r"youtube\.com|youtu\.be|^@", c)) >= max(1, len(cells) // 2):
            mapping["url"] = ci
            break
    return 0, mapping


def preview(data: bytes):
    rows = read_rows(data, limit=40)
    header_row, mapping = guess_mapping(rows)
    n = _count(rows, header_row, mapping)
    return {"rows": [[c[:40] for c in r] for r in rows[:6]], "ncols": max((len(r) for r in rows[:6]), default=0),
            "header_row": header_row, "map": mapping, "channels": n,
            "standard": header_row == 2 and mapping == STD_MAP}


def _count(rows, header_row, mapping):
    n = 0
    for r in rows[header_row:]:
        if _get(r, mapping.get("url")) or _get(r, mapping.get("name")):
            n += 1
    return n


def _get(row, ci):
    return row[ci] if ci is not None and ci < len(row) else ""


def _yymmdd(v):
    v = (v or "").strip()
    m = re.match(r"^(\d{4})[-./](\d{1,2})[-./](\d{1,2})", v)
    if m:
        return f"{int(m.group(1)) % 100:02d}{int(m.group(2)):02d}{int(m.group(3)):02d}"
    m = re.match(r"^(\d{6})", v)
    return m.group(1) if m else ""


def _name_from_url(u):
    u = unquote(u)
    m = re.search(r"@([^/?#\s]+)", u)
    return m.group(1) if m else re.sub(r"^https?://(www\.)?", "", u).split("/")[0] or "채널"


def _build(items):
    """items = [(date, no, name, product, option, url)] -> 표준 형식 xlsx"""
    if not items:
        raise ValueError("채널을 한 곳도 찾지 못했습니다. 링크 열을 확인하세요")
    if len(items) > MAX_CHANNELS:
        raise ValueError(f"채널은 한 작업에 최대 {MAX_CHANNELS}곳입니다 (지금 {len(items)}곳)")
    wb = Workbook()
    ws = wb.active
    ws.append(["입력 정리본"])
    ws.append(HEADER)
    for it in items:
        ws.append(list(it))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def normalize(data: bytes, header_row, mapping, default_product="상품"):
    """다른 열 순서의 엑셀을 표준 형식으로. 한 유튜버당 한 줄로 읽는다."""
    rows = read_rows(data)
    items, nums = [], []
    for r in rows[header_row:]:
        url, name = _get(r, mapping.get("url")), _get(r, mapping.get("name"))
        if not url and not name:
            continue
        if url and not name:
            name = _name_from_url(url)
        no = _get(r, mapping.get("no"))
        nums.append(int(no) if re.fullmatch(r"\d+", no or "") else None)
        items.append([_yymmdd(_get(r, mapping.get("date"))), None, name, _get(r, mapping.get("product")) or default_product,
                      _get(r, mapping.get("option")), url])
    unique = all(n is not None for n in nums) and len(set(nums)) == len(nums)
    for i, it in enumerate(items):
        it[1] = nums[i] if unique else i + 1
    return _build([tuple(i) for i in items])


def from_text(text: str, default_product="상품"):
    """줄마다 '채널 주소' 또는 '이름 주소' 또는 @핸들. 줄 수가 곧 채널 수."""
    items = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        m = URL_RE.search(line)
        if not m:
            raise ValueError(f"주소를 찾지 못한 줄이 있습니다: {line[:40]}")
        raw = m.group(1).rstrip(",;")
        url = f"https://www.youtube.com/{raw}" if raw.startswith("@") else (raw if raw.startswith("http") else "https://" + raw)
        name = (line[:m.start()] + " " + line[m.end():]).strip(" ,;-|\t") or _name_from_url(url)
        items.append(("", len(items) + 1, name[:60], default_product, "", url))
    return _build(items)
