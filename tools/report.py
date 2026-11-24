"""최종 결과 제출 도구 (담당 A).

- submit_report: AGENTS.md 3장의 보고서 형식으로 결과를 받아 루프를 끝낸다.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# AGENTS.md 3장 보고서 형식에서 허용하는 값
RISK_LEVELS = ("위험", "주의", "안전")
POLICY_STATUSES = ("해당", "확인 필요")

# 화면에 그릴 수 있는 분량으로 제한한다.
MAX_CHECKLIST_ITEMS = 15
MAX_POLICIES = 10
MAX_TEXT_CHARS = 200


def _fail(message: str) -> dict[str, Any]:
    return {"ok": False, "error": message}


def _clean_text(value: Any, field: str) -> str:
    """비어 있지 않은 문자열인지 확인하고 앞뒤 공백을 지운다."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field}은(는) 비어 있지 않은 문자열이어야 합니다.")
    text = value.strip()
    if len(text) > MAX_TEXT_CHARS:
        raise ValueError(f"{field}이(가) {MAX_TEXT_CHARS}자를 넘습니다. 짧게 줄여 다시 제출하세요.")
    return text


def _optional_number(value: Any, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field}은(는) 숫자여야 합니다.")
    if value < 0:
        raise ValueError(f"{field}은(는) 0 이상이어야 합니다.")
    return float(value)


def _optional_won(value: Any, field: str) -> int | None:
    """금액은 원 단위 정수로 다룬다 (AGENTS.md 6장)."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field}은(는) 원 단위 정수여야 합니다.")
    if value <= 0:
        raise ValueError(f"{field}은(는) 0보다 커야 합니다.")
    return value


def _validate(
    risk_level: Any,
    risk_note: Any,
    checklist: Any,
    policies: Any,
    jeonse_ratio: Any,
    threshold: Any,
    market_price: Any,
    deposit: Any,
) -> dict[str, Any]:
    if risk_level not in RISK_LEVELS:
        raise ValueError(f"risk_level은 {' / '.join(RISK_LEVELS)} 중 하나여야 합니다.")
    note = _clean_text(risk_note, "risk_note")

    if not isinstance(checklist, list) or not checklist:
        raise ValueError("checklist는 항목이 하나 이상인 목록이어야 합니다.")
    if len(checklist) > MAX_CHECKLIST_ITEMS:
        raise ValueError(f"checklist는 {MAX_CHECKLIST_ITEMS}개 이하로 추려 주세요.")
    items = [_clean_text(item, "checklist 항목") for item in checklist]

    if not isinstance(policies, list):
        raise ValueError("policies는 목록이어야 합니다. 해당 정책이 없으면 빈 목록을 넣으세요.")
    if len(policies) > MAX_POLICIES:
        raise ValueError(f"policies는 {MAX_POLICIES}개 이하로 추려 주세요.")
    cleaned_policies: list[dict[str, str]] = []
    for policy in policies:
        if not isinstance(policy, dict):
            raise ValueError('policies의 각 항목은 {"name": ..., "status": ...} 형식이어야 합니다.')
        status = policy.get("status")
        if status not in POLICY_STATUSES:
            raise ValueError(f"정책 status는 {' / '.join(POLICY_STATUSES)} 중 하나여야 합니다.")
        cleaned_policies.append({"name": _clean_text(policy.get("name"), "정책 name"), "status": status})

    return {
        "risk_level": risk_level,
        "risk_note": note,
        "jeonse_ratio": _optional_number(jeonse_ratio, "jeonse_ratio"),
        "threshold": _optional_number(threshold, "threshold"),
        "market_price": _optional_won(market_price, "market_price"),
        "deposit": _optional_won(deposit, "deposit"),
        "checklist": items,
        "policies": cleaned_policies,
    }


def submit_report(
    risk_level: str,
    risk_note: str,
    checklist: list[str],
    policies: list[dict[str, str]],
    jeonse_ratio: float | None = None,
    threshold: float | None = None,
    market_price: int | None = None,
    deposit: int | None = None,
) -> dict[str, Any]:
    """최종 보고서를 형식에 맞는지 검사해 제출하고 에이전트 루프를 끝낸다.

    언제 쓰나: 위험 진단과 필요한 근거·정책 확인을 마쳐 사용자에게 보여줄 결론이 섰을 때
    마지막에 한 번 부른다. 형식이 틀리면 실패를 돌려주므로 고쳐서 다시 부른다.
    쓰지 말 것: 아직 전세가율을 계산하지 않았거나 확인할 근거가 남았을 때.

    전세가율·기준선·시세·보증금은 에이전트 코어가 calc_jeonse_ratio 결과와 사용자 입력으로
    채운다. LLM이 숫자를 다시 쓰지 않게 해서 지어낸 값이 들어가는 것을 막는다.
    시세를 구하지 못했으면 None으로 둔다.

    Args:
        risk_level: "위험" | "주의" | "안전".
        risk_note: 한 줄 권고. 예: "재검토 권고".
        checklist: 계약 전에 확인할 항목 목록 (1~15개, 각 200자 이내).
        policies: [{"name": 정책 이름, "status": "해당" | "확인 필요"}] (0~10개).
        jeonse_ratio: 전세가율(%).
        threshold: 위험 기준선(%).
        market_price: 매매 시세(원).
        deposit: 보증금(원).

    Returns:
        성공: {"ok": True, "data": AGENTS.md 3장 보고서, "summary": "..."}
        실패: {"ok": False, "error": "무엇이 틀렸고 어떻게 고칠지"}
    """
    try:
        report = _validate(
            risk_level, risk_note, checklist, policies,
            jeonse_ratio, threshold, market_price, deposit,
        )
    except ValueError as exc:
        return _fail(f"보고서 형식 오류: {exc}")
    except Exception as exc:
        logger.warning("submit_report 예상하지 못한 오류: %s", type(exc).__name__)
        return _fail("보고서 검사 중 예상하지 못한 오류가 났습니다. 형식을 확인해 다시 제출하세요.")

    summary = (
        f"보고서 제출: {report['risk_level']} ({report['risk_note']}), "
        f"체크리스트 {len(report['checklist'])}개, 정책 {len(report['policies'])}개"
    )
    return {"ok": True, "data": report, "summary": summary}
