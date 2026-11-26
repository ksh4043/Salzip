"""청년 주거 지원 정책 검색과 자격 판정 도구 (담당 D).

- search_policy: 온통청년 청년정책 API에서 매물 지역의 주거 분야 정책을 찾는다
- check_eligibility: 나이·소득 구간 등으로 정책별 자격을 판정한다

두 도구를 나눈 이유: LLM이 정책 목록을 먼저 보고 고른 뒤 자격을 판정하고,
탈락하면 exclude_ids로 다른 정책을 다시 찾는 경로를 스스로 고르게 하기 위해서다.
(AGENTS.md 1장 "LLM이 다음 행동을 고른다", 2장 테스트 시나리오 인물 C)

판정 원칙: 온통청년 API에 구조화된 값으로 있는 조건만 함수로 판정한다.
소득 조건이 문장(예: "기준 중위소득 60% 이하")으로만 적혀 있거나 값이 서로 어긋나면
지어내 판정하지 않고 "확인 필요"로 돌려준다. (AGENTS.md 5장 사실과 근거)

나이·소득은 정확한 값이 아니라 구간으로 받는다(AGENTS.md 4장). 구간 이름은
AGE_BANDS, INCOME_BANDS의 키이며 화면(app.py)도 이 키를 그대로 선택지로 쓴다.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import date
from typing import Any

import requests

# 지역 → 시군구코드 변환은 search_price와 같은 표·규칙을 쓴다(2026년 행정구역 개편 반영).
# 같은 지역 입력이 두 도구에서 다르게 해석되지 않게 하려고 따로 만들지 않고 가져온다.
from tools.price import _resolve_region
from tools.price import _ToolError as _RegionError

logger = logging.getLogger(__name__)


class _RedactApiKeyFilter(logging.Filter):
    """로그에서 온통청년 인증키를 가린다 (AGENTS.md 6장: 로그에 API 키를 남기지 않는다).

    온통청년은 키를 URL 쿼리(apiKeyNm=...)로만 받는다. 앱이 로그 수준을 DEBUG로 두면
    urllib3가 요청 URL 전체를 남기므로 그 기록에서 키 부분만 지운다. (tools/price.py와 같은 방식)
    """

    _PATTERN = re.compile(r"(apiKeyNm=)[^&\s\"']+", re.IGNORECASE)

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if "apikeynm=" in message.lower():
            record.msg = self._PATTERN.sub(r"\1***", message)
            record.args = None
        return True


logging.getLogger("urllib3.connectionpool").addFilter(_RedactApiKeyFilter())

# --------------------------------------------------------------------------- #
# 온통청년 청년정책 API
# 2026-11-26 공용 인증키로 호출해 응답 형식과 필드를 확인했다.
# 데이터 설명: https://www.data.go.kr/data/15143273/openapi.do (한국고용정보원_온통청년_청년정책API)
# --------------------------------------------------------------------------- #
YOUTHCENTER_API_URL = "https://www.youthcenter.go.kr/go/ythip/getPlcy"
HOUSING_CATEGORY = "주거"  # 정책대분류명(lclsfNm). 주거 분야만 다룬다.
PAGE_SIZE = 100
MAX_PAGES = 10  # 한 지역의 주거 정책은 2026-11-26 기준 100건 안팎이다(서울 강서구 75건).
REQUEST_TIMEOUT_SEC = 10
MAX_RETRIES = 2  # 타임아웃·연결 오류·5xx일 때 추가로 시도하는 횟수
RETRY_BACKOFF_SEC = 0.5  # n번째 재시도 전 대기 = 0.5 × 2^(n-1)초
API_OK_CODE = 200  # 응답 본문의 resultCode
API_AUTH_ERROR_CODE = "e001"  # 응답 본문의 errorCode ("invalid api key.")

# 코드값 의미: 온통청년 정책 상세 화면(ythPlcyDetail)의 스크립트가 쓰는 매핑을 그대로 옮겼다(2026-11-26 확인).
AGE_NO_LIMIT_FLAG = "Y"  # sprtTrgtAgeLmtYn == "Y" → 화면에 "제한없음"으로 표시
EARN_ANY = "0043001"  # 소득 무관
EARN_ANNUAL = "0043002"  # 연소득 earnMinAmt만원 이상 ~ earnMaxAmt만원 이하
EARN_OTHER = "0043003"  # 기타: earnEtcCn 문장
MARRIED_ONLY = "0055001"  # 기혼
SINGLE_ONLY = "0055002"  # 미혼
MARRIAGE_ANY = "0055003"  # 제한없음
APPLY_PERIOD = "0057001"  # 특정기간 (aplyYmd에 기간)
APPLY_ALWAYS = "0057002"  # 상시
APPLY_CLOSED = "0057003"  # 마감
# 취업상태(jobCd)·학력(schoolCd)·특화분야(sbizCd)의 "제한없음" 코드.
# 공식 코드표를 찾지 못해 2026-11-26 주거 정책 428건의 분포로 정했다: 각각 398·400·414건이 이 값 하나만 갖고,
# 다른 값은 "0013001,0013002,..."처럼 특정 대상을 나열한다. 그래서 이 값이 아니면 탈락시키지 않고 "확인 필요"로만 둔다.
UNRESTRICTED_JOB_CODE = "0013010"
UNRESTRICTED_SCHOOL_CODE = "0049010"
UNRESTRICTED_SPECIAL_CODE = "0014010"
WON_PER_MANWON = 10_000  # 소득 금액(earnMinAmt, earnMaxAmt)은 만원 단위
# 연소득 상한이 이보다 작으면 단위가 다를 가능성이 크다. 2026-11-26 자료의 "청년층 대상 공공분양주택 공급"은
# earnMaxAmt가 534(만원)인데, 연 534만원보다는 월평균소득 534만원으로 보인다.
MIN_PLAUSIBLE_ANNUAL_INCOME_MANWON = 1_000
# earnMinAmt·earnMaxAmt가 범위가 아니라 대상별 상한인 경우를 알아보는 말.
# 예: "2026년 전세보증금반환보증 보증료 지원사업"은 5000~7500인데, 본문은 "(청년) 5천만원, (청년 외) 6천만원,
# (신혼부부) 7.5천만원 이하"다(2026-11-26 확인). 이런 정책은 사용자에게 어느 기준이 맞는지 알 수 없다.
_TIERED_INCOME_PATTERN = re.compile(r"청년\s*외|신혼|부부|맞벌이|다자녀|외벌이")

# 2026-02-01 행정구역 개편 전에 등록된 정책은 개편 전 지역 코드를 갖는다(온통청년 상세 화면 스크립트의
# siGunGuChangeDate). 2026-11-26 주거 정책 428건으로 확인한 실제 모양은 두 가지다.
# 1) 옛 코드 그대로: "경기도 공공기숙사 운영"은 화성시 옛 코드 41590만 갖는다. 41590은 옛 화성시 전체라
#    지금의 화성시 4개 구를 모두 포함한다 → 거주지역 충족으로 본다.
PREVIOUS_SIGUNGU_CODES: dict[str, tuple[str, ...]] = {
    "28125": ("28110", "28140"),  # 제물포구 ← 인천 중구(내륙)·동구 (자료에는 없지만 옛 코드 그대로 올 때를 대비)
    "28155": ("28110",),  # 영종구 ← 인천 중구(영종도)
    "28275": ("28260",),  # 서해구 ← 인천 서구
    "28290": ("28260",),  # 검단구 ← 인천 서구
    "41591": ("41590",),  # 화성시 만세구 ← 화성시
    "41593": ("41590",),  # 화성시 효행구 ← 화성시
    "41595": ("41590",),  # 화성시 병점구 ← 화성시
    "41597": ("41590",),  # 화성시 동탄구 ← 화성시
}
# 2) 새 코드로 옮겨졌지만 갈라진 구가 빠짐: 인천시 전체 대상 정책 44건이 영종구(28155)·서해구(28275) 없이
#    옛 중구·동구 → 28125(제물포구), 옛 서구 → 28290(검단구)으로만 등록돼 있다. 그런데 28125에는
#    "(동구) 청년 이사비"처럼 영종도와 상관없는 옛 동구 전용 정책도 있어서, 이 코드로만 맞으면
#    충족도 탈락도 아닌 "확인 필요"로 둔다.
SPLIT_SIBLING_CODES: dict[str, tuple[str, ...]] = {
    "28155": ("28125",),  # 영종구 ↔ 제물포구 (옛 인천 중구)
    "28275": ("28290",),  # 서해구 ↔ 검단구 (옛 인천 서구)
}
# 대상 시군구가 이보다 많으면 "전국" 정책으로 본다. 온통청년 화면은 전체 시군구 수(개편 전 252)와 같을 때
# 전국으로 표시하는데, 2026-11-26 자료에는 247·251·254·256개가 섞여 있어 넉넉히 잡았다.
NATIONWIDE_MIN_CODES = 200

# --------------------------------------------------------------------------- #
# 사용자 입력 구간 (AGENTS.md 4장: 나이·소득은 구간으로 받는다)
# 경계는 2026-11-26 주거 정책의 실제 기준값에 맞췄다. 나이 상한은 34·39·45세, 연소득 상한은
# 4,000·5,000·6,000·7,000·7,500·8,000만원·1억 원이 많아 이 경계에서 판정이 갈리게 했다.
# --------------------------------------------------------------------------- #
AGE_BAND_OPEN_MAX = 200  # "45세 이상"처럼 위가 열린 구간의 상한 대용값
AGE_BANDS: dict[str, tuple[int, int]] = {  # 만 나이, 양 끝 포함
    "19세 미만": (0, 18),
    "19~24세": (19, 24),
    "25~29세": (25, 29),
    "30~34세": (30, 34),
    "35~39세": (35, 39),
    "40~44세": (40, 44),
    "45세 이상": (45, AGE_BAND_OPEN_MAX),
}
INCOME_BANDS: dict[str, tuple[int, int | None]] = {  # 본인 연소득(원), 양 끝 포함, None은 상한 없음
    "소득 없음": (0, 0),
    "2,000만원 이하": (1, 20_000_000),
    "2,000만원 초과~3,000만원 이하": (20_000_001, 30_000_000),
    "3,000만원 초과~4,000만원 이하": (30_000_001, 40_000_000),
    "4,000만원 초과~5,000만원 이하": (40_000_001, 50_000_000),
    "5,000만원 초과~6,000만원 이하": (50_000_001, 60_000_000),
    "6,000만원 초과~7,000만원 이하": (60_000_001, 70_000_000),
    "7,000만원 초과~8,000만원 이하": (70_000_001, 80_000_000),
    "8,000만원 초과~1억원 이하": (80_000_001, 100_000_000),
    "1억원 초과": (100_000_001, None),
}

# --------------------------------------------------------------------------- #
# 결과 형식
# --------------------------------------------------------------------------- #
STATUS_ELIGIBLE = "해당"
STATUS_UNKNOWN = "확인 필요"
STATUS_INELIGIBLE = "해당 없음"
PASS, UNKNOWN, FAIL = "충족", "확인 필요", "미충족"  # 조건 하나의 판정

DEFAULT_MAX_RESULTS = 8
MAX_RESULTS = 15
MAX_POLICY_IDS = 10  # 한 번에 판정하는 정책 수. 보고서 policies 상한(10)과 같다.
MAX_EXCLUDE_IDS = 50
MAX_KEYWORD_CHARS = 50
PREVIEW_CHARS = 120  # 정책 설명·조건 문장을 LLM에 보낼 때 자르는 길이
SUMMARY_MAX_CHARS = 200  # AGENTS.md 3장
SUMMARY_NAME_CHARS = 20
CACHE_MAX_POLICIES = 1000  # 검색한 정책을 판정 때 다시 부르지 않으려고 메모리에 둔다(공개 정책 정보만).
# 추가 조건 칸에 "없음"을 뜻하는 값. 이 값이면 추가 조건이 없는 것으로 본다.
EMPTY_CONDITION_TEXTS = {"", "-", "없음", "해당없음", "해당 없음", "무관", "제한없음", "제한 없음"}
_PRICE_REGION_HINT = "지원 지역으로 다시 조회하거나 사용자에게 시세를 직접 확인하도록 안내하세요."
_POLICY_REGION_HINT = "정책은 빈 목록으로 두고 사용자에게 온통청년(youthcenter.go.kr)에서 직접 찾도록 안내하세요."
_DATE_RANGE = re.compile(r"(\d{8})\s*~\s*(\d{8})")

_POLICY_CACHE: dict[str, dict[str, str]] = {}


class _PolicyError(Exception):
    """도구 안에서만 쓰는 예외. 메시지는 그대로 LLM에 실패 사유로 전달된다."""


class _TransientApiError(_PolicyError):
    """다시 시도하면 나을 수 있는 API 오류(인증 오류가 아닌 오류 코드)."""


@dataclass(frozen=True)
class _Check:
    """조건 하나의 판정."""

    result: str  # PASS | UNKNOWN | FAIL
    reason: str


# --------------------------------------------------------------------------- #
# API 호출
# --------------------------------------------------------------------------- #
def _load_api_key() -> str:
    key = (os.getenv("YOUTHCENTER_API_KEY") or "").strip()
    if not key:
        raise _PolicyError(
            "온통청년 API 인증키(YOUTHCENTER_API_KEY)가 설정되지 않아 정책을 조회할 수 없습니다. "
            "정책은 '확인 필요'로 두고 온통청년(youthcenter.go.kr)에서 직접 찾도록 안내하세요."
        )
    return key


def _request(params: dict[str, Any]) -> dict[str, Any]:
    """API를 한 번 부른다. 일시적인 오류는 다시 시도한다. 인증키는 로그에 남기지 않는다."""
    query = {"apiKeyNm": _load_api_key(), "rtnType": "json", **params}
    for attempt in range(MAX_RETRIES + 1):
        if attempt:
            time.sleep(RETRY_BACKOFF_SEC * 2 ** (attempt - 1))
        try:
            response = requests.get(YOUTHCENTER_API_URL, params=query, timeout=REQUEST_TIMEOUT_SEC)
        except (requests.Timeout, requests.ConnectionError) as exc:
            logger.info("온통청년 API 연결 오류(%s), 시도 %d", type(exc).__name__, attempt + 1)
            continue
        except requests.RequestException as exc:
            logger.warning("온통청년 API 요청 오류: %s", type(exc).__name__)
            break
        if response.status_code >= 500:
            logger.info("온통청년 API HTTP %d, 시도 %d", response.status_code, attempt + 1)
            continue
        try:
            return _parse_body(response)
        except _TransientApiError:
            # 2026-11-26 연속 호출 중 인증 오류가 아닌 오류 응답이 한 번 왔다가 곧 정상으로 돌아왔다.
            continue
    raise _PolicyError(
        "온통청년 API가 응답하지 않습니다. 잠시 뒤 한 번 더 시도하고, 그래도 안 되면 정책을 '확인 필요'로 두세요."
    )


def _parse_body(response: requests.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        raise _PolicyError("온통청년 API 응답을 읽지 못했습니다. 잠시 뒤 다시 시도하세요.") from None
    if not isinstance(body, dict):
        raise _PolicyError("온통청년 API 응답 형식이 예상과 다릅니다. 잠시 뒤 다시 시도하세요.")
    if body.get("errorCode") == API_AUTH_ERROR_CODE:
        raise _PolicyError(
            "온통청년 API 인증키가 올바르지 않습니다. 정책은 '확인 필요'로 두고 온통청년에서 직접 찾도록 안내하세요."
        )
    if body.get("errorCode") or body.get("resultCode") != API_OK_CODE:
        logger.warning("온통청년 API 오류 코드: %s", body.get("errorCode") or body.get("resultCode"))
        raise _TransientApiError("온통청년 API가 오류를 돌려줬습니다.")
    result = body.get("result")
    return result if isinstance(result, dict) else {}


def _policy_rows(result: dict[str, Any]) -> list[dict[str, str]]:
    rows = result.get("youthPolicyList")
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict) and row.get("plcyNo")]


def _fetch_region_policies(zip_codes: list[str]) -> list[dict[str, str]]:
    """해당 시군구(옛 코드 포함)의 주거 분야 정책을 모두 가져온다."""
    policies: dict[str, dict[str, str]] = {}
    for page in range(1, MAX_PAGES + 1):
        result = _request({
            "pageNum": page,
            "pageSize": PAGE_SIZE,
            "lclsfNm": HOUSING_CATEGORY,
            "zipCd": ",".join(zip_codes),  # 쉼표로 여러 코드를 넣으면 OR 조건이다(2026-11-26 확인).
        })
        rows = _policy_rows(result)
        for row in rows:
            policies[row["plcyNo"]] = row
        total = _parse_int((result.get("pagging") or {}).get("totCount"))
        if not rows or len(rows) < PAGE_SIZE or (total is not None and len(policies) >= total):
            break
    return list(policies.values())


def _fetch_policy(policy_id: str) -> dict[str, str] | None:
    rows = _policy_rows(_request({"pageNum": 1, "pageSize": 1, "plcyNo": policy_id}))
    return next((row for row in rows if row.get("plcyNo") == policy_id), None)


def _remember(rows: list[dict[str, str]]) -> None:
    for row in rows:
        _POLICY_CACHE.pop(row["plcyNo"], None)
        _POLICY_CACHE[row["plcyNo"]] = row
    while len(_POLICY_CACHE) > CACHE_MAX_POLICIES:
        _POLICY_CACHE.pop(next(iter(_POLICY_CACHE)))


# --------------------------------------------------------------------------- #
# 필드 해석
# --------------------------------------------------------------------------- #
def _today() -> date:
    return date.today()


def _text(row: dict[str, Any], key: str) -> str:
    value = row.get(key)
    return value.strip() if isinstance(value, str) else ""


def _parse_int(raw: Any) -> int | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return int(raw.replace(",", "").strip())
    except ValueError:
        return None


def _preview(text: str, limit: int = PREVIEW_CHARS) -> str:
    flat = re.sub(r"\s+", " ", text).strip()
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _codes(raw: str) -> set[str]:
    return {code.strip() for code in raw.split(",") if code.strip()}


def _zip_codes(row: dict[str, Any]) -> set[str]:
    return _codes(_text(row, "zipCd"))


def _parse_ymd(raw: str) -> date | None:
    try:
        return date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
    except ValueError:
        return None


def _apply_period(row: dict[str, Any], today: date) -> tuple[str, str]:
    """신청기간 상태와 표시 문구. 상태: open | upcoming | closed | unknown."""
    code = _text(row, "aplyPrdSeCd")
    if code == APPLY_ALWAYS:
        return "open", "상시"
    if code == APPLY_CLOSED:
        return "closed", "마감"
    ranges = [
        (_parse_ymd(begin), _parse_ymd(end))
        for begin, end in _DATE_RANGE.findall(_text(row, "aplyYmd"))  # 여러 기간이 "\N"으로 이어져 올 수 있다
    ]
    ranges = [(begin, end) for begin, end in ranges if begin and end]
    if code != APPLY_PERIOD or not ranges:
        return "unknown", "신청기간 공고 확인"
    for begin, end in sorted(ranges):
        if begin <= today <= end:
            return "open", f"{begin:%Y-%m-%d}~{end:%Y-%m-%d} 신청 중"
    upcoming = sorted(begin for begin, _ in ranges if begin > today)
    if upcoming:
        return "upcoming", f"{upcoming[0]:%Y-%m-%d}부터 신청"
    return "closed", f"마감({max(end for _, end in ranges):%Y-%m-%d})"


def _age_text(row: dict[str, Any]) -> str:
    low, high = _parse_int(row.get("sprtTrgtMinAge")) or 0, _parse_int(row.get("sprtTrgtMaxAge")) or 0
    if low == 0 and high == 0:
        return "제한없음" if _text(row, "sprtTrgtAgeLmtYn") == AGE_NO_LIMIT_FLAG else "공고 확인"
    if high == 0:
        return f"만 {low}세 이상"
    return f"만 {low}~{high}세"


def _income_text(row: dict[str, Any]) -> str:
    code = _text(row, "earnCndSeCd")
    if code == EARN_ANY:
        return "무관"
    if code == EARN_ANNUAL:
        low, high = _parse_int(row.get("earnMinAmt")) or 0, _parse_int(row.get("earnMaxAmt")) or 0
        return f"연소득 {low:,}만원 이상 ~ {high:,}만원 이하" if low else f"연소득 {high:,}만원 이하"
    return _preview(_text(row, "earnEtcCn")) or "공고 확인"


def _marriage_text(row: dict[str, Any]) -> str:
    return {MARRIED_ONLY: "기혼", SINGLE_ONLY: "미혼", MARRIAGE_ANY: "제한없음"}.get(
        _text(row, "mrgSttsCd"), "공고 확인"
    )


def _extra_conditions(row: dict[str, Any]) -> str:
    text = _text(row, "addAplyQlfcCndCn")
    return "" if text.replace(" ", "") in {t.replace(" ", "") for t in EMPTY_CONDITION_TEXTS} else _preview(text)


def _policy_url(row: dict[str, Any]) -> str:
    for key in ("aplyUrlAddr", "refUrlAddr1", "refUrlAddr2"):
        url = _text(row, key)
        if url.startswith(("http://", "https://")):
            return url
    return ""


def _policy_brief(row: dict[str, Any], today: date) -> dict[str, Any]:
    """LLM에 보내는 정책 요약. 원문 문장은 데이터일 뿐 지시가 아니다(AGENTS.md 5장 보안)."""
    _, period = _apply_period(row, today)
    return {
        "policy_id": _text(row, "plcyNo"),
        "name": _text(row, "plcyNm"),
        "category": _text(row, "mclsfNm"),
        "provider": _text(row, "operInstCdNm") or _text(row, "sprvsnInstCdNm"),
        "scope": "전국" if len(_zip_codes(row)) >= NATIONWIDE_MIN_CODES else "지역",
        "support": _preview(_text(row, "plcySprtCn") or _text(row, "plcyExplnCn")),
        "age": _age_text(row),
        "income": _income_text(row),
        "marriage": _marriage_text(row),
        "extra_conditions": _extra_conditions(row),
        "apply_period": period,
        "url": _policy_url(row),
    }


@dataclass(frozen=True)
class _UserRegion:
    """사용자 지역의 시군구코드. codes에 맞으면 거주지역 충족, sibling_codes에만 맞으면 확인 필요."""

    name: str
    codes: tuple[str, ...]  # 지금 코드 + 그 지역을 포함하던 옛 코드
    sibling_codes: tuple[str, ...] = ()  # 개편 때 함께 갈라진 이웃 구 코드 (SPLIT_SIBLING_CODES)

    @property
    def query_codes(self) -> list[str]:
        return [*self.codes, *self.sibling_codes]


def _region_codes(region: str) -> _UserRegion:
    """지역 → 정책 조회·판정에 쓸 시군구코드."""
    if not isinstance(region, str) or not region.strip():
        raise _PolicyError("region이 비어 있습니다. 매물 지역을 '서울 강서구 화곡동'처럼 넣어 다시 부르세요.")
    try:
        resolved = _resolve_region(region)
    except _RegionError as exc:
        # 지역 해석 오류 문구는 search_price용이라 시세 안내가 붙어 있다. 정책 도구에 맞는 안내로 바꾼다.
        message = str(exc).replace(_PRICE_REGION_HINT, _POLICY_REGION_HINT)
        raise _PolicyError(message) from None
    code = resolved.lawd_cd
    return _UserRegion(
        name=f"{resolved.sido} {resolved.sigungu}",
        codes=(code, *PREVIOUS_SIGUNGU_CODES.get(code, ())),
        sibling_codes=SPLIT_SIBLING_CODES.get(code, ()),
    )


# --------------------------------------------------------------------------- #
# 자격 판정
# --------------------------------------------------------------------------- #
def _compare_range(
    user: tuple[int, int | None], rule: tuple[int, int | None]
) -> str:
    """사용자 구간이 기준 구간 안이면 PASS, 전혀 겹치지 않으면 FAIL, 걸치면 UNKNOWN. None은 상한 없음."""
    user_low, user_high = user
    rule_low, rule_high = rule
    inside = rule_low <= user_low and (rule_high is None or (user_high is not None and user_high <= rule_high))
    if inside:
        return PASS
    below = user_high is not None and user_high < rule_low
    above = rule_high is not None and user_low > rule_high
    return FAIL if below or above else UNKNOWN


def _check_region(row: dict[str, Any], region: _UserRegion) -> _Check:
    codes = _zip_codes(row)
    if not codes:
        return _Check(UNKNOWN, "거주지역 조건이 비어 있음")
    if codes & set(region.codes):
        return _Check(PASS, "거주지역 충족")
    if codes & set(region.sibling_codes):
        return _Check(UNKNOWN, "개편 전 구 코드로 등록돼 대상 지역 확인 필요")
    if len(codes) >= NATIONWIDE_MIN_CODES:
        # 전국 정책인데 이 지역 코드만 빠진 경우는 코드표 누락일 수 있어 탈락시키지 않는다.
        return _Check(UNKNOWN, "전국 정책이지만 이 지역 코드가 없어 대상 지역 확인 필요")
    return _Check(FAIL, "매물 지역이 정책 대상 지역이 아님")


def _check_age(row: dict[str, Any], age_band: str | None) -> _Check:
    low, high = _parse_int(row.get("sprtTrgtMinAge")) or 0, _parse_int(row.get("sprtTrgtMaxAge")) or 0
    no_limit_flag = _text(row, "sprtTrgtAgeLmtYn") == AGE_NO_LIMIT_FLAG
    if low == 0 and high == 0:
        if no_limit_flag:
            return _Check(PASS, "나이 제한 없음")
        return _Check(UNKNOWN, "나이 조건이 비어 있어 공고 확인 필요")
    if age_band not in AGE_BANDS:
        return _Check(UNKNOWN, "나이 구간 정보가 없어 나이 조건 미판정")
    rule = (low, high if high else None)  # 상한 0은 상한 없음으로 본다
    result = _compare_range(AGE_BANDS[age_band], rule)
    label = _age_text(row)
    if result == PASS:
        return _Check(PASS, f"나이 조건({label}) 충족")
    if result == FAIL and no_limit_flag:
        # "제한없음" 표시와 나이 값이 함께 있으면 어느 쪽이 맞는지 알 수 없으므로 탈락시키지 않는다.
        return _Check(UNKNOWN, f"나이 조건 표시가 엇갈림(제한없음 / {label}), 공고 확인 필요")
    if result == FAIL:
        return _Check(FAIL, f"나이 조건({label}) 미충족")
    return _Check(UNKNOWN, f"나이 구간이 기준({label})에 걸쳐 있어 확인 필요")


def _has_tiered_income(row: dict[str, Any]) -> bool:
    """정책에 대상별(청년 외·신혼부부·부부합산 등) 소득 기준이 함께 적혀 있는지."""
    text = " ".join(_text(row, key) for key in ("plcyNm", "earnEtcCn", "addAplyQlfcCndCn", "plcySprtCn"))
    return bool(_TIERED_INCOME_PATTERN.search(text))


def _check_income(row: dict[str, Any], income_band: str | None) -> _Check:
    code = _text(row, "earnCndSeCd")
    if code == EARN_ANY:
        return _Check(PASS, "소득 조건 없음")
    if code != EARN_ANNUAL:
        text = _preview(_text(row, "earnEtcCn"), SUMMARY_NAME_CHARS * 2)
        return _Check(UNKNOWN, f"소득 기준이 문장으로만 있어 확인 필요: {text}" if text else "소득 기준 공고 확인 필요")
    low = _parse_int(row.get("earnMinAmt")) or 0
    high = _parse_int(row.get("earnMaxAmt")) or 0
    label = _income_text(row)
    if high <= 0 or low > high:
        return _Check(UNKNOWN, "소득 기준 값이 비정상이라 공고 확인 필요")
    if high < MIN_PLAUSIBLE_ANNUAL_INCOME_MANWON:
        return _Check(UNKNOWN, f"소득 기준({label})이 월소득일 수 있어 공고 확인 필요")
    if low > 0 or _has_tiered_income(row):
        # 하한이 있거나 대상별 기준이 보이면 이 값은 한 사람의 범위가 아니라 대상별 상한이다.
        return _Check(UNKNOWN, f"소득 기준이 대상(청년·신혼부부 등)별로 달라 확인 필요: {label}")
    if income_band not in INCOME_BANDS:
        return _Check(UNKNOWN, "소득 구간 정보가 없어 소득 조건 미판정")
    result = _compare_range(INCOME_BANDS[income_band], (low * WON_PER_MANWON, high * WON_PER_MANWON))
    if result == PASS:
        # 본인 소득인지 부부·가구 합산인지는 API에 없다. 미혼 청년은 본인 소득 기준과 같다.
        return _Check(PASS, f"소득 조건({label}) 충족")
    if result == FAIL:
        return _Check(FAIL, f"소득 조건({label}) 미충족")
    return _Check(UNKNOWN, f"소득 구간이 기준({label})에 걸쳐 있어 확인 필요")


def _check_marriage(row: dict[str, Any]) -> _Check:
    # 혼인 여부는 입력받지 않는다. 또 "청년·신혼부부" 정책에도 기혼 코드가 붙어 있어(2026-11-26 확인)
    # 이 코드만으로 탈락시킬 수 없다.
    code = _text(row, "mrgSttsCd")
    if code in (MARRIED_ONLY, SINGLE_ONLY):
        return _Check(UNKNOWN, f"{_marriage_text(row)} 대상 표시가 있어 혼인 여부 확인 필요")
    return _Check(PASS, "혼인 조건 없음")


def _check_targets(row: dict[str, Any]) -> _Check:
    restricted = [
        name
        for name, key, free in (
            ("취업상태", "jobCd", UNRESTRICTED_JOB_CODE),
            ("학력", "schoolCd", UNRESTRICTED_SCHOOL_CODE),
            ("특화분야", "sbizCd", UNRESTRICTED_SPECIAL_CODE),
        )
        if _codes(_text(row, key)) - {free}
    ]
    if restricted:
        return _Check(UNKNOWN, f"{'·'.join(restricted)} 조건이 있어 공고 확인 필요")
    return _Check(PASS, "취업상태·학력 제한 없음")


def _check_period(row: dict[str, Any], today: date) -> _Check:
    state, text = _apply_period(row, today)
    if state == "closed":
        return _Check(FAIL, f"신청 {text}")
    if state == "unknown":
        return _Check(UNKNOWN, text)
    return _Check(PASS, f"신청기간: {text}")


def _judge(
    row: dict[str, Any],
    region: _UserRegion,
    age_band: str | None,
    income_band: str | None,
    today: date,
) -> dict[str, Any]:
    checks = [
        _check_region(row, region),
        _check_age(row, age_band),
        _check_income(row, income_band),
        _check_marriage(row),
        _check_targets(row),
        _check_period(row, today),
    ]
    if any(check.result == FAIL for check in checks):
        status = STATUS_INELIGIBLE
    elif any(check.result == UNKNOWN for check in checks):
        status = STATUS_UNKNOWN
    else:
        status = STATUS_ELIGIBLE
    # 탈락·미확인 사유를 먼저 보여 준다.
    order = {FAIL: 0, UNKNOWN: 1, PASS: 2}
    reasons = [check.reason for check in sorted(checks, key=lambda c: order[c.result])]
    return {
        "policy_id": _text(row, "plcyNo"),
        "name": _text(row, "plcyNm"),
        "provider": _text(row, "operInstCdNm") or _text(row, "sprvsnInstCdNm"),
        "scope": "전국" if len(_zip_codes(row)) >= NATIONWIDE_MIN_CODES else "지역",
        "status": status,
        "reasons": reasons,
        "extra_conditions": _extra_conditions(row),
        "url": _policy_url(row),
    }


# --------------------------------------------------------------------------- #
# 도구
# --------------------------------------------------------------------------- #
def _fail(message: str) -> dict[str, Any]:
    return {"ok": False, "error": message}


def _clip(text: str) -> str:
    return text if len(text) <= SUMMARY_MAX_CHARS else text[: SUMMARY_MAX_CHARS - 1] + "…"


def _short(name: str) -> str:
    return name if len(name) <= SUMMARY_NAME_CHARS else name[: SUMMARY_NAME_CHARS - 1] + "…"


def _keyword_score(row: dict[str, Any], terms: list[str]) -> int:
    # 중분류명(mclsfNm)은 넣지 않는다. "전월세 및 주거급여 지원"이라 "월세"로 찾으면 그 분류 전체가 걸린다.
    haystack = " ".join(
        _text(row, key) for key in ("plcyNm", "plcyKywdNm", "plcyExplnCn", "plcySprtCn")
    ).replace(" ", "")
    return sum(1 for term in terms if term in haystack)


def _string_list(value: Any, field: str, limit: int) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise _PolicyError(f"{field}는 정책 ID 문자열의 목록이어야 합니다.")
    cleaned = list(dict.fromkeys(item.strip() for item in value if item.strip()))
    if len(cleaned) > limit:
        raise _PolicyError(f"{field}는 {limit}개 이하로 넣어 주세요.")
    return cleaned


def search_policy(
    region: str,
    keyword: str | None = None,
    exclude_ids: list[str] | None = None,
    include_closed: bool = False,
    max_results: int = DEFAULT_MAX_RESULTS,
) -> dict[str, Any]:
    """매물 지역에서 신청할 수 있는 청년 주거 지원 정책을 온통청년 API에서 찾는다.

    언제 쓰나: 사용자가 받을 수 있는 주거 지원(월세·전세자금 대출이자·보증료 지원, 공공임대 등)을
    찾을 때. 찾은 정책은 check_eligibility로 자격을 판정해야 보고서에 "해당"으로 넣을 수 있다.
    자격에서 탈락한 정책이 있으면 그 ID를 exclude_ids에 넣어 다른 정책을 다시 찾는다.
    쓰지 말 것: 시세·전세가율 계산이나 법·보증보험 근거 검색. 정책 이름·내용을 지어내지 않는다.

    Args:
        region: 매물 지역. 시/도·시/군/구까지 넣는다(예: "서울 강서구 화곡동"). 서울·인천·경기만 지원.
        keyword: 좁혀 찾을 말(예: "월세", "전세 이자", "보증료"). 띄어쓰기로 나눈 말 중 하나라도 들어간
            정책을 찾고 많이 맞는 순으로 정렬한다. 없으면 지역의 주거 정책 전체.
        exclude_ids: 결과에서 뺄 정책 ID 목록(이미 판정한 정책 등).
        include_closed: True면 신청이 마감된 정책도 포함한다. 기본 False.
        max_results: 돌려줄 정책 수(1~15, 기본 8).

    Returns:
        성공: {"ok": True, "data": {"region", "total_found", "matched", "policies": [...]}, "summary": ...}
            policies 항목: policy_id, name, category, provider, scope(전국/지역), support, age, income,
            marriage, extra_conditions, apply_period, url.
        실패: {"ok": False, "error": 원인과 다음 행동}
    """
    try:
        if not isinstance(max_results, int) or isinstance(max_results, bool) or not 1 <= max_results <= MAX_RESULTS:
            raise _PolicyError(f"max_results는 1~{MAX_RESULTS} 사이 정수로 넣어 주세요.")
        if keyword is not None and not isinstance(keyword, str):
            raise _PolicyError("keyword는 문자열이어야 합니다.")
        terms = [t for t in (keyword or "")[:MAX_KEYWORD_CHARS].split() if t]
        excluded = set(_string_list(exclude_ids, "exclude_ids", MAX_EXCLUDE_IDS))
        user_region = _region_codes(region)
        region_name = user_region.name
        today = _today()

        rows = _fetch_region_policies(user_region.query_codes)
        _remember(rows)
        total = len(rows)
        candidates = [row for row in rows if row["plcyNo"] not in excluded]
        if not include_closed:
            candidates = [row for row in candidates if _apply_period(row, today)[0] != "closed"]
        scored = [(row, _keyword_score(row, terms)) for row in candidates]
        if terms:
            scored = [(row, score) for row, score in scored if score > 0]
        if not scored:
            if total == 0:
                return _fail(f"{region_name}에 등록된 청년 주거 정책을 찾지 못했습니다. 정책은 빈 목록으로 두세요.")
            hint = "keyword를 바꾸거나 빼고" if terms else "include_closed=True로"
            return _fail(
                f"{region_name} 주거 정책 {total}건 중 조건에 맞는 정책이 없습니다(제외·마감 포함). {hint} 다시 찾으세요."
            )

        period_rank = {"open": 0, "upcoming": 1, "unknown": 2, "closed": 3}
        # 정렬: 키워드가 많이 맞는 순 → 지금 신청 가능한 순 → 지역 정책을 전국 정책보다 먼저 → 최근 수정순.
        # 파이썬 정렬은 안정 정렬이라, 최근 수정순으로 먼저 정렬한 뒤 앞의 기준으로 다시 정렬한다.
        scored.sort(key=lambda item: _text(item[0], "lastMdfcnDt"), reverse=True)
        scored.sort(key=lambda item: (
            -item[1],
            period_rank[_apply_period(item[0], today)[0]],
            len(_zip_codes(item[0])) >= NATIONWIDE_MIN_CODES,
        ))
        policies = [_policy_brief(row, today) for row, _ in scored[:max_results]]
        names = ", ".join(_short(p["name"]) for p in policies[:3])
        summary = (
            f"{region_name} 주거 정책 {total}건 중 {len(scored)}건 일치, {len(policies)}건 반환: {names}. "
            "check_eligibility로 자격 판정 필요"
        )
        return {
            "ok": True,
            "data": {
                "region": region_name,
                "total_found": total,
                "matched": len(scored),
                "policies": policies,
            },
            "summary": _clip(summary),
        }
    except _PolicyError as exc:
        return _fail(str(exc))
    except Exception as exc:
        logger.warning("search_policy 예상하지 못한 오류: %s", type(exc).__name__)
        return _fail("정책 검색 중 예상하지 못한 오류가 났습니다. 정책은 '확인 필요'로 두거나 한 번 더 시도하세요.")


def check_eligibility(
    policy_ids: list[str],
    region: str,
    age_band: str | None = None,
    income_band: str | None = None,
) -> dict[str, Any]:
    """정책별로 사용자의 자격을 판정한다(해당 / 확인 필요 / 해당 없음).

    판정 기준: 온통청년 API의 구조화된 조건(거주지역, 나이, 연소득, 혼인, 취업상태·학력, 신청기간).
    소득 기준이 문장으로만 있거나(예: 중위소득 %), 사용자 구간이 기준에 걸치거나, 표시가 서로 어긋나면
    지어내지 않고 "확인 필요"로 둔다. 하나라도 분명히 맞지 않으면 "해당 없음"이다.
    "해당"도 extra_conditions(무주택·자산 등 추가 조건 문장)는 따로 확인해야 한다.

    언제 쓰나: search_policy로 찾은 정책을 보고서에 넣기 전에 부른다.
    "해당 없음"이 나오면 그 ID를 search_policy의 exclude_ids에 넣어 다른 정책을 다시 찾는다.
    나이·소득 구간은 사용자 입력에서 자동으로 채워지므로 넘기지 않는다.
    쓰지 말 것: search_policy 결과에 없던 정책 ID를 지어내 넣지 않는다.

    Args:
        policy_ids: search_policy 결과의 policy_id 목록(1~10개).
        region: 매물 지역(search_policy에 넣은 값과 같게). 정책 대상 지역인지 판정한다.
        age_band: 나이 구간(AGE_BANDS 키). 에이전트 코어가 세션 값으로 채운다.
        income_band: 본인 연소득 구간(INCOME_BANDS 키). 에이전트 코어가 세션 값으로 채운다.

    Returns:
        성공: {"ok": True, "data": {"results": [...], "counts": {...}, "not_found": [...]}, "summary": ...}
            results 항목: policy_id, name, provider, scope(전국/지역), status, reasons, extra_conditions, url.
            전국 정책은 다른 지역 기관이 운영할 수 있으니 provider를 보고 사용자에게 맞는지 함께 판단한다.
        실패: {"ok": False, "error": 원인과 다음 행동}
    """
    try:
        ids = _string_list(policy_ids, "policy_ids", MAX_POLICY_IDS)
        if not ids:
            raise _PolicyError("policy_ids가 비어 있습니다. search_policy 결과의 policy_id를 넣어 다시 부르세요.")
        user_region = _region_codes(region)
        if age_band is not None and age_band not in AGE_BANDS:
            logger.warning("알 수 없는 나이 구간 이름이 들어왔습니다(값은 남기지 않음).")
        if income_band is not None and income_band not in INCOME_BANDS:
            logger.warning("알 수 없는 소득 구간 이름이 들어왔습니다(값은 남기지 않음).")
        today = _today()

        rows: list[dict[str, str]] = []
        not_found: list[str] = []
        for policy_id in ids:
            row = _POLICY_CACHE.get(policy_id)
            if row is None:
                row = _fetch_policy(policy_id)
                if row:
                    _remember([row])
            if row is None:
                not_found.append(policy_id)
            else:
                rows.append(row)
        if not rows:
            return _fail("넣은 정책 ID를 하나도 찾지 못했습니다. search_policy 결과의 policy_id를 그대로 넣으세요.")

        results = [_judge(row, user_region, age_band, income_band, today) for row in rows]
        counts = {
            status: sum(1 for r in results if r["status"] == status)
            for status in (STATUS_ELIGIBLE, STATUS_UNKNOWN, STATUS_INELIGIBLE)
        }
        eligible = [_short(r["name"]) for r in results if r["status"] == STATUS_ELIGIBLE]
        summary = (
            f"정책 {len(results)}건 판정: 해당 {counts[STATUS_ELIGIBLE]}, 확인 필요 {counts[STATUS_UNKNOWN]}, "
            f"해당 없음 {counts[STATUS_INELIGIBLE]}"
        )
        if eligible:
            summary += f" (해당: {', '.join(eligible[:2])})"
        if counts[STATUS_INELIGIBLE]:
            summary += ". 해당 없음은 exclude_ids로 빼고 다시 검색 가능"
        if not_found:
            summary += f". 못 찾은 ID {len(not_found)}개"
        return {
            "ok": True,
            "data": {"results": results, "counts": counts, "not_found": not_found},
            "summary": _clip(summary),
        }
    except _PolicyError as exc:
        return _fail(str(exc))
    except Exception as exc:
        logger.warning("check_eligibility 예상하지 못한 오류: %s", type(exc).__name__)
        return _fail("자격 판정 중 예상하지 못한 오류가 났습니다. 정책은 '확인 필요'로 두세요.")
