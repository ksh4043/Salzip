"""도구 등록과 도구 스키마 모음 (담당 A).

도구 이름은 AGENTS.md 2장 표와 똑같이 쓴다.
스키마의 description은 각 도구 함수의 docstring과 내용이 맞아야 한다.

개인정보 원칙(AGENTS.md 4장)을 지키려고 도구마다 아래 세 가지를 따로 적는다.
- parameters: LLM이 보는 인자. 사용자 원본 입력(보증금, 나이·소득 구간)은 여기 넣지 않는다.
- injected: 실행 직전에 에이전트 코어가 세션 값으로 채우는 인자 {함수 인자 이름: 세션 키}.
- hidden_result_keys: LLM에 보내는 결과 사본에서 지우는 data 키 (원본 입력을 되돌려 주는 값).
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any, Callable

# 모든 도구 호출에 붙이는 "호출 이유" 인자. 진행 로그의 reason으로 쓰고 도구에는 넘기지 않는다.
REASON_PARAM = "reason"
REASON_SCHEMA = {
    "type": "string",
    "description": "이 도구를 지금 부르는 이유 한 문장. 사용자 화면의 진행 로그에 보인다.",
}


@dataclass(frozen=True)
class ToolSpec:
    """LLM에 보여줄 도구 한 개의 정의."""

    name: str
    func_path: str  # "모듈:함수". 실행할 때 불러온다 (RAG 라이브러리가 없어도 나머지 도구는 동작하게).
    description: str
    parameters: dict[str, Any]
    injected: dict[str, str] = field(default_factory=dict)
    hidden_result_keys: tuple[str, ...] = ()
    # 성공 결과의 data 값을 세션에 저장한다 {data 키: 세션 키}. 보고서 숫자를 채울 때 쓴다.
    exports: dict[str, str] = field(default_factory=dict)
    ends_loop: bool = False

    def resolve(self) -> Callable[..., dict[str, Any]]:
        module_name, attr = self.func_path.split(":")
        return getattr(importlib.import_module(module_name), attr)

    def openai_schema(self) -> dict[str, Any]:
        """OpenAI Chat Completions의 tools 항목 형식으로 만든다."""
        properties = dict(self.parameters.get("properties", {}))
        properties[REASON_PARAM] = REASON_SCHEMA
        required = list(self.parameters.get("required", [])) + [REASON_PARAM]
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
            },
        }


# 세션 키 (agent/core.py가 채운다)
CTX_DEPOSIT = "deposit_won"
CTX_JEONSE_RATIO = "jeonse_ratio"
CTX_THRESHOLD = "threshold"
CTX_MARKET_PRICE = "market_price"
CTX_RISK_LEVEL = "risk_level"
CTX_RISK_NOTE = "risk_note"


SEARCH_PRICE = ToolSpec(
    name="search_price",
    func_path="tools.price:search_price",
    description=(
        "국토교통부 실거래가로 매물 주변의 매매 시세와 전세 시세를 조회한다. "
        "언제 쓰나: 전세가율을 계산하기 전에 매매 시세가 필요할 때 먼저 부른다. "
        "결과의 data.market_price_won을 calc_jeonse_ratio에 넘긴다. "
        "매매 거래가 0건이면 months를 늘려(최대 24) 다시 조회한다. "
        "쓰지 말 것: 법·보증보험 근거나 청년 정책을 찾을 때. 오피스텔은 지원하지 않는다(아파트·연립다세대만)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "region": {
                "type": "string",
                "description": '시/도, 시/군/구, 법정동까지의 지역. 예: "서울 강서구 화곡동". 번지·동호수는 넣지 않는다. 서울·인천·경기만 지원.',
            },
            "housing_type": {
                "type": "string",
                "enum": ["아파트", "연립다세대"],
                "description": "주택 유형. 빌라·다세대·연립은 연립다세대.",
            },
            "building_name": {
                "type": ["string", "null"],
                "description": "단지·건물명. 주면 같은 단지 거래를 먼저 비교한다. 모르면 null.",
            },
            "exclusive_area_m2": {
                "type": ["number", "null"],
                "description": "전용면적(㎡). 공급면적이 아니다. 모르면 null.",
            },
            "months": {
                "type": "integer",
                "minimum": 1,
                "maximum": 24,
                "description": "조회할 최근 개월 수(이번 달 포함). 기본 6.",
            },
        },
        "required": ["region", "housing_type"],
    },
)

CALC_JEONSE_RATIO = ToolSpec(
    name="calc_jeonse_ratio",
    func_path="tools.price:calc_jeonse_ratio",
    description=(
        "보증금 ÷ 매매 시세로 전세가율을 계산하고 위험 등급(70% 미만 안전, 70~80% 주의, 80% 초과 위험)을 정한다. "
        "보증금은 사용자 입력에서 자동으로 채워지므로 넘기지 않는다. "
        "언제 쓰나: search_price로 매매 시세를 얻은 뒤 부른다. 결과 등급으로 다음 행동을 정한다. "
        "쓰지 말 것: 매매 시세가 없을 때(매매 0건) 시세를 지어내 넣지 않는다."
    ),
    parameters={
        "type": "object",
        "properties": {
            "market_price_won": {
                "type": "integer",
                "description": "매매 시세(원). search_price 결과의 data.market_price_won 값을 그대로 넣는다.",
            },
        },
        "required": ["market_price_won"],
    },
    injected={"deposit_won": CTX_DEPOSIT},
    # margin_won(시세 - 보증금)은 시세와 함께 보면 보증금을 되살릴 수 있어서 같이 숨긴다.
    hidden_result_keys=("deposit_won", "margin_won"),
    exports={
        "jeonse_ratio_pct": CTX_JEONSE_RATIO,
        "threshold_pct": CTX_THRESHOLD,
        "market_price_won": CTX_MARKET_PRICE,
        "risk_level": CTX_RISK_LEVEL,
        "risk_note": CTX_RISK_NOTE,
    },
)

SEARCH_LAW = ToolSpec(
    name="search_law",
    func_path="tools.law:search_law",
    description=(
        "주택임대차보호법·시행령, 전세사기 예방 가이드, HUG 전세보증금반환보증 문서에서 원문 근거를 검색한다. "
        "언제 쓰나: 전세가율이 주의·위험이거나 계약의 법적 위험(대항력, 확정일자, 최우선변제, 보증보험 가입 조건 등)을 "
        "설명할 근거가 필요할 때. 보증료율을 물을 때는 보증금액·주택유형·부채비율을 질문에 함께 적는다. "
        "쓰지 말 것: 시세 계산이나 청년 지원정책 자격 판정."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "검색할 법률·보증 관련 질문 문장."},
            "max_results": {
                "type": "integer",
                "minimum": 1,
                "maximum": 8,
                "description": "반환할 근거 수. 기본 4.",
            },
        },
        "required": ["query"],
    },
)

SUBMIT_REPORT = ToolSpec(
    name="submit_report",
    func_path="tools.report:submit_report",
    description=(
        "최종 보고서를 제출하고 작업을 끝낸다. "
        "언제 쓰나: 위험 진단과 필요한 근거·정책 확인을 마쳐 결론이 섰을 때 마지막에 한 번. "
        "형식이 틀리면 실패가 돌아오니 고쳐서 다시 부른다. "
        "전세가율·시세·보증금 숫자는 자동으로 채워지므로 넘기지 않는다. "
        "calc_jeonse_ratio를 성공했다면 risk_level·risk_note도 그 결과로 덮어쓴다. "
        "쓰지 말 것: 아직 전세가율을 계산하지 않았거나 확인할 근거가 남았을 때."
    ),
    parameters={
        "type": "object",
        "properties": {
            "risk_level": {"type": "string", "enum": ["위험", "주의", "안전"]},
            "risk_note": {"type": "string", "description": '한 줄 권고. 예: "재검토 권고".'},
            "checklist": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 15,
                "description": "계약 전에 확인할 항목. 도구로 찾은 근거에 바탕을 둔다.",
            },
            "policies": {
                "type": "array",
                "maxItems": 10,
                "description": "신청할 정책. 자격 판정으로 확인했으면 해당, 판정하지 못했으면 확인 필요. 없으면 빈 목록.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "status": {"type": "string", "enum": ["해당", "확인 필요"]},
                    },
                    "required": ["name", "status"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["risk_level", "risk_note", "checklist", "policies"],
    },
    injected={
        "jeonse_ratio": CTX_JEONSE_RATIO,
        "threshold": CTX_THRESHOLD,
        "market_price": CTX_MARKET_PRICE,
        "deposit": CTX_DEPOSIT,
        "risk_level": CTX_RISK_LEVEL,
        "risk_note": CTX_RISK_NOTE,
    },
    ends_loop=True,
)

# TODO(D): tools/policy.py의 search_policy, check_eligibility가 구현되면 여기에 등록한다.
#   나이·소득 구간·무주택 여부는 parameters가 아니라 injected로 받는다 (AGENTS.md 4장).

TOOLS: dict[str, ToolSpec] = {
    spec.name: spec
    for spec in (SEARCH_PRICE, CALC_JEONSE_RATIO, SEARCH_LAW, SUBMIT_REPORT)
}

FINAL_TOOL = SUBMIT_REPORT.name


def openai_tools(tools: dict[str, ToolSpec] | None = None) -> list[dict[str, Any]]:
    """LLM에 넘길 도구 스키마 목록."""
    return [spec.openai_schema() for spec in (tools or TOOLS).values()]
