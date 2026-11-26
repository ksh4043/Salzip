"""도구 스키마가 연결 규칙(AGENTS.md 2·3·4장)을 지키는지 검증한다."""

import inspect

import pytest

from agent.registry import REASON_PARAM, TOOLS, openai_tools

AGENTS_TOOL_NAMES = {
    "search_price", "calc_jeonse_ratio", "search_law",
    "search_policy", "check_eligibility", "submit_report",
}
# LLM에 보이면 안 되는 사용자 원본 입력 (AGENTS.md 4장)
RAW_INPUT_PARAMS = {"deposit_won", "deposit", "age_band", "income_band"}


def test_tool_names_follow_agents_md() -> None:
    assert set(TOOLS) <= AGENTS_TOOL_NAMES
    for name, spec in TOOLS.items():
        assert spec.name == name


def test_schemas_have_reason_and_no_raw_input() -> None:
    for schema in openai_tools():
        params = schema["function"]["parameters"]
        assert REASON_PARAM in params["properties"]
        assert REASON_PARAM in params["required"]
        assert not RAW_INPUT_PARAMS & set(params["properties"]), schema["function"]["name"]
        assert schema["function"]["description"]


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_schema_params_match_function_signature(name: str) -> None:
    spec = TOOLS[name]
    if name == "search_law":
        pytest.importorskip("langchain_core")
    signature = inspect.signature(spec.resolve())
    visible = set(spec.parameters["properties"])
    assert visible | set(spec.injected) <= set(signature.parameters)
    for param in spec.parameters.get("required", []):
        assert param in signature.parameters
