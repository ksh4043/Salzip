"""실거래가 조회와 전세가율 계산 도구 (담당 B).

- search_price: 국토교통부 실거래가 API로 주변 매매·전세 실거래가를 조회해 시세를 낸다
- calc_jeonse_ratio: 보증금 ÷ 매매 시세로 전세가율과 위험 등급을 계산한다

두 도구를 나눈 이유: LLM이 시세를 먼저 보고(거래가 없으면 기간을 늘려 다시 조회하는 등)
다음 행동을 스스로 고르게 하기 위해서다. (AGENTS.md 1장 "LLM이 다음 행동을 고른다")

금액은 모두 원 단위 정수다. 국토부 API는 만원 단위로 주므로 받자마자 원으로 바꾼다.
"""

from __future__ import annotations

import logging
import math
import os
import re
import statistics
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.parse import unquote

import requests

logger = logging.getLogger(__name__)


class _RedactServiceKeyFilter(logging.Filter):
    """로그에서 인증키를 가린다 (AGENTS.md 6장: 로그에 API 키를 남기지 않는다).

    공공데이터포털은 키를 URL 쿼리(serviceKey=...)로만 받는다. 앱이 로그 수준을 DEBUG로
    두면 urllib3가 요청 URL 전체를 로그에 남기므로, 그 기록에서 키 부분만 지운다.
    """

    _PATTERN = re.compile(r"(serviceKey=)[^&\s\"']+", re.IGNORECASE)

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if "servicekey=" in message.lower():
            record.msg = self._PATTERN.sub(r"\1***", message)
            record.args = None
        return True


logging.getLogger("urllib3.connectionpool").addFilter(_RedactServiceKeyFilter())

# --------------------------------------------------------------------------- #
# 국토교통부 실거래가 API (AGENTS.md 2장 표와 같이 고친다)
# --------------------------------------------------------------------------- #
MOLIT_BASE_URL = "https://apis.data.go.kr/1613000"
# 주택 유형 → (매매 오퍼레이션 경로, 전월세 오퍼레이션 경로)
# 2026-11-24 공용 인증키로 4개 모두 호출해 정상 응답과 태그 이름을 확인했다.
MOLIT_ENDPOINTS: dict[str, tuple[str, str]] = {
    "아파트": (
        "RTMSDataSvcAptTradeDev/getRTMSDataSvcAptTradeDev",
        "RTMSDataSvcAptRent/getRTMSDataSvcAptRent",
    ),
    "연립다세대": (
        "RTMSDataSvcRHTrade2/getRTMSDataSvcRHTrade2",
        "RTMSDataSvcRHRent/getRTMSDataSvcRHRent",
    ),
}
HOUSING_TYPE_ALIASES: dict[str, str] = {
    "아파트": "아파트",
    "apt": "아파트",
    "연립다세대": "연립다세대",
    "연립": "연립다세대",
    "다세대": "연립다세대",
    "빌라": "연립다세대",
}
# 팀 합의로 범위에서 뺀 주택 유형. API를 부르지 않고 이유와 다음 행동을 바로 알려 준다.
# 오피스텔: 공용 인증키에 오피스텔 매매 API가 없어 전세가율을 계산할 수 없으므로 제외했다(2026-11-24).
UNSUPPORTED_HOUSING_TYPES: dict[str, str] = {
    "오피스텔": "오피스텔은 이 도구가 지원하지 않습니다(아파트·연립다세대만 지원). "
    "오피스텔의 매매 시세는 사용자에게 직접 확인하도록 안내하세요.",
}

WON_PER_MANWON = 10_000  # API 금액 단위(만원) → 원
REQUEST_TIMEOUT_SEC = 10
MAX_RETRIES = 2  # 타임아웃·5xx일 때 추가로 시도하는 횟수
RETRY_BACKOFF_SEC = 0.5  # n번째 재시도 전 대기 = 0.5 × 2^(n-1)초
MAX_WORKERS = 6  # 월별 호출을 동시에 보내는 스레드 수
NUM_OF_ROWS = 1000  # 한 페이지 최대 건수
MAX_PAGES = 200
DEFAULT_MONTHS = 6
MAX_MONTHS = 24

# 공공데이터포털 공통 오류 코드 (응답 XML의 returnReasonCode / resultCode)
_OK_CODES = {"00", "000"}
_NO_DATA_CODES = {"03"}
_AUTH_CODES = {"20", "30", "31", "32"}
_LIMIT_CODES = {"22"}
_AUTH_MESSAGES = (
    "SERVICE_KEY_IS_NULL",
    "SERVICE_KEY_IS_NOT_REGISTERED_ERROR",
    "DEADLINE_HAS_EXPIRED_ERROR",
    "SERVICE_ACCESS_DENIED_ERROR",
    "UNREGISTERED_IP_ERROR",
)

# 응답 태그 이름: 현행(영문)과 구버전(한글)을 모두 받는다.
_SALE_PRICE_TAGS = ("dealAmount", "거래금액")
_DEPOSIT_TAGS = ("deposit", "보증금액", "보증금")
_MONTHLY_RENT_TAGS = ("monthlyRent", "월세금액", "월세")
_BUILDING_NAME_TAGS = ("aptNm", "mhouseNm", "아파트", "연립다세대", "단지")

# --------------------------------------------------------------------------- #
# 시세 산출 기준
# --------------------------------------------------------------------------- #
MIN_SAMPLES = 3  # 이보다 적으면 다음(더 넓은) 표본 조건으로 넘어간다
AREA_TOLERANCE_NARROW_M2 = 5.0
AREA_TOLERANCE_WIDE_M2 = 10.0
OUTLIER_IQR_MULTIPLIER = 1.5  # 사분위 범위의 1.5배 밖을 급매·특수거래로 보고 뺀다
OUTLIER_MIN_SAMPLES = 5  # 표본이 이보다 적으면 이상치를 빼지 않는다
RECENT_DEALS_LIMIT = 5
SUMMARY_MAX_CHARS = 200  # AGENTS.md 3장: summary는 200자를 넘기지 않는다

# --------------------------------------------------------------------------- #
# 전세가율 위험 기준 (팀 합의: 70% 미만 안전, 70% 이상~80% 이하 주의, 80% 초과 위험)
# 근거: 70%·80% 두 경계는 HUG 전세보증금반환보증 보증료율표의 부채비율 구간
# ("70% 이하 / 70% 초과~80% 이하 / 80% 초과", data/docs/hug_guarantee_overview.pdf,
# tools/law.py RATE_LABELS)에서 가져왔다. 부채비율은 선순위 채권까지 더한 값이라
# 전세가율과 같지는 않으므로 경계값만 빌려 쓴 것이다. 70% 정각은 팀 합의에 따라 주의로 본다.
# 보고서의 기준선(threshold)은 80%다(AGENTS.md 3장 최종 보고서 형식).
# --------------------------------------------------------------------------- #
JEONSE_RATIO_THRESHOLD_PCT = 80.0  # 이 값을 넘으면 위험
JEONSE_RATIO_CAUTION_PCT = 70.0  # 이 값 이상이면 주의
RISK_DANGER = "위험"
RISK_CAUTION = "주의"
RISK_SAFE = "안전"
RISK_NOTES = {
    RISK_DANGER: "재검토 권고",
    RISK_CAUTION: "추가 확인 필요",
    RISK_SAFE: "기준선 이내",
}
# 원 단위가 아닌 만원 단위로 잘못 넣은 경우를 잡기 위한 값.
# 보증금은 월세 보증금(수십만 원)도 들어오므로 하한을 낮게, 매매 시세는 높게 둔다.
MIN_PLAUSIBLE_DEPOSIT_WON = 100_000  # 10만 원
MIN_PLAUSIBLE_MARKET_PRICE_WON = 5_000_000  # 500만 원
UNIT_SUSPECT_RATIO_PCT = 300.0  # 이보다 높으면 단위 실수를 의심한다
UNIT_SUSPECT_LOW_RATIO_PCT = 1.0  # 이보다 낮으면 보증금만 만원 단위로 넣었을 수 있다
FULL_LOSS_RATIO_PCT = 100.0  # 보증금이 매매 시세 이상

# --------------------------------------------------------------------------- #
# 법정동코드 앞 5자리(LAWD_CD). 서울·인천·경기만 넣었다. 다른 지역은 register_lawd_codes()로 추가한다.
# 2026-11-24 실제 API 응답의 시군구명(sggNm)·법정동(umdNm)으로 모두 대조했다.
# 2026년 행정구역 개편 반영: 인천 중구·동구 → 제물포구·영종구, 인천 서구 → 서해구·검단구,
# 화성시 → 만세구·효행구·병점구·동탄구. 옛 코드(28110, 28140, 28260, 41590)는 거래가 0건이다.
# --------------------------------------------------------------------------- #
LAWD_CODES: dict[str, dict[str, str]] = {
    "서울": {
        "종로구": "11110", "중구": "11140", "용산구": "11170", "성동구": "11200",
        "광진구": "11215", "동대문구": "11230", "중랑구": "11260", "성북구": "11290",
        "강북구": "11305", "도봉구": "11320", "노원구": "11350", "은평구": "11380",
        "서대문구": "11410", "마포구": "11440", "양천구": "11470", "강서구": "11500",
        "구로구": "11530", "금천구": "11545", "영등포구": "11560", "동작구": "11590",
        "관악구": "11620", "서초구": "11650", "강남구": "11680", "송파구": "11710",
        "강동구": "11740",
    },
    "인천": {
        "제물포구": "28125", "영종구": "28155", "미추홀구": "28177", "연수구": "28185",
        "남동구": "28200", "부평구": "28237", "계양구": "28245", "서해구": "28275",
        "검단구": "28290", "강화군": "28710", "옹진군": "28720",
    },
    "경기": {
        "수원시 장안구": "41111", "수원시 권선구": "41113", "수원시 팔달구": "41115",
        "수원시 영통구": "41117",
        "성남시 수정구": "41131", "성남시 중원구": "41133", "성남시 분당구": "41135",
        "의정부시": "41150",
        "안양시 만안구": "41171", "안양시 동안구": "41173",
        "부천시 원미구": "41192", "부천시 소사구": "41194", "부천시 오정구": "41196",
        "광명시": "41210", "평택시": "41220", "동두천시": "41250",
        "안산시 상록구": "41271", "안산시 단원구": "41273",
        "고양시 덕양구": "41281", "고양시 일산동구": "41285", "고양시 일산서구": "41287",
        "과천시": "41290", "구리시": "41310", "남양주시": "41360", "오산시": "41370",
        "시흥시": "41390", "군포시": "41410", "의왕시": "41430", "하남시": "41450",
        "용인시 처인구": "41461", "용인시 기흥구": "41463", "용인시 수지구": "41465",
        "파주시": "41480", "이천시": "41500", "안성시": "41550", "김포시": "41570",
        "화성시 만세구": "41591", "화성시 효행구": "41593", "화성시 병점구": "41595",
        "화성시 동탄구": "41597", "광주시": "41610", "양주시": "41630", "포천시": "41650",
        "여주시": "41670", "연천군": "41800", "가평군": "41820", "양평군": "41830",
    },
}
# 2026년 개편으로 없어진 구 → 바뀐 구. 사용자가 옛 이름으로 넣으면 새 이름을 알려 준다.
# (옛 인천 동구 지역은 제물포구 코드에서, 옛 중구 영종도 지역은 영종구 코드에서 거래가 나온다.)
RENAMED_SIGUNGU: dict[tuple[str, str], str] = {
    ("인천", "중구"): "제물포구(내륙) 또는 영종구(영종도)",
    ("인천", "동구"): "제물포구",
    ("인천", "서구"): "서해구 또는 검단구",
}
SIDO_ALIASES: dict[str, str] = {
    "서울": "서울", "서울시": "서울", "서울특별시": "서울",
    "인천": "인천", "인천시": "인천", "인천광역시": "인천",
    "경기": "경기", "경기도": "경기",
}
# 코드표에 없는 시도. "광주 서구"를 인천 서구로 잘못 잡지 않으려고 둔다.
_UNSUPPORTED_SIDO_PREFIXES = (
    "부산", "대구", "광주광역시", "대전", "울산", "세종", "강원", "충북", "충청북도",
    "충남", "충청남도", "전북", "전라북도", "전남", "전라남도", "경북", "경상북도",
    "경남", "경상남도", "제주",
)
_DONG_PATTERN = re.compile(r"^[가-힣]+\d*(?:동|가|읍|면|리)$")
_NOT_DONG_WORDS = ("상가", "가동")


# --------------------------------------------------------------------------- #
# 내부 자료형과 오류
# --------------------------------------------------------------------------- #
class _ToolError(Exception):
    """도구 안에서만 쓰는 예외. 메시지는 그대로 LLM에 실패 사유로 전달된다."""


class _AuthError(_ToolError):
    """인증키 문제. 재시도해도 소용없으므로 즉시 멈춘다."""


@dataclass(frozen=True)
class _Region:
    lawd_cd: str
    sido: str
    sigungu: str
    dong: str | None


@dataclass(frozen=True)
class _Deal:
    deal_type: str  # "매매" | "전세"
    price_won: int  # 매매는 거래금액, 전세는 보증금
    deal_date: date | None
    dong: str | None
    building_name: str | None
    area_m2: float | None
    floor: int | None
    build_year: int | None


@dataclass(frozen=True)
class _Target:
    dong: str | None
    area_m2: float | None
    building_name: str | None


# --------------------------------------------------------------------------- #
# 주소 → 법정동코드
# --------------------------------------------------------------------------- #
def register_lawd_codes(sido: str, codes: dict[str, str], *, aliases: tuple[str, ...] = ()) -> None:
    """법정동코드표에 시도·시군구를 추가한다. aliases는 입력에 쓰일 시도 표기."""
    for name, code in codes.items():
        if not (len(code) == 5 and code.isdigit()):
            raise ValueError(f"LAWD_CD는 5자리 숫자여야 합니다: {name}={code!r}")
    LAWD_CODES.setdefault(sido, {}).update(codes)
    SIDO_ALIASES[sido] = sido
    for alias in aliases:
        SIDO_ALIASES[alias] = sido


def _tokenize(text: str) -> list[str]:
    return re.sub(r"[(),]", " ", text).split()


def _extract_dong(region: str) -> str | None:
    """읍/면/동 이름을 찾는다. 행정동('역삼1동')은 법정동('역삼동')으로 바꾼다."""
    for token in _tokenize(region):
        if (
            token in SIDO_ALIASES
            or token in _NOT_DONG_WORDS
            or token.endswith(("시", "군", "구", "도"))
        ):
            continue
        if _DONG_PATTERN.match(token):
            # 실거래 데이터는 법정동 기준이다. '을지로1가', '성수동1가'는 그대로 둔다.
            return re.sub(r"\d+동$", "동", token)
    return None


def _match_sigungu(tokens: list[str], table: dict[str, str]) -> list[str]:
    """일치하는 시군구 키. 시와 구가 모두 맞는 것을 우선한다."""
    exact: list[str] = []
    gu_only: list[str] = []
    city_only: list[str] = []
    for key in table:
        parts = key.split()
        if len(parts) == 1:
            name = parts[0]
            if name in tokens or (len(name) > 2 and name[:-1] in tokens):
                exact.append(key)
            continue
        city, gu = parts
        has_city = city in tokens or city[:-1] in tokens
        has_gu = gu in tokens
        if has_city and has_gu:
            exact.append(key)
        elif has_gu:
            gu_only.append(key)
        elif has_city:
            city_only.append(key)
    return exact or gu_only or city_only


def _resolve_region(region: str) -> _Region:
    tokens = _tokenize(region)
    if not tokens:
        raise _ToolError("지역이 비어 있습니다. '서울 강남구 역삼동'처럼 시/도·시/군/구·동을 넣어 주세요.")

    sido = next((SIDO_ALIASES[t] for t in tokens if t in SIDO_ALIASES), None)
    # "광주 서구"는 광주광역시다. 경기 광주시에는 구가 없으므로 뒤에 구가 오면 광주광역시로 본다.
    gwangju_metro = tokens[0] == "광주" and len(tokens) > 1 and tokens[1].endswith("구")
    if sido is None and (gwangju_metro or tokens[0].startswith(_UNSUPPORTED_SIDO_PREFIXES)):
        raise _ToolError(
            f"'{tokens[0]}' 지역은 아직 지원하지 않습니다(지원: {', '.join(LAWD_CODES)}). "
            "지원 지역으로 다시 조회하거나 사용자에게 시세를 직접 확인하도록 안내하세요."
        )

    candidates: list[tuple[str, str]] = []
    for s in [sido] if sido else list(LAWD_CODES):
        candidates += [(s, key) for key in _match_sigungu(tokens, LAWD_CODES[s])]

    renamed = [
        (s, gu, new) for (s, gu), new in RENAMED_SIGUNGU.items() if gu in tokens and sido in (None, s)
    ]
    if renamed:
        s, gu, new = renamed[0]
        if candidates:  # 시도 없이 "중구"만 온 경우: 서울 중구와 옛 인천 중구가 겹친다
            names = ", ".join(f"{cs} {ck}" for cs, ck in candidates)
            raise _ToolError(
                f"지역이 여러 곳과 일치합니다({names}, 또는 지금은 {new}인 옛 {s} {gu}). "
                "시/도와 구까지 넣어 다시 조회하세요."
            )
        raise _ToolError(
            f"{s} {gu}는 2026년 행정구역 개편으로 {new}(으)로 바뀌었습니다. 새 구 이름으로 다시 조회하세요."
        )

    if not candidates:
        raise _ToolError(
            "지역에서 시/군/구를 찾지 못했습니다. '서울 강남구 역삼동'처럼 시/군/구를 포함해 다시 조회하세요."
        )
    if len(candidates) > 1:
        names = ", ".join(f"{s} {k}" for s, k in candidates)
        raise _ToolError(
            f"지역이 여러 곳과 일치합니다({names}). 시/도와 구까지 넣어 다시 조회하세요."
        )

    s, key = candidates[0]
    return _Region(lawd_cd=LAWD_CODES[s][key], sido=s, sigungu=key, dong=_extract_dong(region))


def _recent_deal_ymds(months: int, today: date | None = None) -> list[str]:
    """이번 달을 포함한 최근 months개월의 계약년월(YYYYMM), 최신순."""
    today = today or date.today()
    year, month = today.year, today.month
    result: list[str] = []
    for _ in range(months):
        result.append(f"{year:04d}{month:02d}")
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    return result


# --------------------------------------------------------------------------- #
# API 호출과 XML 정제
# --------------------------------------------------------------------------- #
def _load_api_key() -> str:
    """MOLIT_API_KEY를 디코딩된 원문으로 읽는다.

    공공데이터포털은 Encoding/Decoding 키를 둘 다 준다. 인코딩된 키를 requests의
    params에 넣으면 '%'가 한 번 더 인코딩되어 인증에 실패하므로, 더 풀 것이 없을 때까지 푼다.
    """
    key = (os.getenv("MOLIT_API_KEY") or "").strip()
    if not key:
        raise _AuthError(
            "국토부 API 인증키(MOLIT_API_KEY)가 설정되지 않아 시세를 조회할 수 없습니다. "
            "사용자에게 시세를 직접 확인하도록 안내하세요."
        )
    for _ in range(3):
        decoded = unquote(key)
        if decoded == key:
            break
        key = decoded
    return key


def _parse_int(raw: str | None) -> int | None:
    """' 85,000 ' → 85000. 비었거나 숫자가 아니면 None."""
    if raw is None:
        return None
    cleaned = raw.replace(",", "").strip()
    if not cleaned:
        return None
    try:
        return int(float(cleaned))
    except ValueError:
        return None


def _parse_float(raw: str | None) -> float | None:
    if raw is None or not raw.strip():
        return None
    try:
        return float(raw.replace(",", "").strip())
    except ValueError:
        return None


def _first_text(row: dict[str, str], tags: tuple[str, ...]) -> str | None:
    for tag in tags:
        value = row.get(tag)
        if value is not None and value.strip():
            return value.strip()
    return None


def _raise_api_error(code: str, message: str) -> None:
    text = f"국토부 API 오류 [{code or '?'}] {message}"
    if code in _LIMIT_CODES or "LIMITED_NUMBER_OF_SERVICE_REQUESTS" in message:
        raise _ToolError(f"{text}: 일일 호출 한도를 넘었습니다. 오늘은 시세 조회가 어렵습니다.")
    if code in _AUTH_CODES or any(m in message for m in _AUTH_MESSAGES):
        raise _AuthError(
            f"{text}: 인증키가 없거나 등록되지 않았습니다. 공공데이터포털 활용신청 상태와 "
            "MOLIT_API_KEY를 확인해야 하며, 다시 조회해도 같은 결과가 나옵니다."
        )
    raise _ToolError(text)


def _parse_response(xml_text: str) -> tuple[list[dict[str, str]], int]:
    """XML 응답 → (거래 행 목록, 전체 건수). 오류 응답이면 예외를 던진다."""
    try:
        root = ET.fromstring(xml_text.strip())
    except ET.ParseError as exc:
        raise _ToolError("국토부 API가 XML이 아닌 응답을 보냈습니다. 잠시 뒤 다시 조회하세요.") from exc

    # 게이트웨이 오류: <OpenAPI_ServiceResponse><cmmMsgHeader>
    # 실제 응답은 errMsg에 코드명, returnAuthMsg에 한글 설명이 온다.
    reason = (root.findtext(".//returnReasonCode") or "").strip()
    auth_msg = root.findtext(".//returnAuthMsg")
    if reason or auth_msg is not None:
        parts = [(root.findtext(".//errMsg") or "").strip(), (auth_msg or "").strip()]
        _raise_api_error(reason, " / ".join(p for p in parts if p) or "SERVICE ERROR")

    code = (root.findtext(".//header/resultCode") or "").strip()
    message = (root.findtext(".//header/resultMsg") or "").strip()
    if code in _NO_DATA_CODES:
        return [], 0
    if code and code not in _OK_CODES:
        _raise_api_error(code, message)

    rows = [
        {child.tag: (child.text or "") for child in item}
        for item in root.iterfind(".//items/item")
    ]
    total = _parse_int(root.findtext(".//body/totalCount")) or len(rows)
    return rows, total


def _to_deal(row: dict[str, str], deal_type: str) -> _Deal | None:
    """거래 행 → _Deal. 해제된 거래, 월세, 금액이 없는 행은 None."""
    if (_first_text(row, ("cdealType", "해제여부")) or "").upper() == "O":
        return None

    if deal_type == "매매":
        price_manwon = _parse_int(_first_text(row, _SALE_PRICE_TAGS))
    else:
        monthly_rent = _parse_int(_first_text(row, _MONTHLY_RENT_TAGS)) or 0
        if monthly_rent > 0:
            return None  # 월세·반전세는 전세 시세에서 뺀다
        price_manwon = _parse_int(_first_text(row, _DEPOSIT_TAGS))
    if not price_manwon or price_manwon <= 0:
        return None

    year = _parse_int(_first_text(row, ("dealYear", "년")))
    month = _parse_int(_first_text(row, ("dealMonth", "월")))
    day = _parse_int(_first_text(row, ("dealDay", "일")))
    try:
        deal_date = date(year, month, day) if year and month and day else None
    except ValueError:
        deal_date = None

    return _Deal(
        deal_type=deal_type,
        price_won=price_manwon * WON_PER_MANWON,
        deal_date=deal_date,
        dong=_first_text(row, ("umdNm", "법정동")),
        building_name=_first_text(row, _BUILDING_NAME_TAGS),
        area_m2=_parse_float(_first_text(row, ("excluUseAr", "전용면적"))),
        floor=_parse_int(_first_text(row, ("floor", "층"))),
        build_year=_parse_int(_first_text(row, ("buildYear", "건축년도"))),
    )


def _get_page(path: str, params: dict[str, Any]) -> str:
    """한 페이지를 받는다. 타임아웃·연결 오류·5xx는 재시도한다."""
    url = f"{MOLIT_BASE_URL}/{path}"
    last_error = "알 수 없는 오류"
    for attempt in range(MAX_RETRIES + 1):
        if attempt:
            time.sleep(RETRY_BACKOFF_SEC * 2 ** (attempt - 1))
        try:
            response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT_SEC)
        except requests.Timeout:
            last_error = "응답 시간 초과"
            continue
        except requests.RequestException as exc:
            last_error = f"네트워크 오류({type(exc).__name__})"
            continue
        if response.status_code >= 500:
            last_error = f"서버 오류(HTTP {response.status_code})"
            continue
        if response.status_code >= 400:
            # 4xx 본문도 오류 XML이다. 구체적인 원인(미등록 키, 한도 초과)을 먼저 읽는다.
            auth_failed = response.status_code in (401, 403)
            try:
                _parse_response(response.text)
            except _AuthError:
                raise
            except _ToolError as exc:
                if not auth_failed or "한도" in str(exc):
                    raise
                # 본문이 XML이 아니어도 401·403이면 인증 문제로 본다.
            if auth_failed:
                raise _AuthError(
                    f"국토부 API 인증 실패(HTTP {response.status_code}). MOLIT_API_KEY와 활용신청 상태를 확인해야 합니다."
                )
            raise _ToolError(f"국토부 API 요청 오류(HTTP {response.status_code}).")
        return response.text
    raise _ToolError(
        f"국토부 API {last_error}: {MAX_RETRIES + 1}번 시도했지만 실패했습니다. 잠시 뒤 다시 조회하세요."
    )


def _fetch_month(housing_type: str, deal_type: str, lawd_cd: str, deal_ymd: str, key: str) -> list[_Deal]:
    """한 달치 매매 또는 전세 거래를 마지막 페이지까지 받는다."""
    trade_path, rent_path = MOLIT_ENDPOINTS[housing_type]
    path = trade_path if deal_type == "매매" else rent_path
    params: dict[str, Any] = {
        "serviceKey": key,  # 디코딩된 키 → requests가 한 번만 인코딩한다
        "LAWD_CD": lawd_cd,
        "DEAL_YMD": deal_ymd,
        "pageNo": 1,
        "numOfRows": NUM_OF_ROWS,
    }
    rows, total = _parse_response(_get_page(path, params))
    page = 1
    while len(rows) < total and page < MAX_PAGES:
        page += 1
        more, _ = _parse_response(_get_page(path, {**params, "pageNo": page}))
        if not more:
            break
        rows += more
    return [deal for row in rows if (deal := _to_deal(row, deal_type)) is not None]


def _fetch_all(
    housing_type: str, lawd_cd: str, deal_ymds: list[str]
) -> tuple[list[_Deal], list[_Deal], list[str], list[str]]:
    """여러 달의 매매·전세를 동시에 받는다. (매매, 전세, 실패한 달, 막힌 거래 종류) 반환.

    일부 달만 실패하면 나머지로 계산한다. 매매·전세 중 한쪽 API만 인증 오류면
    (예: 인증키에 매매 API 활용신청이 빠짐) 다른 쪽 결과는 살리고, 양쪽 모두 막히거나
    전부 실패하면 예외를 던진다.
    """
    key = _load_api_key()
    jobs = [(deal_type, ymd) for deal_type in ("매매", "전세") for ymd in deal_ymds]
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [
            pool.submit(_fetch_month, housing_type, deal_type, lawd_cd, ymd, key)
            for deal_type, ymd in jobs
        ]
        outcomes: list[list[_Deal] | _ToolError] = []
        for future in futures:
            try:
                outcomes.append(future.result())
            except _ToolError as exc:
                outcomes.append(exc)

    sales: list[_Deal] = []
    jeonse: list[_Deal] = []
    failed: list[str] = []
    errors: list[_ToolError] = []
    auth_errors: dict[str, _AuthError] = {}
    for (deal_type, ymd), outcome in zip(jobs, outcomes):
        if isinstance(outcome, _AuthError):
            auth_errors.setdefault(deal_type, outcome)
        elif isinstance(outcome, _ToolError):
            errors.append(outcome)
            failed.append(f"{deal_type} {ymd}")
        elif deal_type == "매매":
            sales += outcome
        else:
            jeonse += outcome

    if len(auth_errors) == 2:  # 매매·전세 모두 막힘: 키 자체의 문제
        raise auth_errors["매매"]
    # 한쪽만 막힘: 그 API만 활용신청이 안 된 경우. 막힌 쪽의 달별 실패는 따로 알리지 않는다.
    blocked = list(auth_errors)
    failed = [f for f in failed if f.split()[0] not in blocked]
    if errors and len(errors) + sum(1 for d, _ in jobs if d in blocked) == len(jobs):
        raise errors[0]  # 막히지 않은 쪽도 전부 실패
    return sales, jeonse, failed, blocked


# --------------------------------------------------------------------------- #
# 표본 추출과 통계
# --------------------------------------------------------------------------- #
# 표본 조건 → (범위, 면적 허용 오차). 위에서부터 시도해 3건 이상 모이는 첫 조건을 쓴다.
# 면적이 같은 구 단위 거래가, 면적이 다른 같은 단지·동 거래보다 가격 비교에 낫다고 보고
# 면적 무관 조건을 맨 뒤에 둔다.
_MATCH_LEVELS: dict[str, tuple[str, float | None]] = {
    "같은 단지·전용면적 ±5㎡": ("complex", AREA_TOLERANCE_NARROW_M2),
    "같은 단지·전용면적 ±10㎡": ("complex", AREA_TOLERANCE_WIDE_M2),
    "같은 동·전용면적 ±5㎡": ("dong", AREA_TOLERANCE_NARROW_M2),
    "같은 동·전용면적 ±10㎡": ("dong", AREA_TOLERANCE_WIDE_M2),
    "같은 시군구·전용면적 ±5㎡": ("sigungu", AREA_TOLERANCE_NARROW_M2),
    "같은 시군구·전용면적 ±10㎡": ("sigungu", AREA_TOLERANCE_WIDE_M2),
    "같은 단지·면적 무관": ("complex", None),
    "같은 동·면적 무관": ("dong", None),
    "같은 시군구·면적 무관": ("sigungu", None),
}
NO_MATCH = "표본 없음"


def _normalize_name(name: str | None) -> str:
    return re.sub(r"[\s()\-·.,]", "", name or "").lower()


def _same_dong(deal_dong: str | None, dong: str) -> bool:
    if not deal_dong:
        return False
    return deal_dong == dong or dong in deal_dong.split() or deal_dong.endswith(dong)


def _same_complex(deal: _Deal, target: _Target) -> bool:
    """같은 단지: (동을 알면 동 일치) AND 건물명이 서로 포함 관계."""
    if target.dong and not _same_dong(deal.dong, target.dong):
        return False
    want, have = _normalize_name(target.building_name), _normalize_name(deal.building_name)
    return len(want) >= 2 and len(have) >= 2 and (want in have or have in want)


def _applicable(level: str, target: _Target) -> bool:
    scope, tolerance = _MATCH_LEVELS[level]
    if tolerance is not None and target.area_m2 is None:
        return False
    if scope == "complex":
        return len(_normalize_name(target.building_name)) >= 2
    return scope != "dong" or bool(target.dong)


def _filter(deals: list[_Deal], level: str, target: _Target) -> list[_Deal]:
    scope, tolerance = _MATCH_LEVELS[level]
    picked = deals
    if scope == "complex":
        picked = [d for d in picked if _same_complex(d, target)]
    elif scope == "dong":
        picked = [d for d in picked if _same_dong(d.dong, target.dong or "")]
    if tolerance is not None:
        picked = [
            d for d in picked
            if d.area_m2 is not None and abs(d.area_m2 - (target.area_m2 or 0)) <= tolerance
        ]
    return picked


def _select_samples(deals: list[_Deal], target: _Target) -> tuple[list[_Deal], str]:
    """좁은 조건부터 시도한다. 3건을 못 채우면 가장 많이 모인 조건을 쓴다."""
    best: tuple[list[_Deal], str] = ([], NO_MATCH)
    for level in _MATCH_LEVELS:
        if not _applicable(level, target):
            continue
        picked = _filter(deals, level, target)
        if len(picked) >= MIN_SAMPLES:
            return picked, level
        if len(picked) > len(best[0]):
            best = (picked, level)
    return best


def _remove_outliers(deals: list[_Deal], *, by_unit_price: bool) -> tuple[list[_Deal], int]:
    """사분위 범위(IQR) 밖의 거래를 뺀다. 면적이 섞인 표본은 ㎡당 가격으로 판단한다."""
    if len(deals) < OUTLIER_MIN_SAMPLES:
        return deals, 0
    use_unit = by_unit_price and all(d.area_m2 for d in deals)
    values = [d.price_won / d.area_m2 if use_unit else float(d.price_won) for d in deals]
    q1, _, q3 = statistics.quantiles(values, n=4, method="inclusive")
    if q3 <= q1:
        # 절반 넘는 거래가 같은 값이면 범위가 0이 되어 정상 거래까지 빠지므로 빼지 않는다.
        return deals, 0
    spread = (q3 - q1) * OUTLIER_IQR_MULTIPLIER
    kept = [d for d, v in zip(deals, values) if q1 - spread <= v <= q3 + spread]
    return kept, len(deals) - len(kept)


def _stats(deals: list[_Deal], target: _Target) -> tuple[dict[str, Any], list[_Deal]]:
    picked, level = _select_samples(deals, target)
    empty = {
        "count": 0, "mean_won": None, "median_won": None, "min_won": None, "max_won": None,
        "outliers_removed": 0, "sample_basis": NO_MATCH,
    }
    if not picked:
        return empty, []
    kept, removed = _remove_outliers(picked, by_unit_price=_MATCH_LEVELS[level][1] is None)
    prices = [d.price_won for d in kept]
    return {
        "count": len(prices),
        "mean_won": round(statistics.fmean(prices)),
        "median_won": round(statistics.median(prices)),
        "min_won": min(prices),
        "max_won": max(prices),
        "outliers_removed": removed,
        "sample_basis": level,
    }, kept


def _deal_view(deal: _Deal) -> dict[str, Any]:
    return {
        "deal_date": deal.deal_date.isoformat() if deal.deal_date else None,
        "price_won": deal.price_won,
        "building_name": deal.building_name,
        "dong": deal.dong,
        "exclusive_area_m2": deal.area_m2,
        "floor": deal.floor,
        "build_year": deal.build_year,
    }


def _recent(deals: list[_Deal]) -> list[dict[str, Any]]:
    ordered = sorted(deals, key=lambda d: d.deal_date or date.min, reverse=True)
    return [_deal_view(d) for d in ordered[:RECENT_DEALS_LIMIT]]


def _ratio_pct(numerator: int | None, denominator: int | None) -> float | None:
    if not numerator or not denominator:
        return None
    return round(numerator / denominator * 100, 1)


def _fail(error: str) -> dict[str, Any]:
    return {"ok": False, "error": error}


# --------------------------------------------------------------------------- #
# 도구 1: search_price
# --------------------------------------------------------------------------- #
def search_price(
    region: str,
    housing_type: str = "아파트",
    building_name: str | None = None,
    exclusive_area_m2: float | None = None,
    months: int = DEFAULT_MONTHS,
) -> dict[str, Any]:
    """주변 매매·전세 실거래가를 조회해 매매 시세와 전세 시세를 낸다.

    언제 쓰나: 전세가율을 계산하기 전에 매물 주변의 매매 시세가 필요할 때 먼저 부른다.
    결과의 data.market_price_won을 calc_jeonse_ratio의 market_price_won으로 넘긴다.
    매매 거래가 0건이면 months를 늘려(최대 24) 다시 조회한다.
    쓰지 말 것: 법·보증보험 근거나 청년 정책을 찾을 때는 쓰지 않는다.
    오피스텔은 지원하지 않는다(아파트·연립다세대만).

    국토교통부 실거래가 API에서 최근 months개월의 거래를 받아 월세와 해제된 거래를 빼고,
    같은 단지 → 같은 동 → 같은 시군구 순으로 비슷한 면적의 거래를 골라 평균·중위값을 낸다.
    급매·특수거래는 사분위 범위(IQR) 밖의 값으로 보고 뺀다.

    Args:
        region: 시/도, 시/군/구, 법정동까지의 지역. 예: "서울 강남구 역삼동",
            "경기 성남시 분당구 정자동". 번지·동호수는 넣지 않는다. 지원 지역: 서울·인천·경기.
        housing_type: "아파트" | "연립다세대". 빌라·다세대·연립은 "연립다세대".
        building_name: 단지·건물명. 예: "래미안그레이튼". 주면 같은 단지 거래를 먼저 비교한다.
        exclusive_area_m2: 전용면적(㎡). 공급면적이 아니다. 없으면 면적 구분 없이 계산한다.
        months: 조회할 최근 개월 수(이번 달 포함, 1~24, 기본 6).

    Returns:
        성공: {"ok": True, "data": {...}, "summary": "..."}
            data.market_price_won: 매매 시세(평균, 원). 매매 거래가 없으면 None.
            data.market_jeonse_ratio_pct: 주변 전세 평균 ÷ 매매 평균 × 100.
            data.sale / data.jeonse: count, mean_won, median_won, min_won, max_won,
                outliers_removed, sample_basis(표본 조건).
            data.recent_sales / data.recent_jeonse: 계산에 쓴 최근 거래 최대 5건.
            data.warnings: 결과를 해석할 때 주의할 점.
        실패: {"ok": False, "error": "원인과 다음에 할 일"}
    """
    # 입력 검사부터 결과 조립까지 전부 감싼다. 도구는 예외를 밖으로 던지지 않는다 (AGENTS.md 3장).
    try:
        return _search_price(region, housing_type, building_name, exclusive_area_m2, months)
    except _ToolError as exc:
        logger.info("search_price 실패: %s", type(exc).__name__)
        return _fail(str(exc))
    except Exception as exc:
        logger.warning("search_price 예상하지 못한 오류: %s", type(exc).__name__)
        return _fail("시세 조회 중 예상하지 못한 오류가 났습니다. 잠시 뒤 다시 조회하세요.")


def _is_number(value: Any) -> bool:
    """bool이 아닌 유한한 숫자인가. (bool은 int의 하위 타입이라 따로 뺀다)"""
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _search_price(
    region: Any,
    housing_type: Any,
    building_name: Any,
    exclusive_area_m2: Any,
    months: Any,
) -> dict[str, Any]:
    if not isinstance(region, str) or not region.strip():
        return _fail("지역이 비어 있습니다. '서울 강남구 역삼동'처럼 넣어 다시 조회하세요.")
    type_key = str(housing_type).strip().lower().replace(" ", "").replace("/", "")
    if type_key in UNSUPPORTED_HOUSING_TYPES:
        return _fail(UNSUPPORTED_HOUSING_TYPES[type_key])
    normalized_type = HOUSING_TYPE_ALIASES.get(type_key)
    if normalized_type is None:
        return _fail(
            f"지원하지 않는 주택 유형입니다: {housing_type!r}. '아파트', '연립다세대' 중 하나로 다시 조회하세요."
        )
    # LLM이 6을 6.0으로 넘기는 경우도 받아 준다.
    if not _is_number(months) or months != int(months) or not 1 <= months <= MAX_MONTHS:
        return _fail(f"months는 1~{MAX_MONTHS} 사이 정수여야 합니다.")
    months = int(months)
    if exclusive_area_m2 is not None and (not _is_number(exclusive_area_m2) or exclusive_area_m2 <= 0):
        return _fail("exclusive_area_m2는 0보다 큰 전용면적(㎡)이어야 합니다. 모르면 비워 두세요.")
    if building_name is not None and not isinstance(building_name, str):
        return _fail("building_name은 단지·건물명 문자열이어야 합니다. 모르면 비워 두세요.")
    building_name = (building_name or "").strip() or None

    place = _resolve_region(region)
    deal_ymds = _recent_deal_ymds(months)
    sales, jeonse, failed, blocked = _fetch_all(normalized_type, place.lawd_cd, deal_ymds)

    area = float(exclusive_area_m2) if exclusive_area_m2 is not None else None
    target = _Target(dong=place.dong, area_m2=area, building_name=building_name)
    sale_stats, sale_used = _stats(sales, target)
    jeonse_stats, jeonse_used = _stats(jeonse, target)
    where = " ".join(p for p in (place.sigungu, place.dong) if p)

    api_names = [f"{normalized_type} {'매매' if d == '매매' else '전월세'} 실거래가" for d in blocked]
    blocked_note = (
        f"{', '.join(api_names)} API를 쓸 수 없습니다(인증키에 활용신청이 안 됨). "
        "공공데이터포털에서 해당 API를 활용신청해야 합니다."
    ) if blocked else ""

    if sale_stats["count"] == 0 and jeonse_stats["count"] == 0:
        if blocked:
            return _fail(f"{blocked_note} 나머지 거래도 최근 {months}개월 0건입니다.")
        return _fail(
            f"해당 지역 거래 없음: 최근 {months}개월 {where} {normalized_type} 매매·전세 거래가 0건입니다. "
            "months를 늘리거나 주택 유형을 확인해 다시 조회하세요."
        )

    warnings: list[str] = [blocked_note] if blocked else []
    if area is None:
        warnings.append("전용면적이 없어 면적 구분 없이 계산했습니다. 면적에 따라 가격 차이가 큽니다.")
    if place.dong is None:
        warnings.append("법정동을 찾지 못해 시군구 전체 거래로 계산했습니다.")
    if building_name and not sale_stats["sample_basis"].startswith("같은 단지"):
        warnings.append("같은 단지의 매매 거래가 부족해 주변 거래로 계산했습니다.")
    if place.dong and sale_stats["sample_basis"].startswith("같은 시군구"):
        warnings.append(f"{place.dong}의 매매 거래가 부족해 시군구 전체 거래로 계산했습니다. 동네 시세와 다를 수 있습니다.")
    for label, stats in (("매매", sale_stats), ("전세", jeonse_stats)):
        if label in blocked:
            continue  # 막힌 API는 위에서 이미 알렸다
        if stats["count"] == 0:
            warnings.append(f"{label} 거래가 0건입니다.")
        elif stats["count"] < MIN_SAMPLES:
            warnings.append(f"{label} 표본이 {stats['count']}건뿐이라 신뢰도가 낮습니다.")
    if sale_stats["count"] and jeonse_stats["count"] and sale_stats["sample_basis"] != jeonse_stats["sample_basis"]:
        warnings.append("매매와 전세의 표본 조건이 달라 시장 전세가율은 참고용입니다.")
    if failed:
        warnings.append(f"일부 기간 수집 실패: {', '.join(failed)}")

    market_price_won = sale_stats["mean_won"]
    market_ratio = _ratio_pct(jeonse_stats["mean_won"], market_price_won)

    if market_price_won is None:
        reason = "매매 API 미신청" if "매매" in blocked else "매매 0건"
        next_step = "매매 시세는 사용자에게 확인 필요" if reason == "매매 API 미신청" else "months를 늘려 다시 조회 필요"
        summary = (
            f"{where} {normalized_type}: {reason}이라 전세가율 계산 불가, 전세 {jeonse_stats['count']}건 "
            f"평균 {jeonse_stats['mean_won']:,}원. {next_step}"
        )
    else:
        summary = (
            f"{where} {normalized_type}({sale_stats['sample_basis']}): 매매 {sale_stats['count']}건 "
            f"평균 {market_price_won:,}원, 전세 {jeonse_stats['count']}건"
        )
        if jeonse_stats["mean_won"]:
            summary += f" 평균 {jeonse_stats['mean_won']:,}원, 시장 전세가율 {market_ratio}%"
        summary += f" (최근 {months}개월)"

    return {
        "ok": True,
        "data": {
            "region": {"lawd_cd": place.lawd_cd, "sigungu": f"{place.sido} {place.sigungu}", "dong": place.dong},
            "housing_type": normalized_type,
            "building_name": building_name,
            "exclusive_area_m2": area,
            "deal_ymds": deal_ymds,
            "market_price_won": market_price_won,
            "market_jeonse_ratio_pct": market_ratio,
            "sale": sale_stats,
            "jeonse": jeonse_stats,
            "recent_sales": _recent(sale_used),
            "recent_jeonse": _recent(jeonse_used),
            "warnings": warnings,
        },
        "summary": summary[:SUMMARY_MAX_CHARS],
    }


# --------------------------------------------------------------------------- #
# 도구 2: calc_jeonse_ratio
# --------------------------------------------------------------------------- #
def _classify_risk(ratio_pct: float) -> str:
    """70% 미만 안전, 70~80% 주의, 80% 초과 위험."""
    if ratio_pct > JEONSE_RATIO_THRESHOLD_PCT:
        return RISK_DANGER
    if ratio_pct >= JEONSE_RATIO_CAUTION_PCT:
        return RISK_CAUTION
    return RISK_SAFE


def _as_won(value: Any, name: str, minimum_won: int) -> int:
    if not _is_number(value):
        raise _ToolError(f"{name}은(는) 원 단위 숫자여야 합니다.")
    won = round(value)
    if won <= 0:
        raise _ToolError(f"{name}은(는) 0보다 커야 합니다.")
    if won < minimum_won:
        raise _ToolError(
            f"{name}이(가) {won:,}원으로 너무 작습니다. 만원 단위로 넣은 것 같습니다. "
            "원 단위로 바꿔 다시 계산하세요(예: 2억 1천만 원 → 210000000)."
        )
    return won


def calc_jeonse_ratio(deposit_won: int, market_price_won: int) -> dict[str, Any]:
    """보증금 ÷ 매매 시세로 전세가율을 계산하고 위험 등급을 정한다.

    언제 쓰나: search_price로 매매 시세(data.market_price_won)를 얻은 뒤 부른다.
    결과가 "위험"이나 "주의"면 법·보증보험 근거를 찾고, "안전"이면 지원 정책을 찾는 식으로
    다음 행동을 정하는 데 쓴다.
    쓰지 말 것: 매매 시세가 없을 때(search_price에서 매매 0건) 시세를 지어내 넣지 않는다.

    등급 기준: 70% 미만 안전, 70% 이상 80% 이하 주의, 80% 초과 위험.
    등급은 소수 첫째 자리로 반올림한 전세가율로 정한다(화면에 보이는 값과 등급이 어긋나지 않게).

    Args:
        deposit_won: 계약하려는 보증금(원). 전세 보증금이나 월세 보증금. 예: 2억 1천만 원 → 210000000.
        market_price_won: 매매 시세(원). search_price의 data.market_price_won.

    Returns:
        성공: {"ok": True, "data": {...}, "summary": "..."}
            data.jeonse_ratio_pct: 전세가율(%)
            data.risk_level: "위험" | "주의" | "안전"
            data.risk_note: 한 줄 권고
            data.threshold_pct: 위험 기준선(80.0)
            data.margin_won: 매매 시세 - 보증금. 집값이 이만큼 넘게 떨어지면 보증금을 다 돌려받기 어렵다.
                0 이하면 지금도 보증금이 시세 이상이라는 뜻이다.
        실패: {"ok": False, "error": "원인과 다음에 할 일"}
    """
    # 계산과 결과 조립까지 전부 감싼다. 도구는 예외를 밖으로 던지지 않는다 (AGENTS.md 3장).
    try:
        return _calc_jeonse_ratio(deposit_won, market_price_won)
    except _ToolError as exc:
        return _fail(str(exc))
    except Exception as exc:
        logger.warning("calc_jeonse_ratio 예상하지 못한 오류: %s", type(exc).__name__)
        return _fail("전세가율 계산 중 예상하지 못한 오류가 났습니다. 입력값을 확인해 다시 계산하세요.")


def _calc_jeonse_ratio(deposit_won: Any, market_price_won: Any) -> dict[str, Any]:
    deposit = _as_won(deposit_won, "deposit_won(보증금)", MIN_PLAUSIBLE_DEPOSIT_WON)
    market = _as_won(market_price_won, "market_price_won(매매 시세)", MIN_PLAUSIBLE_MARKET_PRICE_WON)
    ratio = round(deposit / market * 100, 1)
    level = _classify_risk(ratio)
    note = RISK_NOTES[level]
    warnings: list[str] = []
    if ratio > UNIT_SUSPECT_RATIO_PCT:
        warnings.append(
            f"전세가율이 {ratio}%로 비정상적으로 높습니다. 두 금액의 단위(원)가 같은지 확인하세요."
        )
    elif ratio >= FULL_LOSS_RATIO_PCT:
        warnings.append("보증금이 매매 시세 이상입니다. 지금 팔아도 보증금을 다 돌려받기 어렵습니다.")
    elif ratio < UNIT_SUSPECT_LOW_RATIO_PCT:
        warnings.append(
            f"전세가율이 {ratio}%로 매우 낮습니다. 보증금이 작은 월세가 아니라면 "
            "보증금을 만원 단위로 넣지 않았는지 확인하세요."
        )

    return {
        "ok": True,
        "data": {
            "jeonse_ratio_pct": ratio,
            "risk_level": level,
            "risk_note": note,
            "threshold_pct": JEONSE_RATIO_THRESHOLD_PCT,
            "caution_threshold_pct": JEONSE_RATIO_CAUTION_PCT,
            "deposit_won": deposit,
            "market_price_won": market,
            "margin_won": market - deposit,
            "warnings": warnings,
        },
        "summary": (
            f"전세가율 {ratio}% (기준선 {JEONSE_RATIO_THRESHOLD_PCT:g}%) → {level}: {note}"
        )[:SUMMARY_MAX_CHARS],
    }
