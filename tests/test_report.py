"""submit_report의 형식 검사를 검증한다. 값은 모두 가상이다."""

import pytest

from tools.report import submit_report

VALID = {
    "risk_level": "위험",
    "risk_note": "재검토 권고",
    "checklist": ["반환보증 가입 가능 여부 확인", "등기부등본 선순위 채권 확인"],
    "policies": [{"name": "가상 청년 월세 지원", "status": "확인 필요"}],
    "jeonse_ratio": 87.5,
    "threshold": 80.0,
    "market_price": 240_000_000,
    "deposit": 210_000_000,
}


def test_valid_report_returns_contract_format() -> None:
    result = submit_report(**VALID)
    assert result["ok"] is True
    assert set(result["data"]) == {
        "risk_level", "risk_note", "jeonse_ratio", "threshold",
        "market_price", "deposit", "checklist", "policies",
    }
    assert result["data"]["market_price"] == 240_000_000
    assert len(result["summary"]) <= 200


def test_numbers_may_be_missing_when_price_unavailable() -> None:
    args = {**VALID, "jeonse_ratio": None, "threshold": None, "market_price": None, "deposit": None}
    result = submit_report(**args)
    assert result["ok"] is True
    assert result["data"]["jeonse_ratio"] is None


def test_strips_whitespace() -> None:
    result = submit_report(**{**VALID, "checklist": ["  확정일자 받기  "]})
    assert result["data"]["checklist"] == ["확정일자 받기"]


@pytest.mark.parametrize(
    "override",
    [
        {"risk_level": "매우 위험"},
        {"risk_note": "  "},
        {"checklist": []},
        {"checklist": "문자열 하나"},
        {"checklist": ["가"] * 16},
        {"policies": [{"name": "가상 정책", "status": "탈락"}]},
        {"policies": ["가상 정책"]},
        {"policies": None},
        {"market_price": 2.4e8},
        {"deposit": True},
        {"jeonse_ratio": "87.5"},
        {"jeonse_ratio": -1},
    ],
)
def test_invalid_report_returns_error_without_raising(override: dict) -> None:
    result = submit_report(**{**VALID, **override})
    assert result["ok"] is False
    assert result["error"].startswith("보고서 형식 오류")
