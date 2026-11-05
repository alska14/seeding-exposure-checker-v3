"""업종 프리셋: 처음 쓰는 사람을 위한 임시값. 모두 화면에서 고칠 수 있다.

verified=True 는 실제 데이터로 검증된 업종이다(현재 패션만). 나머지는 제목·설명란·댓글·링크 같은 텍스트 점검은 쓸 수 있지만,
영상 분석 프롬프트가 '옷을 입었는가' 기준이라 정확도를 검증하지 못했다.
"""

PRESETS = {
    "fashion": {
        "label": "패션·의류", "verified": True,
        "title_keywords": ["룩북", "코디", "착장", "ootd", "하울", "언박싱", "데일리룩", "패션", "grwm", "lookbook", "haul", "outfit"],
        "product_example": "예: 풋볼져지, 크롭티, 버뮤다쇼츠",
        "look_for": "착용한 옷의 프린트·로고·색", "window_days": 180,
    },
    "beauty": {
        "label": "뷰티·화장품", "verified": False,
        "title_keywords": ["리뷰", "데일리 메이크업", "하울", "파우치", "루틴", "추천템", "grwm", "get ready with me", "review"],
        "product_example": "예: 쿠션, 립틴트, 선크림",
        "look_for": "사용하는 제품 용기·패키지·로고", "window_days": 180,
    },
    "food": {
        "label": "식품·음료", "verified": False,
        "title_keywords": ["먹방", "리뷰", "맛집", "레시피", "편의점", "언박싱", "mukbang", "recipe", "review"],
        "product_example": "예: 신제품 이름, 맛 이름",
        "look_for": "먹거나 보여주는 제품 포장·로고", "window_days": 120,
    },
    "tech": {
        "label": "전자기기·IT", "verified": False,
        "title_keywords": ["리뷰", "언박싱", "사용기", "비교", "세팅", "데스크", "review", "unboxing", "setup"],
        "product_example": "예: 모델명, 제품 시리즈",
        "look_for": "기기 외형·로고·화면", "window_days": 180,
    },
    "game_app": {
        "label": "게임·앱", "verified": False,
        "title_keywords": ["플레이", "후기", "공략", "추천", "체험", "리뷰", "gameplay", "review"],
        "product_example": "예: 게임·앱 이름, 캐릭터 이름",
        "look_for": "화면 속 앱·게임 UI·로고", "window_days": 90,
    },
    "travel": {
        "label": "여행·숙박", "verified": False,
        "title_keywords": ["브이로그", "여행", "숙소", "호텔", "후기", "vlog", "travel", "hotel"],
        "product_example": "예: 숙소 이름, 지역명",
        "look_for": "숙소·장소 간판·로고", "window_days": 180,
    },
    "living": {
        "label": "생활·인테리어", "verified": False,
        "title_keywords": ["룸투어", "인테리어", "집들이", "추천템", "언박싱", "room tour", "haul"],
        "product_example": "예: 가구·소품 이름",
        "look_for": "집 안 제품·가구 외형·로고", "window_days": 180,
    },
    "other": {
        "label": "기타 (직접 입력)", "verified": False,
        "title_keywords": ["리뷰", "후기", "언박싱", "추천", "review", "unboxing"],
        "product_example": "제품·서비스 이름을 쉼표로",
        "look_for": "제품 외형·로고", "window_days": 180,
    },
}

DEFAULT_PRESET = "fashion"


def public_list():
    return [{"id": k, **v} for k, v in PRESETS.items()]


def get(preset_id):
    return PRESETS.get(preset_id) or PRESETS[DEFAULT_PRESET]
