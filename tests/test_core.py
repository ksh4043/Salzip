"""에이전트 루프를 가짜 LLM 응답으로 검증한다. 외부 API와 LLM은 부르지 않는다.

매물·금액·건물명은 모두 가상이다.
"""

import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from agent import core
from agent.core import UserInput, run_agent
from agent.registry import CALC_JEONSE_RATIO, SEARCH_LAW, SEARCH_PRICE, SUBMIT_REPORT

DEPOSIT_WON = 210_000_000
MARKET_PRICE_WON = 240_000_000

USER = UserInput(
    region="서울 강서구 화곡동",
    housing_type="연립다세대",
    contract_type="전세",
    deposit_won=DEPOSIT_WON,
    age_band="25~29세",
    income_band="중위소득 60% 이하",
    building_name="가상빌라",
)


def fake_search_price(**kwargs: Any) -> dict[str, Any]:
    return {"ok": True, "data": {"market_price_won": MARKET_PRICE_WON}, "summary": "매매 3건, 전세 6건 조회"}


def fake_search_law(**kwargs: Any) -> dict[str, Any]:
    return {"ok": True, "data": {"evidence": []}, "summary": "HUG 반환보증 근거 2개"}


def raising_tool(**kwargs: Any) -> dict[str, Any]:
    raise RuntimeError("도구 내부 오류")


FAKE_TOOLS = {
    "search_price": replace(SEARCH_PRICE, func_path="tests.test_core:fake_search_price"),
    "calc_jeonse_ratio": CALC_JEONSE_RATIO,  # 실제 계산 함수 (외부 호출 없음)
    "search_law": replace(SEARCH_LAW, func_path="tests.test_core:fake_search_law"),
    "submit_report": SUBMIT_REPORT,
}


def call(name: str, **args: Any) -> SimpleNamespace:
    args.setdefault("reason", f"{name} 호출 이유")
    return SimpleNamespace(
        id=f"call_{name}",
        function=SimpleNamespace(name=name, arguments=json.dumps(args, ensure_ascii=False)),
    )


def reply(*calls: SimpleNamespace, content: str | None = None) -> SimpleNamespace:
    message = SimpleNamespace(content=content, tool_calls=list(calls) or None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class FakeClient:
    """정해진 응답을 차례로 돌려주고, 받은 요청을 기록한다."""

    def __init__(self, responses: list[SimpleNamespace]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any) -> SimpleNamespace:
        self.requests.append(json.loads(json.dumps(kwargs, ensure_ascii=False, default=str)))
        return self.responses.pop(0)


REPORT_ARGS = {
    "risk_level": "안전",  # 계산 결과로 덮어써져야 한다
    "risk_note": "임의 메모",
    "checklist": ["반환보증 가입 가능 여부 확인"],
    "policies": [],
}


def risky_path_client() -> FakeClient:
    return FakeClient([
        reply(call("search_price", region=USER.region, housing_type="연립다세대")),
        reply(call("calc_jeonse_ratio", market_price_won=MARKET_PRICE_WON)),
        reply(call("search_law", query="전세보증금반환보증 가입 조건")),
        reply(call("submit_report", **REPORT_ARGS)),
    ])


def test_risky_path_produces_report_with_tool_numbers() -> None:
    steps: list[dict] = []
    result = run_agent(USER, on_step=steps.append, client=risky_path_client(), tools=FAKE_TOOLS)

    assert result.ok is True
    assert result.tool_trace == ["search_price", "calc_jeonse_ratio", "search_law", "submit_report"]
    report = result.report
    assert report["jeonse_ratio"] == 87.5
    assert report["threshold"] == 80.0
    assert report["market_price"] == MARKET_PRICE_WON
    assert report["deposit"] == DEPOSIT_WON
    # LLM이 넣은 등급이 아니라 계산 결과가 들어간다
    assert report["risk_level"] == "위험"
    assert report["risk_note"] == "재검토 권고"

    assert steps == result.steps
    assert [s["tools"] for s in steps] == [[n] for n in result.tool_trace]
    assert steps[1]["warn"] is True and steps[0]["warn"] is False
    assert steps[0]["reason"] == "search_price 호출 이유"
    assert set(steps[0]) == {"tools", "text", "reason", "warn"}


def test_raw_inputs_never_reach_llm() -> None:
    client = risky_path_client()
    run_agent(USER, client=client, tools=FAKE_TOOLS)
    sent = json.dumps(client.requests, ensure_ascii=False)
    for raw in (str(DEPOSIT_WON), USER.age_band, USER.income_band, "margin_won", str(MARKET_PRICE_WON - DEPOSIT_WON)):
        assert raw not in sent
    # 도구 결과는 대화 기록에 들어가 다음 판단에 쓰인다
    last_messages = client.requests[-1]["messages"]
    assert any(m["role"] == "tool" and "87.5" in m["content"] for m in last_messages)


def test_tool_failure_is_returned_to_llm_and_loop_continues() -> None:
    client = FakeClient([
        reply(call("calc_jeonse_ratio", market_price_won=0)),  # 실패: 시세가 0
        reply(call("calc_jeonse_ratio", market_price_won=MARKET_PRICE_WON)),
        reply(call("submit_report", **REPORT_ARGS)),
    ])
    result = run_agent(USER, client=client, tools=FAKE_TOOLS)
    assert result.ok is True
    assert result.steps[0]["warn"] is True
    error_message = client.requests[1]["messages"][-1]
    assert error_message["role"] == "tool" and '"ok": false' in error_message["content"]


def test_invalid_report_is_retried() -> None:
    client = FakeClient([
        reply(call("submit_report", **{**REPORT_ARGS, "checklist": []})),
        reply(call("submit_report", **REPORT_ARGS)),
    ])
    result = run_agent(USER, client=client, tools=FAKE_TOOLS)
    assert result.ok is True
    assert result.tool_trace == ["submit_report", "submit_report"]
    # 시세를 못 구했으면 숫자는 비고 보증금만 채워진다
    assert result.report["jeonse_ratio"] is None
    assert result.report["deposit"] == DEPOSIT_WON
    assert result.report["risk_level"] == "안전"


def test_unknown_tool_bad_json_and_exception_do_not_crash() -> None:
    tools = {**FAKE_TOOLS, "search_law": replace(SEARCH_LAW, func_path="tests.test_core:raising_tool")}
    bad_json = SimpleNamespace(id="call_bad", function=SimpleNamespace(name="search_price", arguments="{"))
    client = FakeClient([
        reply(call("search_policy", query="월세 지원")),
        reply(bad_json),
        reply(call("search_law", query="대항력")),
        reply(call("submit_report", **REPORT_ARGS)),
    ])
    result = run_agent(USER, client=client, tools=tools)
    assert result.ok is True
    assert [s["warn"] for s in result.steps[:3]] == [True, True, True]


def test_text_reply_gets_nudge() -> None:
    client = FakeClient([reply(content="생각 중입니다."), reply(call("submit_report", **REPORT_ARGS))])
    result = run_agent(USER, client=client, tools=FAKE_TOOLS)
    assert result.ok is True
    assert client.requests[1]["messages"][-1] == {"role": "user", "content": core.NUDGE_MESSAGE}


def test_max_steps_forces_report_on_last_step() -> None:
    client = FakeClient([
        reply(call("search_law", query="확정일자")),
        reply(call("search_law", query="대항력")),
        reply(call("submit_report", **REPORT_ARGS)),
    ])
    result = run_agent(USER, client=client, tools=FAKE_TOOLS, max_steps=3)
    assert result.ok is True
    choices = [r["tool_choice"] for r in client.requests]
    assert choices[:2] == ["auto", "auto"]
    assert choices[2] == {"type": "function", "function": {"name": "submit_report"}}


def test_stops_at_max_steps_without_report() -> None:
    client = FakeClient([reply(call("search_law", query="확정일자")) for _ in range(2)])
    result = run_agent(USER, client=client, tools=FAKE_TOOLS, max_steps=2)
    assert result.ok is False
    assert len(client.requests) == 2
    assert "최대 반복 횟수" in result.error


def test_llm_error_returns_failure() -> None:
    class BrokenClient(FakeClient):
        def _create(self, **kwargs: Any) -> SimpleNamespace:
            raise ConnectionError("network")

    result = run_agent(USER, client=BrokenClient([]), tools=FAKE_TOOLS)
    assert result.ok is False
    assert result.steps[-1]["warn"] is True


@pytest.mark.parametrize("injected", ["deposit_won"])
def test_llm_cannot_override_injected_input(injected: str) -> None:
    client = FakeClient([
        reply(call("calc_jeonse_ratio", market_price_won=MARKET_PRICE_WON, **{injected: 1})),
        reply(call("submit_report", **REPORT_ARGS)),
    ])
    result = run_agent(USER, client=client, tools=FAKE_TOOLS)
    assert result.report["jeonse_ratio"] == 87.5
