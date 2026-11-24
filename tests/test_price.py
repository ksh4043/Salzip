"""외부 API 없이 search_price, calc_jeonse_ratio의 계산과 반환 형식을 검증한다.

국토부 응답은 실제 XML 형식을 흉내 낸 가짜 응답으로 대체한다.
거래 데이터와 건물명은 모두 가상이다.
"""

import logging
import os
from datetime import date
from pathlib import Path

import pytest
import requests

import tools.price as price

TODAY = date(2026, 11, 17)
REAL_YMDS = price._recent_deal_ymds  # 아래 fixture가 바꾸기 전 원본


def _real_api_key() -> str | None:
    """실제 API 테스트용 키. 환경 변수 → 저장소의 .env 순서로 찾는다."""
    if os.getenv("MOLIT_API_KEY"):
        return os.getenv("MOLIT_API_KEY")
    try:
        from dotenv import dotenv_values
    except ImportError:
        return None
    return dotenv_values(Path(__file__).resolve().parents[1] / ".env").get("MOLIT_API_KEY") or None


REAL_API_KEY = _real_api_key()  # 아래 fixture가 가상 키로 덮기 전에 읽어 둔다
RAW_KEY = "test+key/value=="  # 가상 인증키 (base64 특수문자 포함)
ENCODED_KEY = "test%2Bkey%2Fvalue%3D%3D"
EOK = 100_000_000


# --------------------------------------------------------------------------- #
# 가짜 국토부 응답
# --------------------------------------------------------------------------- #
def sale_item(amount, dong="역삼동", area="84.97", name="가상힐스", day=10, cancel=""):
    return f"""<item>
      <aptNm>{name}</aptNm><buildYear>2015</buildYear><cdealType>{cancel}</cdealType>
      <dealAmount>{amount}</dealAmount><dealDay>{day}</dealDay><dealMonth>10</dealMonth>
      <dealYear>2026</dealYear><excluUseAr>{area}</excluUseAr><floor>12</floor>
      <jibun>1</jibun><sggCd>11680</sggCd><umdNm>{dong}</umdNm>
    </item>"""


def rent_item(deposit, monthly="0", dong="역삼동", area="84.97", name="가상힐스", day=5):
    return f"""<item>
      <aptNm>{name}</aptNm><buildYear>2015</buildYear><dealDay>{day}</dealDay>
      <dealMonth>10</dealMonth><dealYear>2026</dealYear><deposit>{deposit}</deposit>
      <excluUseAr>{area}</excluUseAr><floor>7</floor><monthlyRent>{monthly}</monthlyRent>
      <sggCd>11680</sggCd><umdNm>{dong}</umdNm>
    </item>"""


def ok_xml(items, total=None):
    total = len(items) if total is None else total
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<response>
  <header><resultCode>000</resultCode><resultMsg>OK</resultMsg></header>
  <body><items>{''.join(items)}</items>
    <numOfRows>1000</numOfRows><pageNo>1</pageNo><totalCount>{total}</totalCount></body>
</response>"""


def gateway_error(err, auth, code):
    """apis.data.go.kr 게이트웨이 오류 본문 (2026-11-24 실제 응답 형식)."""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<OpenAPI_ServiceResponse>
<cmmMsgHeader>
  <errMsg>{err}</errMsg>
  <returnAuthMsg>{auth}</returnAuthMsg>
  <returnReasonCode>{code}</returnReasonCode>
</cmmMsgHeader>
</OpenAPI_ServiceResponse>"""


# 매매: 같은 단지 3건(약 10억) + 다른 동 1건 + 해제 거래 1건
SALES = [
    sale_item(" 100,000 ", day=3),
    sale_item("102,000", day=20),
    sale_item("  98,000", day=11),
    sale_item("250,000", dong="대치동", name="다른단지"),
    sale_item("50,000", cancel="O"),
]
# 전세: 3건(약 6억) + 월세 1건
RENTS = [rent_item(" 60,000 "), rent_item("62,000"), rent_item("58,000"), rent_item("10,000", monthly="150")]


class FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


class FakeApi:
    """requests.get 대체. 호출 기록을 남기고 handler가 응답을 정한다."""

    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def __call__(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        return self.handler(url, params or {})


def is_rent(url):
    return "Rent" in url


def data_month_handler(sales=SALES, rents=RENTS, data_month="202610"):
    """data_month에만 거래가 있고 나머지 달은 0건."""

    def handler(url, params):
        if params["DEAL_YMD"] != data_month:
            return FakeResponse(text=ok_xml([]))
        return FakeResponse(text=ok_xml(rents if is_rent(url) else sales))

    return handler


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("MOLIT_API_KEY", RAW_KEY)
    monkeypatch.setattr(price.time, "sleep", lambda _: None)
    monkeypatch.setattr(price, "_recent_deal_ymds", lambda months, today=None: REAL_YMDS(months, TODAY))


def use_api(monkeypatch, handler):
    api = FakeApi(handler)
    monkeypatch.setattr(price.requests, "get", api)
    return api


def run(monkeypatch, handler, **kwargs):
    use_api(monkeypatch, handler)
    params = {"region": "서울 강남구 역삼동", "exclusive_area_m2": 84.9, "months": 3}
    params.update(kwargs)
    return price.search_price(**params)


def assert_success_shape(result):
    assert set(result) == {"ok", "data", "summary"}
    assert result["ok"] is True
    assert 0 < len(result["summary"]) <= price.SUMMARY_MAX_CHARS


def assert_failure_shape(result):
    assert set(result) == {"ok", "error"}
    assert result["ok"] is False
    assert result["error"]


# --------------------------------------------------------------------------- #
# 지역 → 법정동코드
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("region", "code", "dong"),
    [
        ("서울특별시 강남구 역삼동", "11680", "역삼동"),
        ("서울 강남구 역삼1동", "11680", "역삼동"),  # 행정동 → 법정동
        ("서울 중구 을지로1가", "11140", "을지로1가"),
        ("인천광역시 영종구 운서동", "28155", "운서동"),  # 2026년 개편 뒤 코드 (실제 API로 확인)
        ("인천 서해구 가정동", "28275", "가정동"),
        ("인천 검단구 마전동", "28290", "마전동"),
        ("인천 제물포구 도원동", "28125", "도원동"),
        ("경기 화성시 동탄구 반송동", "41597", "반송동"),
        ("화성 병점구", "41595", None),
        ("경기 부천시 원미구 중동", "41192", "중동"),
        ("경기도 성남시 분당구 정자동 가상타워 상가", "41135", "정자동"),
        ("성남 분당구", "41135", None),
        ("분당구 정자동", "41135", "정자동"),
        ("경기 남양주시 화도읍 묵현리", "41360", "화도읍"),
        ("마포구", "11440", None),
    ],
)
def test_resolve_region(region, code, dong):
    place = price._resolve_region(region)
    assert (place.lawd_cd, place.dong) == (code, dong)


@pytest.mark.parametrize(
    ("region", "needle"),
    [
        ("중구 명동", "여러 곳"),  # 서울 중구 / 옛 인천 중구
        ("경기 수원시", "여러 곳"),  # 구 누락
        ("경기 화성시 반송동", "여러 곳"),  # 2026년부터 화성시도 구가 있다
        ("인천 서구 청라동", "서해구 또는 검단구"),  # 옛 이름이면 새 이름을 알려 준다
        ("인천광역시 중구 운서동", "영종구"),
        ("인천 동구 송림동", "제물포구"),
        ("서구 가정동", "서해구 또는 검단구"),
        ("부산 해운대구 우동", "지원하지 않습니다"),
        ("역삼동", "시/군/구를 찾지 못했습니다"),
    ],
)
def test_resolve_region_errors(region, needle):
    with pytest.raises(price._ToolError, match=needle):
        price._resolve_region(region)


def test_lawd_codes_are_unique_five_digits():
    codes = [code for table in price.LAWD_CODES.values() for code in table.values()]
    assert all(len(c) == 5 and c.isdigit() for c in codes)
    assert len(codes) == len(set(codes))
    retired = {"28110", "28140", "28260", "41590"}  # 2026년 개편으로 거래가 0건인 옛 코드
    assert not retired & set(codes)


def test_register_lawd_codes(monkeypatch):
    monkeypatch.setattr(price, "LAWD_CODES", {k: dict(v) for k, v in price.LAWD_CODES.items()})
    monkeypatch.setattr(price, "SIDO_ALIASES", dict(price.SIDO_ALIASES))
    price.register_lawd_codes("부산", {"해운대구": "26350"}, aliases=("부산광역시",))
    assert price._resolve_region("부산광역시 해운대구 우동").lawd_cd == "26350"
    with pytest.raises(ValueError):
        price.register_lawd_codes("부산", {"수영구": "265"})


def test_recent_deal_ymds_crosses_year():
    assert REAL_YMDS(3, TODAY) == ["202611", "202610", "202609"]
    assert REAL_YMDS(4, date(2026, 2, 1)) == ["202602", "202601", "202512", "202511"]


# --------------------------------------------------------------------------- #
# XML 정제
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("raw", "expected"),
    [(" 85,000 ", 85000), ("1,234,567", 1234567), ("", None), ("  ", None), (None, None), ("-", None)],
)
def test_parse_int(raw, expected):
    assert price._parse_int(raw) == expected


def test_to_deal_converts_manwon_to_won():
    rows, total = price._parse_response(ok_xml([sale_item(" 85,000 ")]))
    deal = price._to_deal(rows[0], "매매")
    assert total == 1
    assert deal.price_won == 850_000_000
    assert (deal.deal_date, deal.dong, deal.building_name, deal.area_m2, deal.floor, deal.build_year) == (
        date(2026, 10, 10), "역삼동", "가상힐스", 84.97, 12, 2015)


def test_to_deal_filters_and_missing_values():
    to_deal = price._to_deal
    assert to_deal({"deposit": "10,000", "monthlyRent": "50"}, "전세") is None  # 월세
    assert to_deal({"dealAmount": "90,000", "cdealType": "O"}, "매매") is None  # 해제
    assert to_deal({"dealAmount": "  "}, "매매") is None  # 금액 결측
    deal = to_deal({"deposit": " 30,000 ", "monthlyRent": " ", "floor": "", "dealDay": "x"}, "전세")
    assert deal.price_won == 300_000_000
    assert (deal.deal_date, deal.dong, deal.area_m2, deal.floor) == (None, None, None, None)
    legacy = {"거래금액": " 82,500", "법정동": " 역삼동", "아파트": "가상빌", "전용면적": "59.9",
              "년": "2026", "월": "10", "일": "2"}
    deal = to_deal(legacy, "매매")
    assert (deal.price_won, deal.dong, deal.building_name, deal.deal_date) == (
        825_000_000, "역삼동", "가상빌", date(2026, 10, 2))


def test_villa_rows_use_mhouse_name(monkeypatch):
    """실제 연립다세대 응답은 건물명을 mhouseNm으로 준다 (2026-11-24 실제 응답 형식)."""
    item = """<item><buildYear>2017</buildYear><cdealType> </cdealType><dealAmount>   75,000</dealAmount>
      <dealDay>28</dealDay><dealMonth>10</dealMonth><dealYear>2026</dealYear><excluUseAr>44.85</excluUseAr>
      <floor>4</floor><houseType>다세대</houseType><mhouseNm>가상빌라</mhouseNm><umdNm>역삼동</umdNm></item>"""
    rows, _ = price._parse_response(ok_xml([item]))
    deal = price._to_deal(rows[0], "매매")
    assert (deal.price_won, deal.building_name, deal.area_m2, deal.dong) == (750_000_000, "가상빌라", 44.85, "역삼동")

    def handler(url, params):
        if params["DEAL_YMD"] != "202610" or is_rent(url):
            return FakeResponse(text=ok_xml([]))
        return FakeResponse(text=ok_xml([item] * 3))

    result = run(monkeypatch, handler, housing_type="빌라", building_name="가상빌라", exclusive_area_m2=45)
    assert result["data"]["sale"]["sample_basis"] == "같은 단지·전용면적 ±5㎡"
    assert result["data"]["market_price_won"] == 750_000_000


def test_parse_response_errors():
    with pytest.raises(price._AuthError):
        price._parse_response(gateway_error("SERVICE_KEY_IS_NOT_REGISTERED_ERROR", "등록되지 않은 서비스키", "30"))
    with pytest.raises(price._ToolError, match="한도"):
        price._parse_response(gateway_error("LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR", "한도 초과", "22"))
    with pytest.raises(price._ToolError, match="XML이 아닌"):
        price._parse_response("Unexpected errors")
    nodata = "<response><header><resultCode>03</resultCode><resultMsg>NODATA_ERROR</resultMsg></header></response>"
    assert price._parse_response(nodata) == ([], 0)


def test_encoded_key_is_sent_decoded(monkeypatch):
    monkeypatch.setenv("MOLIT_API_KEY", f"  {ENCODED_KEY}\n")
    api = use_api(monkeypatch, lambda url, params: FakeResponse(text=ok_xml([])))
    price.search_price("서울 강남구 역삼동", months=3)
    assert len(api.calls) == 6  # (매매 + 전월세) × 3개월
    for url, params in api.calls:
        assert params["serviceKey"] == RAW_KEY  # requests가 한 번만 인코딩한다
        assert params["LAWD_CD"] == "11680"
    assert {p["DEAL_YMD"] for _, p in api.calls} == {"202611", "202610", "202609"}
    assert sum(is_rent(url) for url, _ in api.calls) == 3


def test_endpoints_by_housing_type(monkeypatch):
    api = use_api(monkeypatch, lambda url, params: FakeResponse(text=ok_xml([])))
    price.search_price("서울 강남구 역삼동", housing_type="아파트", months=1)
    price.search_price("서울 강남구 역삼동", housing_type="빌라", months=1)
    assert {url.rsplit("/", 1)[-1] for url, _ in api.calls} == {
        "getRTMSDataSvcAptTradeDev", "getRTMSDataSvcAptRent",
        "getRTMSDataSvcRHTrade2", "getRTMSDataSvcRHRent",
    }


@pytest.mark.parametrize("housing_type", ["오피스텔", " 오피스텔 "])
def test_officetel_is_out_of_scope(monkeypatch, housing_type):
    """팀 합의로 오피스텔은 뺐다. API를 부르지 않고 이유와 다음 행동을 알려 준다."""
    api = use_api(monkeypatch, lambda url, params: FakeResponse(text=ok_xml([])))
    result = price.search_price("서울 강남구 역삼동", housing_type=housing_type)
    assert_failure_shape(result)
    assert "오피스텔은 이 도구가 지원하지 않습니다" in result["error"]
    assert "사용자에게 직접 확인" in result["error"]
    assert api.calls == []
    assert set(price.MOLIT_ENDPOINTS) == {"아파트", "연립다세대"}


def test_pagination_collects_all_pages(monkeypatch):
    def handler(url, params):
        if is_rent(url):
            return FakeResponse(text=ok_xml([]))
        pages = {1: [sale_item("100,000"), sale_item("101,000")], 2: [sale_item("102,000")]}
        return FakeResponse(text=ok_xml(pages.get(params["pageNo"], []), total=3))

    result = run(monkeypatch, handler, months=1)
    assert result["data"]["sale"]["count"] == 3


# --------------------------------------------------------------------------- #
# search_price
# --------------------------------------------------------------------------- #
def test_search_price_same_complex(monkeypatch):
    result = run(monkeypatch, data_month_handler(), building_name="가상힐스")
    assert_success_shape(result)
    data = result["data"]
    assert data["region"] == {"lawd_cd": "11680", "sigungu": "서울 강남구", "dong": "역삼동"}
    # 다른 동·해제 거래·월세가 빠지고 3건씩 남는다. 금액은 원 단위.
    assert data["sale"] == {
        "count": 3, "mean_won": 10 * EOK, "median_won": 10 * EOK,
        "min_won": 980_000_000, "max_won": 1_020_000_000,
        "outliers_removed": 0, "sample_basis": "같은 단지·전용면적 ±5㎡",
    }
    assert (data["jeonse"]["count"], data["jeonse"]["mean_won"]) == (3, 6 * EOK)
    assert data["market_price_won"] == 10 * EOK
    assert data["market_jeonse_ratio_pct"] == 60.0
    assert data["warnings"] == []
    assert [d["deal_date"] for d in data["recent_sales"]] == ["2026-10-20", "2026-10-11", "2026-10-03"]
    assert "jibun" not in data["recent_sales"][0]  # 번지는 넘기지 않는다 (AGENTS.md 4장)
    assert "매매 3건" in result["summary"] and "전세가율 60.0%" in result["summary"]


def test_search_price_complex_changes_market_price(monkeypatch):
    """같은 동에 비싼 신축이 섞여 있어도 건물명을 주면 그 단지 시세를 쓴다."""
    sales = [sale_item("50,000", name="가상빌라", day=d) for d in (1, 2, 3)]
    sales += [sale_item("150,000", name="가상신축", day=d) for d in (4, 5, 6)]
    handler = data_month_handler(sales=sales)

    by_name = run(monkeypatch, handler, building_name="가상 빌라")
    assert by_name["data"]["sale"]["sample_basis"] == "같은 단지·전용면적 ±5㎡"
    assert by_name["data"]["market_price_won"] == 5 * EOK

    by_dong = run(monkeypatch, handler)
    assert by_dong["data"]["sale"]["sample_basis"] == "같은 동·전용면적 ±5㎡"
    assert by_dong["data"]["market_price_won"] == 10 * EOK


def test_search_price_unknown_building_falls_back(monkeypatch):
    result = run(monkeypatch, data_month_handler(), building_name="없는단지")
    assert result["data"]["sale"]["sample_basis"] == "같은 동·전용면적 ±5㎡"
    assert any("같은 단지의 매매 거래가 부족" in w for w in result["data"]["warnings"])


def test_search_price_no_deals(monkeypatch):
    result = run(monkeypatch, lambda url, params: FakeResponse(text=ok_xml([])))
    assert_failure_shape(result)
    assert result["error"].startswith("해당 지역 거래 없음")
    assert "months" in result["error"]


def test_search_price_no_sales_but_jeonse(monkeypatch):
    result = run(monkeypatch, data_month_handler(sales=[]))
    assert_success_shape(result)
    assert result["data"]["market_price_won"] is None
    assert result["data"]["market_jeonse_ratio_pct"] is None
    assert "전세가율 계산 불가" in result["summary"]


def test_search_price_missing_area(monkeypatch):
    result = run(monkeypatch, data_month_handler(), exclusive_area_m2=None)
    assert result["data"]["sale"]["sample_basis"] == "같은 동·면적 무관"
    assert result["data"]["market_price_won"] == 10 * EOK
    assert any("전용면적이 없어" in w for w in result["data"]["warnings"])


def test_search_price_without_dong_uses_sigungu(monkeypatch):
    result = run(monkeypatch, data_month_handler(), region="서울 강남구")
    assert result["data"]["sale"]["sample_basis"] == "같은 시군구·전용면적 ±5㎡"
    assert result["data"]["sale"]["count"] == 4  # 대치동 거래 포함
    assert any("법정동을 찾지 못해" in w for w in result["data"]["warnings"])


def test_thin_dong_falls_back_to_sigungu_with_warning(monkeypatch):
    sales = [sale_item("100,000", dong="대치동") for _ in range(3)] + [sale_item("90,000")]
    result = run(monkeypatch, data_month_handler(sales=sales))
    assert result["data"]["sale"]["sample_basis"] == "같은 시군구·전용면적 ±5㎡"
    assert any("역삼동의 매매 거래가 부족해 시군구 전체" in w for w in result["data"]["warnings"])


def test_outliers_are_removed(monkeypatch):
    sales = [sale_item(p) for p in ("100,000", "101,000", "99,000", "100,500", "99,500", "30,000")]
    result = run(monkeypatch, data_month_handler(sales=sales))
    sale = result["data"]["sale"]
    assert (sale["count"], sale["outliers_removed"], sale["mean_won"]) == (5, 1, 10 * EOK)


def test_identical_prices_do_not_remove_normal_deals(monkeypatch):
    """절반 넘게 같은 값이면 사분위 범위가 0이 되어도 정상 거래를 빼지 않는다."""
    sales = [sale_item("100,000") for _ in range(5)] + [sale_item("101,000")]
    result = run(monkeypatch, data_month_handler(sales=sales))
    assert (result["data"]["sale"]["count"], result["data"]["sale"]["outliers_removed"]) == (6, 0)


@pytest.mark.parametrize(
    ("status", "body", "needle"),
    [
        (403, gateway_error("SERVICE_KEY_IS_NOT_REGISTERED_ERROR", "등록되지 않은 서비스키", "30"), "MOLIT_API_KEY"),
        (401, gateway_error("SERVICE_KEY_IS_NULL", "서비스 접근거부", "20"), "MOLIT_API_KEY"),
        (200, gateway_error("SERVICE_KEY_IS_NOT_REGISTERED_ERROR", "등록되지 않은 서비스키", "30"), "MOLIT_API_KEY"),
        (429, gateway_error("LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR", "한도 초과", "22"), "한도"),
        (400, gateway_error("NO_OPENAPI_SERVICE_ERROR", "해당 오픈API 서비스가 없거나 폐기됨", "12"), "NO_OPENAPI_SERVICE_ERROR"),
    ],
)
def test_api_errors_are_returned_not_raised(monkeypatch, status, body, needle):
    api = use_api(monkeypatch, lambda url, params: FakeResponse(status, body))
    result = price.search_price("서울 강남구 역삼동", months=3)
    assert_failure_shape(result)
    assert needle in result["error"]
    assert len(api.calls) == 6  # 4xx·인증 오류는 재시도하지 않는다


def test_unregistered_sale_api_keeps_jeonse(monkeypatch):
    """인증키에 매매 API 활용신청만 빠진 경우 (2026-11-24 공용키에서 실제로 겪은 상황)."""
    base = data_month_handler()
    denied = gateway_error("SERVICE_KEY_IS_NOT_REGISTERED_ERROR", "등록되지 않은 서비스키", "30")

    def handler(url, params):
        if "RHTrade" in url:
            return FakeResponse(403, denied)
        return base(url, params)

    result = run(monkeypatch, handler, housing_type="연립다세대")
    assert_success_shape(result)
    data = result["data"]
    assert data["market_price_won"] is None
    assert data["jeonse"]["count"] == 3  # 전세 결과는 살린다
    assert any("연립다세대 매매 실거래가 API를 쓸 수 없습니다" in w for w in data["warnings"])
    assert not any("매매 거래가 0건" in w or "수집 실패" in w for w in data["warnings"])
    assert "매매 API 미신청" in result["summary"]


def test_unregistered_sale_api_and_no_jeonse(monkeypatch):
    denied = gateway_error("SERVICE_KEY_IS_NOT_REGISTERED_ERROR", "등록되지 않은 서비스키", "30")

    def handler(url, params):
        return FakeResponse(403, denied) if "Trade" in url else FakeResponse(text=ok_xml([]))

    result = run(monkeypatch, handler, housing_type="연립다세대")
    assert_failure_shape(result)
    assert "활용신청" in result["error"]


def test_unregistered_sale_api_and_jeonse_timeout(monkeypatch):
    denied = gateway_error("SERVICE_KEY_IS_NOT_REGISTERED_ERROR", "등록되지 않은 서비스키", "30")

    def handler(url, params):
        if "Trade" in url:
            return FakeResponse(403, denied)
        raise requests.Timeout("timed out")

    result = run(monkeypatch, handler, housing_type="연립다세대")
    assert_failure_shape(result)
    assert "시간 초과" in result["error"]


def test_non_xml_403_is_auth_error(monkeypatch):
    use_api(monkeypatch, lambda url, params: FakeResponse(403, "<html>Forbidden</html>"))
    result = price.search_price("서울 강남구 역삼동", months=1)
    assert_failure_shape(result)
    assert "MOLIT_API_KEY" in result["error"]


def test_service_key_is_redacted_in_urllib3_logs(caplog):
    """앱이 DEBUG 로그를 켜도 인증키가 로그에 남지 않는다 (AGENTS.md 6장)."""
    pool_logger = logging.getLogger("urllib3.connectionpool")
    with caplog.at_level(logging.DEBUG, logger="urllib3.connectionpool"):
        pool_logger.debug(
            '%s://%s:%s "%s %s %s" %s %s', "https", "apis.data.go.kr", 443, "GET",
            f"/1613000/x?serviceKey={ENCODED_KEY}&LAWD_CD=11680", "HTTP/1.1", 200, 10,
        )
    assert ENCODED_KEY not in caplog.text and RAW_KEY not in caplog.text
    assert "serviceKey=***&LAWD_CD=11680" in caplog.text


def test_missing_api_key(monkeypatch):
    monkeypatch.delenv("MOLIT_API_KEY")
    api = use_api(monkeypatch, lambda url, params: FakeResponse(text=ok_xml([])))
    result = price.search_price("서울 강남구 역삼동")
    assert_failure_shape(result)
    assert "MOLIT_API_KEY" in result["error"]
    assert api.calls == []


def test_timeout_after_retries(monkeypatch):
    def handler(url, params):
        raise requests.Timeout("timed out")

    api = use_api(monkeypatch, handler)
    result = price.search_price("서울 강남구 역삼동", months=3)
    assert_failure_shape(result)
    assert "시간 초과" in result["error"]
    assert len(api.calls) == 6 * (price.MAX_RETRIES + 1)


def test_retry_recovers_from_transient_failures(monkeypatch):
    attempts = {}
    base = data_month_handler()

    def handler(url, params):
        key = (url, params["DEAL_YMD"])
        attempts[key] = attempts.get(key, 0) + 1
        if attempts[key] == 1:
            raise requests.ConnectionError("reset")
        if attempts[key] == 2:
            return FakeResponse(503, "busy")
        return base(url, params)

    result = run(monkeypatch, handler)
    assert result["data"]["sale"]["count"] == 3
    assert set(attempts.values()) == {3}


def test_partial_failure_is_a_warning(monkeypatch):
    base = data_month_handler()

    def handler(url, params):
        if params["DEAL_YMD"] == "202609" and not is_rent(url):
            raise requests.Timeout("timed out")
        return base(url, params)

    result = run(monkeypatch, handler)
    assert_success_shape(result)
    assert any("매매 202609" in w for w in result["data"]["warnings"])


@pytest.mark.parametrize(
    ("kwargs", "needle"),
    [
        ({"region": "  "}, "지역이 비어"),
        ({"housing_type": "상가"}, "주택 유형"),
        ({"months": 0}, "months"),
        ({"months": 25}, "months"),
        ({"months": True}, "months"),
        ({"exclusive_area_m2": -1}, "exclusive_area_m2"),
        ({"months": 6.5}, "months"),
        ({"exclusive_area_m2": float("nan")}, "exclusive_area_m2"),
        ({"exclusive_area_m2": float("inf")}, "exclusive_area_m2"),
        ({"exclusive_area_m2": "84"}, "exclusive_area_m2"),
        ({"building_name": 123}, "building_name"),
        ({"building_name": ["가상힐스"]}, "building_name"),
        ({"region": None}, "지역이 비어"),
        ({"region": "부산 해운대구 우동"}, "지원하지 않습니다"),
        ({"region": "광주 서구 치평동"}, "지원하지 않습니다"),  # 인천 서구로 잡으면 안 된다
        ({"region": "중구 명동"}, "여러 곳"),
    ],
)
def test_invalid_inputs(monkeypatch, kwargs, needle):
    result = run(monkeypatch, data_month_handler(), **kwargs)
    assert_failure_shape(result)
    assert needle in result["error"]


def test_lenient_inputs_from_llm(monkeypatch):
    """LLM이 흔히 보내는 형태(6.0, 공백 건물명)는 오류 없이 처리한다."""
    result = run(monkeypatch, data_month_handler(), months=3.0, building_name="   ")
    assert_success_shape(result)
    assert result["data"]["building_name"] is None
    assert result["data"]["warnings"] == []  # 공백 건물명으로 '단지 거래 부족' 경고를 내지 않는다
    assert price._resolve_region("경기 광주시 오포읍").lawd_cd == "41610"  # 경기 광주시는 그대로


def test_error_after_fetch_is_still_returned(monkeypatch):
    """조회 뒤 계산 단계에서 나는 예외도 밖으로 나가지 않는다."""
    def boom(*args):
        raise ZeroDivisionError

    monkeypatch.setattr(price, "_stats", boom)
    result = run(monkeypatch, data_month_handler())
    assert_failure_shape(result)


def test_unexpected_error_is_returned(monkeypatch):
    def boom(_):
        raise RuntimeError("boom")

    monkeypatch.setattr(price, "_resolve_region", boom)
    result = price.search_price("서울 강남구 역삼동")
    assert_failure_shape(result)
    assert "boom" not in result["error"]  # 내부 오류 문구는 LLM에 넘기지 않는다


# --------------------------------------------------------------------------- #
# calc_jeonse_ratio
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("deposit", "ratio", "level", "note"),
    [
        (699_000_000, 69.9, "안전", "기준선 이내"),
        (700_000_000, 70.0, "주의", "추가 확인 필요"),
        (800_000_000, 80.0, "주의", "추가 확인 필요"),
        (801_000_000, 80.1, "위험", "재검토 권고"),
    ],
)
def test_calc_jeonse_ratio_boundaries(deposit, ratio, level, note):
    result = price.calc_jeonse_ratio(deposit, 10 * EOK)
    assert_success_shape(result)
    data = result["data"]
    assert (data["jeonse_ratio_pct"], data["risk_level"], data["risk_note"]) == (ratio, level, note)
    assert data["threshold_pct"] == 80.0
    assert data["margin_won"] == 10 * EOK - deposit
    assert result["summary"] == f"전세가율 {ratio}% (기준선 80%) → {level}: {note}"


def test_calc_jeonse_ratio_report_example():
    """AGENTS.md 3장 보고서 예시: 2억 1천만 원 / 2억 4천만 원 = 87.5% → 위험."""
    data = price.calc_jeonse_ratio(210_000_000, 240_000_000)["data"]
    assert (data["jeonse_ratio_pct"], data["risk_level"], data["risk_note"]) == (87.5, "위험", "재검토 권고")
    assert (data["deposit_won"], data["market_price_won"]) == (210_000_000, 240_000_000)


@pytest.mark.parametrize(
    ("deposit", "market", "needle"),
    [
        (21_000, 240_000_000, "만원 단위"),  # 보증금 2억 1천을 만원으로 넣음
        (210_000_000, 24_000, "만원 단위"),  # 시세 2억 4천을 만원으로 넣음
        (210_000_000, 4_999_999, "만원 단위"),
        (0, 240_000_000, "0보다"),
        (-1, 240_000_000, "0보다"),
        ("2억", 240_000_000, "숫자"),
        (True, 240_000_000, "숫자"),
        (210_000_000, None, "숫자"),
        (float("nan"), 240_000_000, "숫자"),
        (210_000_000, float("inf"), "숫자"),
    ],
)
def test_calc_jeonse_ratio_invalid(deposit, market, needle):
    result = price.calc_jeonse_ratio(deposit, market)
    assert_failure_shape(result)
    assert needle in result["error"]


def test_calc_grade_follows_rounded_ratio():
    """80.04%는 80.0%로 보이므로 등급도 주의여야 화면과 어긋나지 않는다."""
    data = price.calc_jeonse_ratio(800_400_000, 10 * EOK)["data"]
    assert (data["jeonse_ratio_pct"], data["risk_level"]) == (80.0, "주의")


def test_calc_deposit_above_market_warns():
    data = price.calc_jeonse_ratio(11 * EOK, 10 * EOK)["data"]
    assert (data["jeonse_ratio_pct"], data["risk_level"], data["margin_won"]) == (110.0, "위험", -EOK)
    assert any("시세 이상" in w for w in data["warnings"])


def test_calc_unexpected_error_is_returned(monkeypatch):
    def boom(*args):
        raise ZeroDivisionError

    monkeypatch.setattr(price, "_classify_risk", boom)
    assert_failure_shape(price.calc_jeonse_ratio(210_000_000, 240_000_000))


def test_calc_jeonse_ratio_unit_mismatch_warning():
    result = price.calc_jeonse_ratio(2_100_000_000, 240_000_000)  # 보증금 단위 실수
    assert result["data"]["risk_level"] == "위험"
    assert any("단위" in w for w in result["data"]["warnings"])


def test_calc_small_monthly_rent_deposit():
    """월세 보증금 50만 원도 받는다. 시세의 1% 미만이면 단위 확인 경고만 붙인다."""
    result = price.calc_jeonse_ratio(500_000, 232_680_717)
    assert_success_shape(result)
    assert (result["data"]["jeonse_ratio_pct"], result["data"]["risk_level"]) == (0.2, "안전")
    assert any("만원 단위" in w for w in result["data"]["warnings"])
    # 월세 보증금 3천만 원(시나리오 B 예시)은 경고 없이 안전
    data = price.calc_jeonse_ratio(30_000_000, 232_680_717)["data"]
    assert (data["jeonse_ratio_pct"], data["risk_level"], data["warnings"]) == (12.9, "안전", [])


def test_calc_accepts_float_from_llm():
    assert price.calc_jeonse_ratio(210_000_000.0, 240_000_000.0)["data"]["jeonse_ratio_pct"] == 87.5


def test_search_then_calc_flow(monkeypatch):
    """LLM이 search_price 결과의 market_price_won을 calc_jeonse_ratio에 넘기는 흐름."""
    searched = run(monkeypatch, data_month_handler(), building_name="가상힐스")
    calc = price.calc_jeonse_ratio(8.5 * EOK, searched["data"]["market_price_won"])
    assert (calc["data"]["jeonse_ratio_pct"], calc["data"]["risk_level"]) == (85.0, "위험")


def test_docstrings_describe_when_to_use():
    for tool in (price.search_price, price.calc_jeonse_ratio):
        doc = tool.__doc__
        assert "언제 쓰나" in doc and "쓰지 말 것" in doc and "Args:" in doc and "Returns:" in doc


# --------------------------------------------------------------------------- #
# 실제 API 호출 (기본 실행에서 제외: pytest -m external)
# --------------------------------------------------------------------------- #
@pytest.mark.external
@pytest.mark.parametrize(
    ("region", "housing_type"),
    [("서울 송파구 잠실동", "아파트"), ("서울 강서구 화곡동", "연립다세대")],
)
def test_real_api_returns_sales(monkeypatch, region, housing_type):
    if not REAL_API_KEY:
        pytest.skip("실제 MOLIT_API_KEY가 없습니다 (환경 변수나 .env에 넣으세요)")
    monkeypatch.setenv("MOLIT_API_KEY", REAL_API_KEY)
    monkeypatch.setattr(price, "_recent_deal_ymds", REAL_YMDS)
    # 거래가 많은 지역이라 정상 응답이면 매매가 0건일 수 없다. 응답 태그 이름이 맞는지 확인한다.
    result = price.search_price(region, housing_type=housing_type, months=3)
    assert result["ok"] is True, result
    assert result["data"]["sale"]["count"] > 0
    assert result["data"]["jeonse"]["count"] > 0
    assert not any("API를 쓸 수 없습니다" in w for w in result["data"]["warnings"])  # 활용신청 누락 없음
