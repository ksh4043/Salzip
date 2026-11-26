"""AGENTS.md 2장 테스트 시나리오(인물 A·B·C)를 에이전트 루프 전체로 검증한다.

기본 실행(외부 호출 없음)
- LLM 자리에는 SYSTEM_PROMPT의 판단 기준을 옮긴 규칙 기반 가짜 LLM을 둔다. 호출 순서를 미리 적어 두지 않고
  매번 대화 기록의 도구 결과(위험 등급, 자격 판정)를 읽고 다음 도구를 고른다. 그래서 도구 결과가 바뀌면
  경로도 바뀌고, "도구 결과에 다음 판단에 필요한 정보가 담겨 있는가"와 "주입·숨김 규칙"을 함께 확인한다.
- calc_jeonse_ratio, search_policy, check_eligibility, submit_report는 실제 함수를 쓰고,
  국토부·RAG는 가짜 결과로, 온통청년 API는 가짜 HTTP 응답으로 바꾼다.

pytest -m external
- 같은 시나리오를 실제 OpenAI 모델로 돌려 경로의 핵심만 확인한다(OPENAI_API_KEY 필요).

인물, 매물, 정책은 모두 가상이다.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import tools.policy as policy
from agent.core import UserInput, run_agent
from agent.registry import TOOLS
from tests.test_policy import FakeApi, make_policy

EOK = 100_000_000
MAN = 10_000

# --------------------------------------------------------------------------- #
# 인물 (AGENTS.md 2장 테스트 시나리오)
# B는 오피스텔이 범위에서 빠져(2026-11-24 팀 합의) 빌라(연립다세대) 월세로 바꿨다.
# --------------------------------------------------------------------------- #
PERSON_A = UserInput(  # 전세가율이 높은 빌라 전세
    region="서울 강서구 화곡동",
    housing_type="연립다세대",
    contract_type="전세",
    deposit_won=2 * EOK + 1000 * MAN,  # 87.5% → 위험
    age_band="25~29세",
    income_band="3,000만원 초과~4,000만원 이하",
    building_name="가상빌라",
)
PERSON_B = UserInput(  # 시세 대비 안전한 빌라 월세
    region="서울 강서구 화곡동",
    housing_type="연립다세대",
    contract_type="월세",
    deposit_won=1000 * MAN,  # 약 4.2% → 안전
    age_band="25~29세",
    income_band="2,000만원 이하",
    building_name="가상하우스",
)
PERSON_C = UserInput(  # 안전하지만 소득 기준이 애매함
    region="서울 강서구 화곡동",
    housing_type="연립다세대",
    contract_type="전세",
    deposit_won=1 * EOK + 2000 * MAN,  # 50% → 안전
    age_band="30~34세",
    income_band="4,000만원 초과~5,000만원 이하",
    building_name="가상맨션",
)
MARKET_PRICE_WON = 2 * EOK + 4000 * MAN

GANGSEO = "11500"
RENT_SUPPORT = make_policy(  # 청년 월세 지원: 나이 19~39세, 연소득 5,000만원 이하
    "RENT", name="가상 청년 월세 지원",
    sprtTrgtMinAge="19", sprtTrgtMaxAge="39", sprtTrgtAgeLmtYn="N",
    earnCndSeCd=policy.EARN_ANNUAL, earnMaxAmt="5000", zipCd=GANGSEO,
)
LOW_INCOME_RENT = make_policy(  # 저소득 월세 지원: 연소득 3,000만원 이하 → 인물 C는 탈락
    "LOWRENT", name="가상 저소득 청년 월세",
    sprtTrgtMinAge="19", sprtTrgtMaxAge="39", sprtTrgtAgeLmtYn="N",
    earnCndSeCd=policy.EARN_ANNUAL, earnMaxAmt="3000", zipCd=GANGSEO,
)
GUARANTEE_FEE = make_policy(  # 보증료 지원: 소득 무관
    "FEE", name="가상 전세보증료 지원",
    sprtTrgtMinAge="19", sprtTrgtMaxAge="39", sprtTrgtAgeLmtYn="N",
    earnCndSeCd=policy.EARN_ANY, zipCd=GANGSEO,
)


# --------------------------------------------------------------------------- #
# 가짜 외부 의존성
# --------------------------------------------------------------------------- #
def fake_search_price(**kwargs: Any) -> dict[str, Any]:
    return {"ok": True, "data": {"market_price_won": MARKET_PRICE_WON}, "summary": "매매 5건, 전세 8건 조회"}


def fake_search_law(**kwargs: Any) -> dict[str, Any]:
    return {
        "ok": True,
        "data": {"category": "hug_guarantee_conditions", "evidence": [{"title": "가상 반환보증 개요"}]},
        "summary": "HUG 반환보증 가입 조건 근거 2개",
    }


SCENARIO_TOOLS = {
    **TOOLS,
    "search_price": replace(TOOLS["search_price"], func_path="tests.test_scenarios:fake_search_price"),
    "search_law": replace(TOOLS["search_law"], func_path="tests.test_scenarios:fake_search_law"),
}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("YOUTHCENTER_API_KEY", "test-key-0000")  # 가상 키
    monkeypatch.setattr(policy, "_POLICY_CACHE", {})


def use_policies(monkeypatch, rows: list[dict[str, str]]) -> FakeApi:
    api = FakeApi(rows)
    monkeypatch.setattr(policy.requests, "get", api)
    return api


# --------------------------------------------------------------------------- #
# 규칙 기반 가짜 LLM
# --------------------------------------------------------------------------- #
@dataclass
class ToolEvent:
    name: str
    args: dict[str, Any]
    result: dict[str, Any]


def _history(messages: list[dict[str, Any]]) -> list[ToolEvent]:
    """대화 기록에서 (도구 이름, 인자, 결과)를 순서대로 뽑는다. LLM이 보는 것과 같은 정보만 쓴다."""
    calls: dict[str, tuple[str, dict[str, Any]]] = {}
    events: list[ToolEvent] = []
    for message in messages:
        for call in message.get("tool_calls") or []:
            calls[call["id"]] = (call["function"]["name"], json.loads(call["function"]["arguments"]))
        if message["role"] == "tool":
            name, args = calls[message["tool_call_id"]]
            events.append(ToolEvent(name, args, json.loads(message["content"])))
    return events


class RuleBasedLLM:
    """SYSTEM_PROMPT의 판단 기준을 그대로 따르는 가짜 LLM.

    1. 시세 → 전세가율 계산
    2. 위험·주의: 법·보증보험 근거 검색. 위험이면 재검토 권고로 보고.
    3. 안전·주의: 정책 검색 → 자격 판정. 해당이 없고 탈락이 있으면 탈락 정책을 빼고 다시 검색.
    4. 보고서에는 해당·확인 필요 정책만 넣는다.
    """

    MAX_POLICY_SEARCHES = 3

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self._call_no = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any) -> SimpleNamespace:
        self.requests.append(json.loads(json.dumps(kwargs, ensure_ascii=False, default=str)))
        messages = self.requests[-1]["messages"]
        region = next(
            line.split(":", 1)[1].strip()
            for line in messages[1]["content"].splitlines() if line.startswith("- 지역:")
        )
        name, args = self._decide(_history(messages), region, kwargs["tool_choice"])
        self._call_no += 1
        args["reason"] = f"{name} 판단"
        call = SimpleNamespace(
            id=f"call_{self._call_no}",
            function=SimpleNamespace(name=name, arguments=json.dumps(args, ensure_ascii=False)),
        )
        message = SimpleNamespace(content=None, tool_calls=[call])
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    def _decide(self, events: list[ToolEvent], region: str, tool_choice: Any) -> tuple[str, dict[str, Any]]:
        ok = [e for e in events if e.result.get("ok")]
        price = next((e for e in ok if e.name == "search_price"), None)
        ratio = next((e for e in ok if e.name == "calc_jeonse_ratio"), None)
        risk = ratio.result["data"]["risk_level"] if ratio else None
        laws = [e for e in ok if e.name == "search_law"]
        searches = [e for e in ok if e.name == "search_policy"]
        judgements = [r for e in ok if e.name == "check_eligibility" for r in e.result["data"]["results"]]

        if isinstance(tool_choice, dict):  # 마지막 반복: 보고서 강제
            return self._report(risk, judgements)
        if price is None:
            return "search_price", {"region": region, "housing_type": "연립다세대"}
        if ratio is None:
            return "calc_jeonse_ratio", {"market_price_won": price.result["data"]["market_price_won"]}
        if risk in ("위험", "주의") and not laws:
            return "search_law", {"query": "전세보증금반환보증 가입 조건과 보증한도"}
        if risk == "위험":
            return self._report(risk, judgements)

        judged = {r["policy_id"] for r in judgements}
        if not searches:
            return "search_policy", {"region": region, "keyword": "월세"}
        pending = [p["policy_id"] for p in searches[-1].result["data"]["policies"] if p["policy_id"] not in judged]
        if pending:
            return "check_eligibility", {"policy_ids": pending, "region": region}
        has_eligible = any(r["status"] == "해당" for r in judgements)
        has_rejected = any(r["status"] == "해당 없음" for r in judgements)
        if not has_eligible and has_rejected and len(searches) < self.MAX_POLICY_SEARCHES:
            return "search_policy", {"region": region, "keyword": None, "exclude_ids": sorted(judged)}
        return self._report(risk, judgements)

    @staticmethod
    def _report(risk: str | None, judgements: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
        level = risk or "주의"
        checklist = ["등기부등본으로 선순위 채권 확인", "전입신고와 확정일자 받기"]
        if level == "위험":
            checklist.insert(0, "전세보증금반환보증 가입 가능 여부 확인(HUG 반환보증 근거)")
        policies = [
            {"name": r["name"], "status": r["status"]}
            for r in judgements if r["status"] in ("해당", "확인 필요")
        ]
        note = "재검토 권고" if level == "위험" else "기준선 이내"
        return "submit_report", {"risk_level": level, "risk_note": note, "checklist": checklist, "policies": policies}


def run(person: UserInput, client: Any) -> Any:
    return run_agent(person, client=client, tools=SCENARIO_TOOLS, max_steps=12)


def sent_text(client: RuleBasedLLM) -> str:
    return json.dumps(client.requests, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# 시나리오 (기본 실행)
# --------------------------------------------------------------------------- #
def test_person_a_risky_villa_goes_to_law_and_review(monkeypatch):
    api = use_policies(monkeypatch, [RENT_SUPPORT, GUARANTEE_FEE])
    client = RuleBasedLLM()
    result = run(PERSON_A, client)

    assert result.ok is True
    assert result.tool_trace == ["search_price", "calc_jeonse_ratio", "search_law", "submit_report"]
    assert result.report["risk_level"] == "위험"
    assert result.report["risk_note"] == "재검토 권고"
    assert result.report["jeonse_ratio"] == 87.5
    assert any("반환보증" in item for item in result.report["checklist"])
    assert api.calls == []  # 위험 경로는 정책 탐색으로 가지 않는다


def test_person_b_safe_rent_finds_policy_list(monkeypatch):
    use_policies(monkeypatch, [RENT_SUPPORT, GUARANTEE_FEE])
    client = RuleBasedLLM()
    result = run(PERSON_B, client)

    assert result.ok is True
    assert result.tool_trace == [
        "search_price", "calc_jeonse_ratio", "search_policy", "check_eligibility", "submit_report",
    ]
    assert result.report["risk_level"] == "안전"
    assert result.report["policies"] == [{"name": "가상 청년 월세 지원", "status": "해당"}]


def test_person_c_rejected_then_searches_again(monkeypatch):
    use_policies(monkeypatch, [LOW_INCOME_RENT, GUARANTEE_FEE])
    client = RuleBasedLLM()
    result = run(PERSON_C, client)

    assert result.ok is True
    assert result.tool_trace == [
        "search_price", "calc_jeonse_ratio",
        "search_policy", "check_eligibility",  # 월세 지원 → 소득 기준 탈락
        "search_policy", "check_eligibility",  # 탈락 정책을 빼고 다시 검색 → 보증료 지원 해당
        "submit_report",
    ]
    assert result.report["risk_level"] == "안전"
    assert result.report["policies"] == [{"name": "가상 전세보증료 지원", "status": "해당"}]

    events = _history(client.requests[-1]["messages"])
    first_check = events[3].result["data"]["results"][0]
    assert first_check["status"] == "해당 없음"
    assert any("소득 조건" in reason for reason in first_check["reasons"])
    assert events[4].args["exclude_ids"] == ["LOWRENT"]


def test_same_person_takes_different_path_when_policy_data_changes(monkeypatch):
    """경로가 코드에 고정되지 않았다: 인물 C라도 처음 정책이 해당이면 다시 찾지 않는다."""
    use_policies(monkeypatch, [RENT_SUPPORT, GUARANTEE_FEE])
    result = run(PERSON_C, RuleBasedLLM())
    assert result.tool_trace.count("search_policy") == 1
    assert result.report["policies"] == [{"name": "가상 청년 월세 지원", "status": "해당"}]


@pytest.mark.parametrize("person", [PERSON_A, PERSON_B, PERSON_C])
def test_raw_inputs_never_reach_llm(monkeypatch, person):
    use_policies(monkeypatch, [LOW_INCOME_RENT, RENT_SUPPORT, GUARANTEE_FEE])
    client = RuleBasedLLM()
    result = run(person, client)
    assert result.ok is True
    text = sent_text(client)
    for raw in (str(person.deposit_won), f"{person.deposit_won:,}", person.age_band, person.income_band):
        assert raw not in text  # AGENTS.md 4장


def test_policy_failure_still_produces_report(monkeypatch):
    """온통청년 API 키가 없어도 루프는 멈추지 않고 보고서를 낸다."""
    monkeypatch.delenv("YOUTHCENTER_API_KEY")
    use_policies(monkeypatch, [RENT_SUPPORT])
    client = RuleBasedLLM()
    result = run(PERSON_B, client)
    assert result.ok is True
    assert result.report["policies"] == []
    assert any(step["warn"] and "YOUTHCENTER_API_KEY" in step["text"] for step in result.steps)


# --------------------------------------------------------------------------- #
# 실제 LLM (pytest -m external)
# --------------------------------------------------------------------------- #
def _openai_key() -> str | None:
    if os.getenv("OPENAI_API_KEY"):
        return os.getenv("OPENAI_API_KEY")
    try:
        from dotenv import dotenv_values
    except ImportError:
        return None
    return dotenv_values(Path(__file__).resolve().parents[1] / ".env").get("OPENAI_API_KEY") or None


OPENAI_KEY = _openai_key()


def _real_client() -> Any:
    if not OPENAI_KEY:
        pytest.skip("OPENAI_API_KEY가 없어 실제 LLM 시나리오를 건너뜀")
    from openai import OpenAI

    return OpenAI(api_key=OPENAI_KEY)


@pytest.mark.external
def test_real_llm_person_a(monkeypatch):
    use_policies(monkeypatch, [RENT_SUPPORT, GUARANTEE_FEE])
    result = run(PERSON_A, _real_client())
    assert result.ok is True, result.error
    trace = result.tool_trace
    assert trace.index("calc_jeonse_ratio") < trace.index("search_law")
    assert result.report["risk_level"] == "위험"


@pytest.mark.external
def test_real_llm_person_b(monkeypatch):
    use_policies(monkeypatch, [RENT_SUPPORT, GUARANTEE_FEE])
    result = run(PERSON_B, _real_client())
    assert result.ok is True, result.error
    trace = result.tool_trace
    assert trace.index("calc_jeonse_ratio") < trace.index("search_policy") < trace.index("check_eligibility")
    assert result.report["risk_level"] == "안전"
    assert {"name": "가상 청년 월세 지원", "status": "해당"} in result.report["policies"]


@pytest.mark.external
def test_real_llm_person_c(monkeypatch):
    use_policies(monkeypatch, [LOW_INCOME_RENT, GUARANTEE_FEE])
    result = run(PERSON_C, _real_client())
    assert result.ok is True, result.error
    assert "check_eligibility" in result.tool_trace
    names = [p["name"] for p in result.report["policies"]]
    assert "가상 저소득 청년 월세" not in names  # 해당 없음은 보고서에 넣지 않는다
