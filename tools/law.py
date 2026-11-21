"""주택임대차 법령·전세사기 예방·HUG 반환보증의 근거를 검색하는 도구."""

from __future__ import annotations

import logging
import re
from decimal import Decimal
from typing import Any

from langchain_core.documents import Document

from rag.vector_store import get_vector_store

logger = logging.getLogger(__name__)

ACT_FILE = "housing_lease_act.pdf"
DECREE_FILE = "housing_lease_decree.pdf"
GUIDE_FILE = "jeonse_fraud_guide.pdf"
HUG_OVERVIEW_FILE = "hug_guarantee_overview.pdf"
HUG_TERMS_FILE = "hug_guarantee_terms.pdf"

DEFAULT_RESULTS = 4
MAX_RESULTS = 8
SEARCH_CANDIDATES = 15
EVIDENCE_CHAR_LIMIT = 1000

RATE_LABELS = ("70% 이하", "70% 초과~80% 이하", "80% 초과")
HUG_RATE_AMOUNTS = (
    "1억 원 이하",
    "1억 원 초과~2억 원 이하",
    "2억 원 초과~5억 원 이하",
    "5억 원 초과",
)


def _get_rows(store: Any, source: str) -> list[Document]:
    """특정 PDF의 저장된 청크를 임베딩 API 호출 없이 읽는다."""
    response = store.get(
        where={"source": source},
        include=["documents", "metadatas"],
    )
    result: list[Document] = []
    for content, metadata in zip(
        response.get("documents") or [],
        response.get("metadatas") or [],
    ):
        if content and metadata:
            result.append(Document(page_content=content, metadata=metadata))
    return result


def _evidence(document: Document) -> dict[str, Any]:
    """원문 출처를 포함한 근거를 반환한다."""
    metadata = document.metadata
    return {
        "source": metadata.get("source", ""),
        "title": metadata.get("title", ""),
        "article": metadata.get("article", ""),
        "page": metadata.get("page"),
        "page_end": metadata.get("page_end", metadata.get("page")),
        "section_type": metadata.get("section_type", ""),
        "source_url": metadata.get("source_url", ""),
        "source_date": metadata.get("source_date", ""),
        "text": document.page_content[:EVIDENCE_CHAR_LIMIT],
    }


def _result(
    documents: list[Document],
    *,
    category: str,
    summary: str,
    additional: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """팀 표준 성공 응답을 만든다."""
    data: dict[str, Any] = {
        "category": category,
        "evidence": [_evidence(document) for document in documents],
    }
    if additional:
        data.update(additional)
    return {"ok": True, "data": data, "summary": summary[:200]}


def _explicit_deposit_bucket(question: str) -> str | None:
    """질문에서 HUG 표의 명시적 보증금 구간을 찾는다."""
    compact = re.sub(r"\s+", "", question)
    cases = (
        (r"2억원?초과[~～-]?5억원?이하", HUG_RATE_AMOUNTS[2]),
        (r"1억원?초과[~～-]?2억원?이하", HUG_RATE_AMOUNTS[1]),
        (r"5억원?초과", HUG_RATE_AMOUNTS[3]),
        (r"1억원?이하", HUG_RATE_AMOUNTS[0]),
    )
    for pattern, bucket in cases:
        if re.search(pattern, compact):
            return bucket
    return None


def _parse_deposit_won(question: str) -> int | None:
    """보증금을 원 단위로 해석한다.

    지원 예시: 1억 원, 1억 5천만원, 1억 5천5백만 원,
              1억 5000만원, 5000만원, 5천만원, 1.5억원.
    명시적 구간("1억 초과")과 불완전한 금액은 임의로 추정하지 않는다.
    """
    prefix = r"(?:전세\s*)?보증금(?:액)?\s*(?:은|는|이|가)?\s*"
    start = re.search(prefix, question)
    if not start:
        return None

    tail = question[start.end():]
    eok_match = re.match(r"(\d+(?:\.\d+)?)\s*억(?:\s*원)?", tail)
    if eok_match:
        eok_won = int(Decimal(eok_match.group(1)) * 100_000_000)
        tail = tail[eok_match.end():]
    else:
        eok_won = 0

    # '5천5백만원'은 만 원 단위 5,500으로 취급한다.
    # '5000만원', '5천만원', '5천5백만 원' 모두 처리한다.
    man_match = re.match(
        r"\s*(?:(?P<thousand>\d+)\s*천\s*)?"
        r"(?:(?P<hundred>\d+)\s*백\s*)?"
        r"(?:(?P<ten>\d+)\s*십\s*)?"
        r"(?:(?P<ones>\d[\d,]*)\s*)?만\s*원?",
        tail,
    )
    if man_match:
        parts = man_match.groupdict()
        if not any(value is not None for value in parts.values()):
            return None
        man_units = (
            int(parts["thousand"] or 0) * 1000
            + int(parts["hundred"] or 0) * 100
            + int(parts["ten"] or 0) * 10
            + int((parts["ones"] or "0").replace(",", ""))
        )
        # '10천만원' 등 비표준·모호한 표기는 자동 보정하지 않는다.
        if any(int(parts[name] or 0) >= 10 for name in ("thousand", "hundred", "ten")):
            return None
        if man_units >= 10_000:
            return None
        return eok_won + man_units * 10_000

    if eok_match:
        # '1억 초과'는 정확한 보증금이 아닌 조건이므로 숫자로 확정하지 않는다.
        # '1억 5천'처럼 단위가 누락된 경우에도 임의로 해석하지 않는다.
        if re.match(r"\s*(?:초과|이상|미만|\d)", tail):
            return None
        return eok_won

    return None


def _parse_debt_ratio_pct(question: str) -> float | None:
    """'부채비율 75%'에서 퍼센트 단위의 비율을 추출한다."""
    match = re.search(r"부채\s*비율\s*(?:이|은|가)?\s*[:=]?\s*(\d+(?:\.\d+)?)\s*%", question)
    if not match:
        return None
    ratio = float(match.group(1))
    return ratio if 0 <= ratio <= 100 else None


def _parse_housing_type(question: str) -> str | None:
    """보증료율표의 주택유형 구분(아파트/기타)을 판정한다."""
    if "아파트" in question:
        return "아파트"
    if any(word in question for word in ("기타", "빌라", "연립", "다세대", "다가구", "단독", "오피스텔")):
        return "기타"
    return None


def _rate_bucket(deposit_won: int) -> str:
    """보증금 금액을 HUG 보증료율표 구간으로 분류한다."""
    if deposit_won <= 100_000_000:
        return HUG_RATE_AMOUNTS[0]
    if deposit_won <= 200_000_000:
        return HUG_RATE_AMOUNTS[1]
    if deposit_won <= 500_000_000:
        return HUG_RATE_AMOUNTS[2]
    return HUG_RATE_AMOUNTS[3]


def _rate_label(ratio_pct: float) -> str:
    """부채비율을 HUG 보증료율표의 구간으로 분류한다."""
    if ratio_pct <= 70:
        return RATE_LABELS[0]
    if ratio_pct <= 80:
        return RATE_LABELS[1]
    return RATE_LABELS[2]


def _search_hug_rate(store: Any, question: str) -> dict[str, Any]:
    """보증금·주택유형·부채비율이 모두 있을 때만 보증료율을 확정 검색한다."""
    explicit_bucket = _explicit_deposit_bucket(question)
    deposit_won = _parse_deposit_won(question)
    housing_type = _parse_housing_type(question)
    ratio_pct = _parse_debt_ratio_pct(question)
    if (deposit_won is None and explicit_bucket is None) or housing_type is None or ratio_pct is None:
        return {
            "ok": True,
            "data": {
                "category": "hug_premium_rate",
                "needs_more_information": True,
                "required": [
                    item for item, value in (
                        ("보증금액", deposit_won if deposit_won is not None else explicit_bucket),
                        ("주택유형", housing_type),
                        ("부채비율", ratio_pct),
                    ) if value is None
                ],
                "evidence": [],
            },
            "summary": "HUG 보증료율을 확인하려면 보증금액·주택유형·부채비율을 알려주세요.",
        }

    amount_bucket = explicit_bucket or _rate_bucket(deposit_won)
    ratio_label = _rate_label(ratio_pct)
    candidates = _get_rows(store, HUG_OVERVIEW_FILE)
    matched = [
        doc for doc in candidates
        if doc.metadata.get("section_type") == "structured_premium_table"
        and doc.metadata.get("deposit_range") == amount_bucket
        and doc.metadata.get("housing_type") == housing_type
    ]
    if len(matched) != 1:
        return {
            "ok": False,
            "error": "HUG 보증료율표의 해당 조건을 찾지 못했습니다. ChromaDB의 원본 문서를 확인하고 다시 구축해 주세요.",
        }

    selected_line = next(
        (line for line in matched[0].page_content.splitlines()
         if line.startswith(f"부채비율 {ratio_label}:")),
        "",
    )
    rate_match = re.search(r"연\s*(\d+(?:\.\d+)?)%", selected_line)
    if not rate_match:
        return {
            "ok": False,
            "error": "해당 부채비율 구간의 보증료율을 읽지 못했습니다. HUG 요율표 청크를 확인해 주세요.",
        }

    rate_pct = float(rate_match.group(1))
    deposit_description = (
        f"보증금 구간 {amount_bucket}" if explicit_bucket
        else f"보증금 {deposit_won:,}원"
    )
    return _result(
        matched,
        category="hug_premium_rate",
        summary=f"HUG 연 보증료율 {rate_pct:.3f}% ({deposit_description}, {housing_type}, 부채비율 {ratio_pct:g}%). 보증 가입 가능 여부는 별도 심사 대상입니다.",
        additional={
            "deposit_won": deposit_won,
            "housing_type": housing_type,
            "debt_ratio_pct": ratio_pct,
            "deposit_range": amount_bucket,
            "debt_ratio_range": ratio_label,
            "premium_rate_pct": rate_pct,
            "needs_more_information": False,
        },
    )


def _search_priority_decree(store: Any, question: str, max_results: int) -> dict[str, Any]:
    """소액임차인 보호 질문은 현행 시행령 제10·11조에서 직접 찾는다."""
    documents = _get_rows(store, DECREE_FILE)
    requested = ("제10조", "제11조")
    priority: list[Document] = []
    for number in requested:
        matching = [
            doc for doc in documents
            if doc.metadata.get("section_type") == "current_article"
            and str(doc.metadata.get("article", "")).startswith(number + "(")
        ]
        matching.sort(key=lambda doc: doc.metadata.get("part", 0))
        priority.extend(matching[:2])

    if not priority:
        return {"ok": False, "error": "현행 시행령 제10·11조 청크를 찾지 못했습니다. 문서 분할과 ChromaDB 저장을 확인해 주세요."}
    return _result(
        priority[:max_results],
        category="small_tenant_protection",
        summary="현행 주택임대차보호법 시행령 제10조(최우선변제액)와 제11조(소액임차인 범위)를 검색했습니다. 담보물권 설정일에 따른 경과규정은 별도 확인이 필요합니다.",
        additional={"requires_collateral_date_check": True},
    )


def _choose_source(question: str) -> str | None:
    """질문의 주제에 맞는 우선 검색 문서를 선택한다."""
    lowered = question.lower()
    if any(word in lowered for word in ("면책", "보증사고", "이행청구", "보증이행", "보증책임", "약관")):
        return HUG_TERMS_FILE
    if any(word in lowered for word in ("hug", "반환보증", "보증보험", "보증한도", "보증료", "가입 조건", "가입조건")):
        return HUG_OVERVIEW_FILE
    if any(word in lowered for word in ("전세사기", "전세 사기", "체크리스트", "신탁사기", "깡통전세", "계약 전 확인")):
        return GUIDE_FILE
    if any(word in lowered for word in ("대항력", "확정일자", "갱신", "임차권등기", "임대차보호법", "차임")):
        return ACT_FILE
    return None


def _rank_docs(question: str, documents: list[Document], source: str | None, limit: int) -> list[Document]:
    """보증료율이 아닌 질문에서는 표를 제외하고 관련 조항을 우선한다."""
    filtered = [
        doc for doc in documents
        if doc.metadata.get("section_type") not in (
            "structured_premium_table", "supplementary_provision"
        )
    ]
    if not filtered:
        filtered = documents

    def score(doc: Document) -> int:
        content = doc.page_content.replace(" ", "").lower()
        score_value = 0
        if source == HUG_OVERVIEW_FILE:
            if any(word in question for word in ("가입", "보증한도", "보증 조건", "보증조건")):
                score_value += 8 * int("보증한도" in content)
                score_value += 6 * int("보증조건" in content)
                score_value += 5 * int("보증대상주택" in content)
                score_value += 3 * int(doc.metadata.get("page") in (2, 3))
        return score_value

    # 원래 유사도 순위는 동점 때 그대로 유지한다.
    return sorted(filtered, key=score, reverse=True)[:limit]


def search_law(query: str, max_results: int = DEFAULT_RESULTS) -> dict[str, Any]:
    """법령·전세사기 예방·HUG 반환보증의 원문 근거를 검색한다.

    전·월세 계약의 법적 위험요인이나 보증보험 조건을 설명해야 할 때 사용한다.
    시세 계산 또는 청년 지원정책 자격 판정에는 사용하지 않는다.

    Args:
        query: 검색할 법률 또는 보증 관련 질문 문자열.
        max_results: 반환할 근거 수(1~8, 기본 4).

    Returns:
        성공 시 {ok: True, data: {evidence: [...]}, summary: ...}.
        실패 시 {ok: False, error: ...}.
    """
    if not isinstance(query, str) or not query.strip():
        return {"ok": False, "error": "검색어가 비어 있습니다. 법률 또는 보증 관련 질문을 입력해 주세요."}
    if not isinstance(max_results, int) or not 1 <= max_results <= MAX_RESULTS:
        return {"ok": False, "error": "max_results는 1~8 사이 정수로 지정해 주세요."}

    try:
        # 일반 검색과 구분하여 조건·법 조항을 정확히 찾는다.
        store = get_vector_store()
        if "보증료율" in query or ("보증료" in query and "부채비율" in query):
            return _search_hug_rate(store, query)
        if any(term in query for term in ("최우선변제", "최우선 변제", "소액임차인", "소액 임차인")):
            return _search_priority_decree(store, query, max_results)

        source = _choose_source(query)
        # HUG 가입 조건·보증한도는 요율표보다 상품개요 2~3쪽을 먼저 확인한다.
        if source == HUG_OVERVIEW_FILE and any(
            word in query for word in ("가입", "보증한도", "보증 조건", "보증조건")
        ):
            overview = [
                doc for doc in _get_rows(store, HUG_OVERVIEW_FILE)
                if doc.metadata.get("section_type") == "product_info"
                and doc.metadata.get("page") in (2, 3)
            ]
            if overview:
                selected = _rank_docs(query, overview, source, max_results)
                return _result(
                    selected,
                    category="hug_guarantee_conditions",
                    summary=f"HUG 반환보증 상품개요 2~3쪽에서 가입 조건·보증한도 근거 {len(selected)}개를 찾았습니다. 실제 가입 가능 여부는 HUG 심사로 확인해야 합니다.",
                )

        filter_by = {"source": source} if source else None
        documents = store.similarity_search(
            query, k=SEARCH_CANDIDATES, filter=filter_by
        )
        selected = _rank_docs(query, documents, source, max_results)
        if not selected:
            return {
                "ok": False,
                "error": "관련 근거 문서를 찾지 못했습니다. 질문을 구체화하거나 RAG 문서 범위를 확인해 주세요.",
            }
        source_names = sorted({doc.metadata.get("title", "") for doc in selected})
        return _result(
            selected,
            category="document_search",
            summary=f"{len(selected)}개 법률·안내 근거 검색: {', '.join(name for name in source_names if name)}. 적용 시점과 실제 계약 자료를 함께 확인해 주세요.",
        )
    except Exception as exc:
        logger.warning("RAG 검색 오류 유형: %s", type(exc).__name__)
        return {
            "ok": False,
            "error": "법률 근거 검색에 실패했습니다. Ubuntu 가상환경, OpenAI API 설정과 data/chroma DB를 확인한 뒤 다시 시도해 주세요.",
        }
