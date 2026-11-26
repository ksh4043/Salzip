"""외부 API 없이 search_policy, check_eligibility의 판정 규칙과 반환 형식을 검증한다.

온통청년 응답은 2026-11-26에 확인한 실제 JSON 형식을 흉내 낸 가짜 응답으로 대체한다.
정책 이름·번호·금액은 모두 가상이다.
"""

import os
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import requests

import tools.policy as policy

TODAY = date(2026, 11, 26)
SEOUL_GANGSEO = "11500"
SEOUL_MAPO = "11440"
HWASEONG_DONGTAN = "41597"
HWASEONG_OLD = "41590"
NATIONWIDE_ZIPS = ",".join(str(10000 + i) for i in range(policy.NATIONWIDE_MIN_CODES)) + "," + SEOUL_GANGSEO
FAKE_KEY = "test-key-0000"  # 가상 인증키
REGION = "서울 강서구 화곡동"
AGE = "25~29세"
INCOME = "3,000만원 초과~4,000만원 이하"


def _real_api_key() -> str | None:
    """실제 API 테스트용 키. 환경 변수 → 저장소의 .env 순서로 찾는다."""
    if os.getenv("YOUTHCENTER_API_KEY"):
        return os.getenv("YOUTHCENTER_API_KEY")
    try:
        from dotenv import dotenv_values
    except ImportError:
        return None
    return dotenv_values(Path(__file__).resolve().parents[1] / ".env").get("YOUTHCENTER_API_KEY") or None


REAL_API_KEY = _real_api_key()  # 아래 fixture가 가상 키로 덮기 전에 읽어 둔다


def make_policy(no: str, name: str = "가상 청년 월세 지원", **overrides: str) -> dict[str, str]:
    """온통청년 응답의 정책 한 건 (필요한 필드만). 기본값은 '조건 없음·상시·강서구'."""
    row = {
        "plcyNo": no,
        "plcyNm": name,
        "plcyKywdNm": "주거지원",
        "plcyExplnCn": "가상 정책 설명",
        "plcySprtCn": "가상 지원 내용",
        "lclsfNm": "주거",
        "mclsfNm": "전월세 및 주거급여 지원",
        "operInstCdNm": "가상시청",
        "sprvsnInstCdNm": "가상시청",
        "aplyPrdSeCd": policy.APPLY_ALWAYS,
        "aplyYmd": "",
        "sprtTrgtMinAge": "0",
        "sprtTrgtMaxAge": "0",
        "sprtTrgtAgeLmtYn": "Y",
        "mrgSttsCd": policy.MARRIAGE_ANY,
        "earnCndSeCd": policy.EARN_ANY,
        "earnMinAmt": "0",
        "earnMaxAmt": "0",
        "earnEtcCn": "",
        "addAplyQlfcCndCn": "",
        "jobCd": policy.UNRESTRICTED_JOB_CODE,
        "schoolCd": policy.UNRESTRICTED_SCHOOL_CODE,
        "sbizCd": policy.UNRESTRICTED_SPECIAL_CODE,
        "zipCd": SEOUL_GANGSEO,
        "aplyUrlAddr": "https://example.org/apply",
        "refUrlAddr1": "",
        "refUrlAddr2": "",
        "lastMdfcnDt": "2026-11-01 10:00:00",
    }
    row.update(overrides)
    return row


def ok_body(rows: list[dict[str, str]], total: int | None = None) -> dict[str, Any]:
    return {
        "resultCode": 200,
        "resultMessage": "성공적으로 데이터를 가지고 왔습니다.",
        "result": {
            "pagging": {"totCount": len(rows) if total is None else total, "pageNum": 1, "pageSize": 100},
            "youthPolicyList": rows,
        },
    }


class FakeResponse:
    def __init__(self, body: Any = None, status_code: int = 200, raw: str | None = None) -> None:
        self._body = body
        self.status_code = status_code
        self._raw = raw

    def json(self) -> Any:
        if self._raw is not None:
            raise ValueError("not json")
        return self._body


class FakeApi:
    """가짜 온통청년 API. zipCd·plcyNo·페이지 인자를 실제처럼 처리하고 받은 요청을 기록한다."""

    def __init__(self, rows: list[dict[str, str]]) -> None:
        self.rows = rows
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url: str, params: dict[str, Any], timeout: float) -> FakeResponse:
        assert url == policy.YOUTHCENTER_API_URL
        self.calls.append(dict(params))
        if "plcyNo" in params:
            return FakeResponse(ok_body([r for r in self.rows if r["plcyNo"] == params["plcyNo"]]))
        wanted = set(params["zipCd"].split(","))
        rows = [r for r in self.rows if wanted & set(r["zipCd"].split(","))]
        size, page = params["pageSize"], params["pageNum"]
        return FakeResponse(ok_body(rows[(page - 1) * size: page * size], total=len(rows)))


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("YOUTHCENTER_API_KEY", FAKE_KEY)
    monkeypatch.setattr(policy.time, "sleep", lambda _: None)
    monkeypatch.setattr(policy, "_today", lambda: TODAY)
    monkeypatch.setattr(policy, "_POLICY_CACHE", {})


def use_api(monkeypatch, rows: list[dict[str, str]]) -> FakeApi:
    api = FakeApi(rows)
    monkeypatch.setattr(policy.requests, "get", api)
    return api


GANGSEO_REGION = policy._UserRegion(name="서울 강서구", codes=(SEOUL_GANGSEO,))


def judge(row: dict[str, str], age: str | None = AGE, income: str | None = INCOME) -> dict[str, Any]:
    return policy._judge(row, GANGSEO_REGION, age, income, TODAY)


# --------------------------------------------------------------------------- #
# 구간 표
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bands", [policy.AGE_BANDS, policy.INCOME_BANDS])
def test_bands_are_contiguous_and_cover_everything(bands):
    ranges = list(bands.values())
    assert ranges[0][0] == 0
    for (_, high), (next_low, _) in zip(ranges, ranges[1:]):
        assert high is not None and next_low == high + 1
    assert ranges[-1][1] in (None, policy.AGE_BAND_OPEN_MAX)


@pytest.mark.parametrize(
    "user, rule, expected",
    [
        ((25, 29), (19, 39), policy.PASS),
        ((19, 24), (19, 34), policy.PASS),
        ((40, 44), (19, 39), policy.FAIL),
        ((0, 18), (19, 39), policy.FAIL),
        ((0, 18), (18, 39), policy.UNKNOWN),  # 18세는 되고 17세는 안 된다
        ((45, 200), (19, 45), policy.UNKNOWN),
        ((45, 200), (19, None), policy.PASS),
        ((100_000_001, None), (0, 50_000_000), policy.FAIL),
        ((100_000_001, None), (0, None), policy.PASS),
        ((0, 0), (0, 50_000_000), policy.PASS),
        ((0, 0), (50_000_000, 75_000_000), policy.FAIL),
    ],
)
def test_compare_range(user, rule, expected):
    assert policy._compare_range(user, rule) == expected


# --------------------------------------------------------------------------- #
# 조건별 판정
# --------------------------------------------------------------------------- #
def test_unrestricted_policy_is_eligible():
    result = judge(make_policy("P1"))
    assert result["status"] == policy.STATUS_ELIGIBLE
    assert set(result) == {
        "policy_id", "name", "provider", "scope", "status", "reasons", "extra_conditions", "url",
    }


@pytest.mark.parametrize(
    "low, high, flag, age, expected",
    [
        ("19", "39", "N", "25~29세", policy.STATUS_ELIGIBLE),
        ("19", "34", "N", "35~39세", policy.STATUS_INELIGIBLE),
        ("19", "34", "N", "19세 미만", policy.STATUS_INELIGIBLE),
        ("19", "45", "N", "45세 이상", policy.STATUS_UNKNOWN),
        # 화면은 "제한없음"인데 나이 값도 있으면 어느 쪽이 맞는지 몰라 탈락시키지 않는다
        ("19", "34", "Y", "35~39세", policy.STATUS_UNKNOWN),
        ("19", "34", "Y", "25~29세", policy.STATUS_ELIGIBLE),
        ("0", "0", "Y", "45세 이상", policy.STATUS_ELIGIBLE),
        ("0", "0", "N", "25~29세", policy.STATUS_UNKNOWN),  # 나이 칸이 비어 있음
        ("19", "0", "N", "45세 이상", policy.STATUS_ELIGIBLE),  # 상한 없음
        ("19", "39", "N", None, policy.STATUS_UNKNOWN),
        ("19", "39", "N", "알 수 없는 구간", policy.STATUS_UNKNOWN),
    ],
)
def test_age_rules(low, high, flag, age, expected):
    row = make_policy("P1", sprtTrgtMinAge=low, sprtTrgtMaxAge=high, sprtTrgtAgeLmtYn=flag)
    assert judge(row, age=age)["status"] == expected


@pytest.mark.parametrize(
    "code, low, high, income, expected",
    [
        (policy.EARN_ANY, "0", "0", "1억원 초과", policy.STATUS_ELIGIBLE),
        (policy.EARN_ANNUAL, "0", "5000", "4,000만원 초과~5,000만원 이하", policy.STATUS_ELIGIBLE),
        (policy.EARN_ANNUAL, "0", "5000", "5,000만원 초과~6,000만원 이하", policy.STATUS_INELIGIBLE),
        (policy.EARN_ANNUAL, "0", "4500", "4,000만원 초과~5,000만원 이하", policy.STATUS_UNKNOWN),
        (policy.EARN_ANNUAL, "", "5000", "소득 없음", policy.STATUS_ELIGIBLE),  # 하한이 빈 문자열
        # 하한이 있으면 범위가 아니라 대상별(청년·청년 외·신혼부부) 상한이라 탈락시키지 않는다
        (policy.EARN_ANNUAL, "5000", "7500", "2,000만원 이하", policy.STATUS_UNKNOWN),
        (policy.EARN_ANNUAL, "5000", "7500", "1억원 초과", policy.STATUS_UNKNOWN),
        (policy.EARN_ANNUAL, "0", "534", "2,000만원 이하", policy.STATUS_UNKNOWN),  # 월소득으로 보이는 값
        (policy.EARN_ANNUAL, "0", "0", "2,000만원 이하", policy.STATUS_UNKNOWN),  # 상한 없음 = 비정상
        (policy.EARN_ANNUAL, "0", "5000", None, policy.STATUS_UNKNOWN),
        (policy.EARN_OTHER, "0", "0", "2,000만원 이하", policy.STATUS_UNKNOWN),
        ("", "0", "0", "2,000만원 이하", policy.STATUS_UNKNOWN),
    ],
)
def test_income_rules(code, low, high, income, expected):
    row = make_policy("P1", earnCndSeCd=code, earnMinAmt=low, earnMaxAmt=high, earnEtcCn="기준 중위소득 60% 이하")
    assert judge(row, income=income)["status"] == expected


def test_income_text_is_shown_in_reason():
    row = make_policy("P1", earnCndSeCd=policy.EARN_OTHER, earnEtcCn="기준 중위소득 60% 이하")
    reasons = judge(row)["reasons"]
    assert "기준 중위소득 60% 이하" in reasons[0]


@pytest.mark.parametrize("code", [policy.MARRIED_ONLY, policy.SINGLE_ONLY])
def test_marriage_restriction_needs_check(code):
    # 혼인 여부는 입력받지 않고, 청년·신혼부부 겸용 정책에도 기혼 코드가 붙어 있어 탈락시키지 않는다
    assert judge(make_policy("P1", mrgSttsCd=code))["status"] == policy.STATUS_UNKNOWN


@pytest.mark.parametrize(
    "field, value",
    [("jobCd", "0013001,0013002"), ("schoolCd", "0049005"), ("sbizCd", "0014003,0014009")],
)
def test_target_restriction_needs_check(field, value):
    assert judge(make_policy("P1", **{field: value}))["status"] == policy.STATUS_UNKNOWN


@pytest.mark.parametrize(
    "code, ymd, expected_state",
    [
        (policy.APPLY_ALWAYS, "", "open"),
        (policy.APPLY_CLOSED, "", "closed"),
        (policy.APPLY_PERIOD, "20261101 ~ 20261231", "open"),
        (policy.APPLY_PERIOD, "20261126 ~ 20261126", "open"),
        (policy.APPLY_PERIOD, "20270101 ~ 20270131", "upcoming"),
        (policy.APPLY_PERIOD, "20260330 ~ 20260529", "closed"),
        (policy.APPLY_PERIOD, "20250101 ~ 20251231\\N20270101 ~ 20271231", "upcoming"),
        (policy.APPLY_PERIOD, "20260101 ~ 20260131\\N20261101 ~ 20261130", "open"),
        (policy.APPLY_PERIOD, "", "unknown"),
        (policy.APPLY_PERIOD, "20261399 ~ 20261231", "unknown"),
        ("", "", "unknown"),
    ],
)
def test_apply_period(code, ymd, expected_state):
    state, text = policy._apply_period(make_policy("P1", aplyPrdSeCd=code, aplyYmd=ymd), TODAY)
    assert state == expected_state
    assert text


def test_closed_policy_is_ineligible():
    row = make_policy("P1", aplyPrdSeCd=policy.APPLY_PERIOD, aplyYmd="20260330 ~ 20260529")
    result = judge(row)
    assert result["status"] == policy.STATUS_INELIGIBLE
    assert "마감" in result["reasons"][0]  # 탈락 사유가 맨 앞


def test_region_rules():
    assert judge(make_policy("P1", zipCd=SEOUL_MAPO))["status"] == policy.STATUS_INELIGIBLE
    assert judge(make_policy("P1", zipCd=""))["status"] == policy.STATUS_UNKNOWN
    assert judge(make_policy("P1", zipCd=f"{SEOUL_MAPO},{SEOUL_GANGSEO}"))["status"] == policy.STATUS_ELIGIBLE


def test_old_sigungu_code_matches_new_gu():
    region = policy._region_codes("경기 화성시 동탄구 오산동")
    assert region.query_codes == [HWASEONG_DONGTAN, HWASEONG_OLD]
    row = make_policy("P1", zipCd=HWASEONG_OLD)  # 옛 화성시 전체 코드
    assert policy._judge(row, region, AGE, INCOME, TODAY)["status"] == policy.STATUS_ELIGIBLE


INCHEON_CITYWIDE_OLD = "28125,28177,28185,28200,28237,28245,28290,28710,28720"  # 실제 인천시 전체 정책 모양


@pytest.mark.parametrize(
    "region_text, expected",
    [
        ("인천 영종구 운서동", policy.STATUS_UNKNOWN),  # 28155가 빠지고 28125(옛 중구)로만 등록
        ("인천 서해구 가정동", policy.STATUS_UNKNOWN),  # 28275가 빠지고 28290(옛 서구)으로만 등록
        ("인천 검단구 당하동", policy.STATUS_ELIGIBLE),
        ("인천 제물포구 송월동", policy.STATUS_ELIGIBLE),
    ],
)
def test_split_incheon_gu_is_not_rejected(region_text, expected):
    region = policy._region_codes(region_text)
    row = make_policy("P1", zipCd=INCHEON_CITYWIDE_OLD)
    assert policy._judge(row, region, AGE, INCOME, TODAY)["status"] == expected


def test_split_incheon_gu_queries_sibling_code():
    assert policy._region_codes("인천 영종구").query_codes == ["28155", "28110", "28125"]
    assert policy._region_codes("인천 서해구").query_codes == ["28275", "28260", "28290"]


def test_nationwide_policy_missing_user_code_needs_check():
    row = make_policy("P1", zipCd=",".join(str(10000 + i) for i in range(policy.NATIONWIDE_MIN_CODES)))
    assert judge(row)["status"] == policy.STATUS_UNKNOWN


def test_region_code_tables_use_current_codes():
    from tools.price import LAWD_CODES

    known = {code for table in LAWD_CODES.values() for code in table.values()}
    assert set(policy.PREVIOUS_SIGUNGU_CODES) <= known
    assert set(policy.SPLIT_SIBLING_CODES) <= known
    for olds in policy.PREVIOUS_SIGUNGU_CODES.values():
        assert not set(olds) & known  # 옛 코드는 지금 코드표에 없어야 한다
    for siblings in policy.SPLIT_SIBLING_CODES.values():
        assert set(siblings) <= known  # 이웃 구 코드는 지금 코드다


@pytest.mark.parametrize(
    "field, text",
    [
        ("addAplyQlfcCndCn", "연소득 (청년) 5천만원, (청년 외) 6천만원 이하"),
        ("plcyNm", "가상 청년 신혼부부 월세 지원"),
        ("plcySprtCn", "부부합산 연소득 기준 적용"),
    ],
)
def test_tiered_income_is_not_judged(field, text):
    # 인천 보증료 지원처럼 상한 6,000만원이 '청년 외' 기준이고 청년은 5,000만원인 경우: 통과시키면 안 된다
    row = make_policy("P1", earnCndSeCd=policy.EARN_ANNUAL, earnMaxAmt="6000", **{field: text})
    assert judge(row, income="5,000만원 초과~6,000만원 이하")["status"] == policy.STATUS_UNKNOWN


def test_extra_conditions_are_cleaned():
    assert judge(make_policy("P1", addAplyQlfcCndCn="해당 없음"))["extra_conditions"] == ""
    assert judge(make_policy("P1", addAplyQlfcCndCn="해당없음"))["extra_conditions"] == ""
    long_text = "무주택 세대주 " * 50
    extra = judge(make_policy("P1", addAplyQlfcCndCn=long_text))["extra_conditions"]
    assert extra.startswith("무주택 세대주") and len(extra) <= policy.PREVIEW_CHARS


# --------------------------------------------------------------------------- #
# search_policy
# --------------------------------------------------------------------------- #
def test_search_policy_requests_and_shape(monkeypatch):
    api = use_api(monkeypatch, [make_policy("P1"), make_policy("P2", name="가상 전세 이자 지원")])
    result = policy.search_policy(REGION)

    assert result["ok"] is True
    params = api.calls[0]
    assert params["apiKeyNm"] == FAKE_KEY
    assert params["rtnType"] == "json"
    assert params["lclsfNm"] == "주거"
    assert params["zipCd"] == SEOUL_GANGSEO
    data = result["data"]
    assert data["region"] == "서울 강서구"
    assert data["total_found"] == 2 and data["matched"] == 2
    assert set(data["policies"][0]) == {
        "policy_id", "name", "category", "provider", "scope", "support", "age", "income",
        "marriage", "extra_conditions", "apply_period", "url",
    }
    assert len(result["summary"]) <= policy.SUMMARY_MAX_CHARS
    assert FAKE_KEY not in str(result)


def test_search_policy_queries_old_codes_too(monkeypatch):
    api = use_api(monkeypatch, [make_policy("P1", zipCd=HWASEONG_OLD)])
    result = policy.search_policy("경기 화성시 동탄구")
    assert api.calls[0]["zipCd"] == f"{HWASEONG_DONGTAN},{HWASEONG_OLD}"
    assert result["data"]["total_found"] == 1


def test_search_policy_paginates(monkeypatch):
    rows = [make_policy(f"P{i:03d}") for i in range(policy.PAGE_SIZE + 5)]
    api = use_api(monkeypatch, rows)
    result = policy.search_policy(REGION)
    assert [c["pageNum"] for c in api.calls] == [1, 2]
    assert result["data"]["total_found"] == policy.PAGE_SIZE + 5


def test_search_policy_filters_closed_and_excluded(monkeypatch):
    use_api(monkeypatch, [
        make_policy("OPEN"),
        make_policy("CLOSED", aplyPrdSeCd=policy.APPLY_CLOSED),
        make_policy("EXPIRED", aplyPrdSeCd=policy.APPLY_PERIOD, aplyYmd="20260101 ~ 20260131"),
        make_policy("SEEN"),
    ])
    ids = [p["policy_id"] for p in policy.search_policy(REGION, exclude_ids=["SEEN"])["data"]["policies"]]
    assert ids == ["OPEN"]
    with_closed = policy.search_policy(REGION, include_closed=True)["data"]["policies"]
    assert {p["policy_id"] for p in with_closed} == {"OPEN", "CLOSED", "EXPIRED", "SEEN"}


def test_search_policy_keyword_and_order(monkeypatch):
    use_api(monkeypatch, [
        make_policy("NATION", name="가상 전국 월세 대출", zipCd=NATIONWIDE_ZIPS),
        make_policy("LOCAL", name="가상 구청 월세 지원"),
        make_policy("BOTH", name="가상 월세 보증료 지원", zipCd=NATIONWIDE_ZIPS),
        make_policy("SOON", name="가상 월세 지원 예정", aplyPrdSeCd=policy.APPLY_PERIOD, aplyYmd="20270101 ~ 20270131"),
        make_policy("OTHER", name="가상 기숙사"),
    ])
    result = policy.search_policy(REGION, keyword="월세 보증료")
    ids = [p["policy_id"] for p in result["data"]["policies"]]
    # 키워드 2개 맞음 → 신청 중인 지역 정책 → 신청 중인 전국 정책 → 신청 예정
    assert ids == ["BOTH", "LOCAL", "NATION", "SOON"]
    scopes = {p["policy_id"]: p["scope"] for p in result["data"]["policies"]}
    assert scopes["NATION"] == "전국" and scopes["LOCAL"] == "지역"


def test_search_policy_keyword_without_match(monkeypatch):
    use_api(monkeypatch, [make_policy("P1")])
    result = policy.search_policy(REGION, keyword="존재하지않는말")
    assert result["ok"] is False and "keyword" in result["error"]


def test_search_policy_empty_region(monkeypatch):
    use_api(monkeypatch, [])
    result = policy.search_policy(REGION)
    assert result["ok"] is False and "빈 목록" in result["error"]


def test_search_policy_max_results(monkeypatch):
    use_api(monkeypatch, [make_policy(f"P{i}") for i in range(20)])
    assert len(policy.search_policy(REGION, max_results=3)["data"]["policies"]) == 3
    for bad in (0, policy.MAX_RESULTS + 1, True, "3"):
        assert policy.search_policy(REGION, max_results=bad)["ok"] is False


@pytest.mark.parametrize(
    "region, needle",
    [("", "region"), ("부산 해운대구", "지원하지 않습니다"), ("인천 중구", "제물포구")],
)
def test_search_policy_region_errors(monkeypatch, region, needle):
    api = use_api(monkeypatch, [])
    result = policy.search_policy(region)
    assert result["ok"] is False and needle in result["error"]
    assert "시세" not in result["error"]  # 시세 도구용 안내가 섞이지 않는다
    assert api.calls == []


def test_search_policy_bad_arguments(monkeypatch):
    use_api(monkeypatch, [make_policy("P1")])
    assert policy.search_policy(REGION, keyword=123)["ok"] is False
    assert policy.search_policy(REGION, exclude_ids="P1")["ok"] is False
    assert policy.search_policy(REGION, exclude_ids=[1])["ok"] is False


def test_missing_api_key(monkeypatch):
    monkeypatch.delenv("YOUTHCENTER_API_KEY")
    api = use_api(monkeypatch, [make_policy("P1")])
    result = policy.search_policy(REGION)
    assert result["ok"] is False and "YOUTHCENTER_API_KEY" in result["error"]
    assert api.calls == []


def test_auth_error(monkeypatch):
    monkeypatch.setattr(
        policy.requests, "get",
        lambda *a, **k: FakeResponse({"errorCode": "e001", "errorMsg": "invalid api key."}),
    )
    result = policy.search_policy(REGION)
    assert result["ok"] is False and "인증키" in result["error"]


def test_retries_server_error_then_succeeds(monkeypatch):
    responses = [FakeResponse(status_code=503), FakeResponse(ok_body([make_policy("P1")]))]
    monkeypatch.setattr(policy.requests, "get", lambda *a, **k: responses.pop(0))
    assert policy.search_policy(REGION)["ok"] is True


def test_gives_up_after_timeouts(monkeypatch):
    calls = []

    def timeout(*args, **kwargs):
        calls.append(1)
        raise requests.Timeout("slow")

    monkeypatch.setattr(policy.requests, "get", timeout)
    result = policy.search_policy(REGION)
    assert result["ok"] is False and "응답하지 않습니다" in result["error"]
    assert len(calls) == policy.MAX_RETRIES + 1


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse(raw="<html>"),
        FakeResponse(["not", "a", "dict"]),
        FakeResponse({"resultCode": 500, "resultMessage": "오류"}),
    ],
)
def test_bad_responses_become_failures(monkeypatch, response):
    monkeypatch.setattr(policy.requests, "get", lambda *a, **k: response)
    result = policy.search_policy(REGION)
    assert result["ok"] is False and result["error"]


def test_api_error_code_is_retried(monkeypatch):
    responses = [FakeResponse({"resultCode": 500, "resultMessage": "오류"}), FakeResponse(ok_body([make_policy("P1")]))]
    monkeypatch.setattr(policy.requests, "get", lambda *a, **k: responses.pop(0))
    assert policy.search_policy(REGION)["ok"] is True


def test_unexpected_exception_is_caught(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("bug")

    monkeypatch.setattr(policy, "_fetch_region_policies", boom)
    assert policy.search_policy(REGION)["ok"] is False
    monkeypatch.setattr(policy, "_judge", boom)
    monkeypatch.setattr(policy, "_POLICY_CACHE", {"P1": make_policy("P1")})
    assert policy.check_eligibility(["P1"], REGION, AGE, INCOME)["ok"] is False


# --------------------------------------------------------------------------- #
# check_eligibility
# --------------------------------------------------------------------------- #
def test_check_uses_search_cache(monkeypatch):
    api = use_api(monkeypatch, [
        make_policy("OK"),
        make_policy("LOW", earnCndSeCd=policy.EARN_ANNUAL, earnMaxAmt="3000"),
        make_policy("TEXT", earnCndSeCd=policy.EARN_OTHER, earnEtcCn="중위소득 150% 이하"),
    ])
    policy.search_policy(REGION)
    calls_after_search = len(api.calls)

    result = policy.check_eligibility(["OK", "LOW", "TEXT"], REGION, age_band=AGE, income_band=INCOME)
    assert len(api.calls) == calls_after_search  # 다시 부르지 않는다
    assert result["ok"] is True
    statuses = {r["policy_id"]: r["status"] for r in result["data"]["results"]}
    assert statuses == {
        "OK": policy.STATUS_ELIGIBLE, "LOW": policy.STATUS_INELIGIBLE, "TEXT": policy.STATUS_UNKNOWN,
    }
    assert result["data"]["counts"] == {"해당": 1, "확인 필요": 1, "해당 없음": 1}
    assert "exclude_ids" in result["summary"]
    assert len(result["summary"]) <= policy.SUMMARY_MAX_CHARS


def test_check_fetches_unknown_ids_and_reports_missing(monkeypatch):
    api = use_api(monkeypatch, [make_policy("P1")])
    result = policy.check_eligibility(["P1", "NOPE"], REGION, age_band=AGE, income_band=INCOME)
    assert [c.get("plcyNo") for c in api.calls] == ["P1", "NOPE"]
    assert result["ok"] is True
    assert result["data"]["not_found"] == ["NOPE"]
    assert [r["policy_id"] for r in result["data"]["results"]] == ["P1"]


def test_check_all_missing_is_failure(monkeypatch):
    use_api(monkeypatch, [])
    result = policy.check_eligibility(["NOPE"], REGION, age_band=AGE, income_band=INCOME)
    assert result["ok"] is False and "policy_id" in result["error"]


@pytest.mark.parametrize(
    "ids",
    [[], None, "P1", [1], [f"P{i}" for i in range(policy.MAX_POLICY_IDS + 1)]],
)
def test_check_bad_ids(monkeypatch, ids):
    use_api(monkeypatch, [make_policy("P1")])
    assert policy.check_eligibility(ids, REGION, age_band=AGE, income_band=INCOME)["ok"] is False


def test_check_dedupes_ids(monkeypatch):
    use_api(monkeypatch, [make_policy("P1")])
    result = policy.check_eligibility(["P1", " P1 ", "P1"], REGION, age_band=AGE, income_band=INCOME)
    assert len(result["data"]["results"]) == 1


def test_check_without_bands_does_not_guess(monkeypatch):
    use_api(monkeypatch, [make_policy(
        "P1", sprtTrgtMinAge="19", sprtTrgtMaxAge="34", sprtTrgtAgeLmtYn="N",
        earnCndSeCd=policy.EARN_ANNUAL, earnMaxAmt="5000",
    )])
    result = policy.check_eligibility(["P1"], REGION)
    assert result["data"]["results"][0]["status"] == policy.STATUS_UNKNOWN


def test_check_result_does_not_echo_raw_bands(monkeypatch):
    use_api(monkeypatch, [make_policy("P1", earnCndSeCd=policy.EARN_ANNUAL, earnMaxAmt="5000")])
    result = policy.check_eligibility(["P1"], REGION, age_band=AGE, income_band=INCOME)
    text = str(result)
    assert AGE not in text and INCOME not in text  # AGENTS.md 4장: 원본 구간은 LLM에 보내지 않는다


def test_api_key_is_redacted_from_urllib3_log():
    import logging

    record = logging.LogRecord(
        "urllib3.connectionpool", logging.DEBUG, __file__, 1,
        'GET /go/ythip/getPlcy?apiKeyNm=%s&pageNum=1 HTTP/1.1', (FAKE_KEY,), None,
    )
    policy._RedactApiKeyFilter().filter(record)
    assert FAKE_KEY not in record.getMessage()
    assert "apiKeyNm=***" in record.getMessage()


def test_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(policy, "CACHE_MAX_POLICIES", 3)
    policy._remember([make_policy(f"P{i}") for i in range(5)])
    assert list(policy._POLICY_CACHE) == ["P2", "P3", "P4"]


# --------------------------------------------------------------------------- #
# 실제 API (pytest -m external)
# --------------------------------------------------------------------------- #
@pytest.mark.external
def test_real_api_search_and_check(monkeypatch):
    if not REAL_API_KEY:
        pytest.skip("YOUTHCENTER_API_KEY가 없어 실제 API 테스트를 건너뜀")
    monkeypatch.setenv("YOUTHCENTER_API_KEY", REAL_API_KEY)
    monkeypatch.setattr(policy, "_today", date.today)

    found = policy.search_policy("서울 강서구 화곡동", keyword="월세", max_results=5)
    assert found["ok"] is True, found
    assert found["data"]["policies"]
    for item in found["data"]["policies"]:
        assert item["policy_id"] and item["name"]

    ids = [p["policy_id"] for p in found["data"]["policies"]]
    checked = policy.check_eligibility(ids, "서울 강서구 화곡동", age_band="25~29세", income_band="2,000만원 이하")
    assert checked["ok"] is True, checked
    assert {r["status"] for r in checked["data"]["results"]} <= {"해당", "확인 필요", "해당 없음"}

    # 캐시에 없는 정책은 policy_id로 다시 받아 온다
    monkeypatch.setattr(policy, "_POLICY_CACHE", {})
    again = policy.check_eligibility(ids[:1], "서울 강서구 화곡동", age_band="25~29세", income_band="2,000만원 이하")
    assert again["ok"] is True and again["data"]["not_found"] == []
