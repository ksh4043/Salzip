"""에이전트 루프 (담당 A).

LLM 호출 → 도구 실행 → 결과를 대화 기록에 넣고 다시 호출하는 루프.
최대 반복 횟수를 반드시 둔다. 도구 호출 순서는 코드에 고정하지 않는다.

사용자 원본 입력(보증금, 나이·소득 구간)은 세션(ctx)에만 두고, 도구를 실행할 때
registry의 injected 규칙으로 채운다. LLM에는 계산된 결과만 보낸다 (AGENTS.md 4장).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable

from agent.prompts import SYSTEM_PROMPT, build_user_message
from agent.registry import (
    CTX_DEPOSIT,
    FINAL_TOOL,
    REASON_PARAM,
    TOOLS,
    ToolSpec,
    openai_tools,
)

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-4o"  # 팀 기본값 (.env.example의 LLM_MODEL)
DEFAULT_MAX_STEPS = 10  # LLM 호출 횟수 상한 (.env.example의 AGENT_MAX_STEPS)
LOG_TEXT_MAX_CHARS = 200  # 진행 로그 한 줄 길이. 도구 summary 상한(AGENTS.md 3장)과 같게 둔다.
WARN_RISK_LEVELS = ("위험", "주의")  # 진행 로그에서 경고로 표시할 위험 등급

# LLM이 도구를 부르지 않고 글로만 답했을 때 다시 보내는 안내
NUDGE_MESSAGE = "결론이 섰으면 submit_report 도구로 제출하고, 아니면 필요한 도구를 부르세요."

StepCallback = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class UserInput:
    """화면에서 받은 조건. 동·호수, 이름, 연락처는 받지 않는다 (AGENTS.md 4장)."""

    region: str
    housing_type: str  # "아파트" | "연립다세대"
    contract_type: str  # "전세" | "월세"
    deposit_won: int
    age_band: str
    income_band: str
    building_name: str | None = None
    exclusive_area_m2: float | None = None

    def session_context(self) -> dict[str, Any]:
        """도구 인자 주입에 쓰는 세션 값. 정책 도구용 키는 담당 D와 맞춘 뒤 registry에 연결한다."""
        return {
            CTX_DEPOSIT: self.deposit_won,
            "age_band": self.age_band,
            "income_band": self.income_band,
        }


@dataclass
class AgentResult:
    ok: bool
    report: dict[str, Any] | None
    steps: list[dict[str, Any]] = field(default_factory=list)  # 진행 로그 (AGENTS.md 3장 형식)
    tool_trace: list[str] = field(default_factory=list)  # 호출된 도구 순서 (시나리오 테스트용)
    error: str | None = None


def _fail(message: str) -> dict[str, Any]:
    return {"ok": False, "error": message}


def _visible_result(spec: ToolSpec, result: dict[str, Any]) -> dict[str, Any]:
    """LLM에 보낼 결과 사본. 원본 입력을 되돌려 주는 키를 지운다."""
    if not spec.hidden_result_keys or not isinstance(result.get("data"), dict):
        return result
    data = {k: v for k, v in result["data"].items() if k not in spec.hidden_result_keys}
    return {**result, "data": data}


def execute_tool(
    spec: ToolSpec, llm_args: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    """LLM 인자에 세션 값을 주입해 도구를 실행한다. 예외는 실패 형식으로 바꾼다."""
    args = dict(llm_args)
    for param, ctx_key in spec.injected.items():
        if ctx_key in ctx:
            # 세션 값이 있으면 LLM이 넣은 값보다 우선한다 (지어낸 숫자를 막는다).
            args[param] = ctx[ctx_key]
        elif param not in spec.parameters.get("properties", {}):
            # LLM이 볼 수 없는 인자를 넣었으면 버린다.
            args.pop(param, None)
    try:
        result = spec.resolve()(**args)
    except TypeError as exc:
        logger.info("도구 인자 오류: %s", spec.name)
        return _fail(f"{spec.name} 인자가 맞지 않습니다: {exc}. 도구 설명의 인자를 확인해 다시 부르세요.")
    except Exception as exc:
        # 도구는 예외를 던지지 않기로 했지만(AGENTS.md 3장) 루프가 멈추지 않게 한 번 더 막는다.
        logger.warning("도구 %s 예외: %s", spec.name, type(exc).__name__)
        return _fail(f"{spec.name} 실행 중 오류가 났습니다. 다른 방법을 고려하세요.")
    if not isinstance(result, dict) or "ok" not in result:
        return _fail(f"{spec.name}이(가) 형식에 맞지 않는 결과를 돌려줬습니다.")

    if result["ok"] and isinstance(result.get("data"), dict):
        for data_key, ctx_key in spec.exports.items():
            if data_key in result["data"]:
                ctx[ctx_key] = result["data"][data_key]
    return result


def _step_log(name: str, reason: str, result: dict[str, Any]) -> dict[str, Any]:
    """진행 로그 한 줄 (AGENTS.md 3장 형식)."""
    if result.get("ok"):
        text = result.get("summary", "")
        data = result.get("data")
        risk = data.get("risk_level") if isinstance(data, dict) else None
        warn = risk in WARN_RISK_LEVELS
    else:
        text = result.get("error", "")
        warn = True
    return {"tools": [name], "text": text[:LOG_TEXT_MAX_CHARS], "reason": reason, "warn": warn}


def _assistant_message(message: Any) -> dict[str, Any]:
    """SDK 응답 메시지를 대화 기록용 dict로 바꾼다."""
    entry: dict[str, Any] = {"role": "assistant", "content": message.content}
    if message.tool_calls:
        entry["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.function.name, "arguments": call.function.arguments},
            }
            for call in message.tool_calls
        ]
    return entry


def _default_client() -> Any:
    from openai import OpenAI  # 테스트에서는 가짜 클라이언트를 넘기므로 여기서 불러온다.

    return OpenAI()


def run_agent(
    user_input: UserInput,
    on_step: StepCallback | None = None,
    client: Any | None = None,
    tools: dict[str, ToolSpec] | None = None,
    max_steps: int | None = None,
    model: str | None = None,
) -> AgentResult:
    """에이전트 루프를 돌려 최종 보고서를 만든다.

    Args:
        user_input: 화면에서 받은 조건.
        on_step: 진행 로그 한 줄이 생길 때마다 부르는 함수 (화면 실시간 표시용).
        client: OpenAI 클라이언트. 없으면 환경 변수 OPENAI_API_KEY로 만든다.
        tools: 쓸 도구 모음. 없으면 registry.TOOLS.
        max_steps: LLM 호출 횟수 상한. 없으면 AGENT_MAX_STEPS 또는 10.
        model: 모델 ID. 없으면 LLM_MODEL 또는 gpt-4o.

    Returns:
        AgentResult. ok가 True면 report에 AGENTS.md 3장 보고서가 들어 있다.
    """
    tools = tools or TOOLS
    client = client or _default_client()
    model = model or os.getenv("LLM_MODEL") or DEFAULT_MODEL
    if max_steps is None:
        max_steps = int(os.getenv("AGENT_MAX_STEPS") or DEFAULT_MAX_STEPS)
    max_steps = max(1, max_steps)

    ctx = user_input.session_context()
    result = AgentResult(ok=False, report=None)
    schemas = openai_tools(tools)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": build_user_message(
                user_input.region,
                user_input.housing_type,
                user_input.contract_type,
                user_input.building_name,
                user_input.exclusive_area_m2,
            ),
        },
    ]

    def emit(entry: dict[str, Any]) -> None:
        result.steps.append(entry)
        if on_step:
            on_step(entry)

    for step in range(max_steps):
        last_step = step == max_steps - 1
        # tool_choice 강제는 마지막 반복에서만 쓴다. 반복 한도에 닿아도 빈손으로 끝나지 않고
        # 그때까지 모은 정보로 보고서를 내게 하기 위해서다. 그 전에는 LLM이 자유롭게 고른다.
        tool_choice: Any = (
            {"type": "function", "function": {"name": FINAL_TOOL}} if last_step else "auto"
        )
        try:
            response = client.chat.completions.create(
                model=model, messages=messages, tools=schemas, tool_choice=tool_choice
            )
        except Exception as exc:
            logger.warning("LLM 호출 실패: %s", type(exc).__name__)
            result.error = "LLM 호출에 실패했습니다. API 키와 네트워크를 확인하세요."
            emit({"tools": [], "text": result.error, "reason": "", "warn": True})
            return result

        message = response.choices[0].message
        messages.append(_assistant_message(message))

        if not message.tool_calls:
            messages.append({"role": "user", "content": NUDGE_MESSAGE})
            continue

        for call in message.tool_calls:
            name = call.function.name
            result.tool_trace.append(name)
            try:
                args = json.loads(call.function.arguments or "{}")
                if not isinstance(args, dict):
                    raise ValueError
            except ValueError:
                args = None
            reason = str(args.pop(REASON_PARAM, "")) if args is not None else ""

            spec = tools.get(name)
            if spec is None:
                tool_result = _fail(f"{name}은(는) 없는 도구입니다. 도구 목록에 있는 것만 쓰세요.")
            elif args is None:
                tool_result = _fail("인자가 올바른 JSON 객체가 아닙니다. 다시 부르세요.")
            else:
                tool_result = execute_tool(spec, args, ctx)

            emit(_step_log(name, reason, tool_result))
            visible = _visible_result(spec, tool_result) if spec else tool_result
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(visible, ensure_ascii=False, default=str),
                }
            )

            if spec and spec.ends_loop and tool_result.get("ok"):
                result.ok = True
                result.report = tool_result["data"]
                return result

    result.error = f"최대 반복 횟수({max_steps}회) 안에 보고서를 만들지 못했습니다."
    emit({"tools": [], "text": result.error, "reason": "", "warn": True})
    return result
