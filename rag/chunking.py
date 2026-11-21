"""법령·전세사기 가이드·HUG 보증 문서를 검색용 청크로 분할한다."""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import date

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

from rag.pdf_loader import load_all_documents

logger = logging.getLogger(__name__)

# PDF 발행일/시행일은 각 PDF의 표지·본문 기준. URL은 공식 자료의 안내 페이지.
# 기관 홈페이지 URL만 기재한 자료는 추후 원문 상세 URL로 교체하는 것이 좋다.
SOURCES = {
    "housing_lease_act.pdf": {
        "title": "주택임대차보호법",
        "source_url": "https://www.law.go.kr",
        "source_date": "2026-01-02",  # 시행일
    },
    "housing_lease_decree.pdf": {
        "title": "주택임대차보호법 시행령",
        "source_url": "https://www.law.go.kr",
        "source_date": "2026-07-01",  # 시행일
    },
    "jeonse_fraud_guide.pdf": {
        "title": "전세사기 피해예방 종합안내서",
        "source_url": "https://www.molit.go.kr",
        "source_date": "2025-08-05",  # 안내서 게시일(원문과 확인 필요)
    },
    "hug_guarantee_overview.pdf": {
        "title": "HUG 전세보증금반환보증 상품개요",
        "source_url": "https://www.khug.or.kr/hug/web/ig/dr/igdr000001.jsp?tabMenu=Y",
        "source_date": "2026-11-16",  # 웹페이지 PDF 저장일
    },
    "hug_guarantee_terms.pdf": {
        "title": "HUG 전세보증금반환보증약관",
        "source_url": "https://www.khug.or.kr/hug/web/ig/dr/igdr000004.jsp",
        "source_date": "2024-05-03",  # 약관 시행일
    },
}

LEGAL_FILES = {"housing_lease_act.pdf", "housing_lease_decree.pdf"}
ARTICLE_RE = re.compile(r"(?m)^[ \t]*(제\d+조(?:의\d+)?\([^\n)]*\))")
ADDENDUM_RE = re.compile(r"(?m)^[ \t]*부\s*칙\s*<")
TERMS_ARTICLE_RE = ARTICLE_RE

SPLITTER = RecursiveCharacterTextSplitter(
    chunk_size=950,
    chunk_overlap=120,
    separators=["\n\n", "\n", ". ", " ", ""],
)

# 2026-11-16에 저장한 HUG 상품개요 7~8쪽의 보증료율표를 원본 순서대로 전사.
# 각 숫자는 아래 검증 함수에서 PDF 추출값과 한 번 더 비교한다.
HUG_RATE_TABLE = [
    ("1억 원 이하", "아파트", ("0.097", "0.117", "0.137")),
    ("1억 원 이하", "기타", ("0.111", "0.142", "0.172")),
    ("1억 원 초과~2억 원 이하", "아파트", ("0.102", "0.124", "0.146")),
    ("1억 원 초과~2억 원 이하", "기타", ("0.117", "0.151", "0.184")),
    ("2억 원 초과~5억 원 이하", "아파트", ("0.107", "0.131", "0.154")),
    ("2억 원 초과~5억 원 이하", "기타", ("0.124", "0.161", "0.197")),
    ("5억 원 초과", "아파트", ("0.113", "0.138", "0.164")),
    ("5억 원 초과", "기타", ("0.132", "0.172", "0.211")),
]


def clean_text(text: str) -> str:
    """반복 머리말·쪽번호 등 검색에 불필요한 요소만 제거한다."""
    text = text.replace("\u00a0", " ")
    text = re.sub(r"(?m)^[ \t]*법제처[ \t]+\d+[ \t]+국가법령정보센터[ \t]*$", "", text)
    text = re.sub(r"(?m)^[ \t]*-[ \t]*\d+[ \t]*-[ \t]*$", "", text)
    text = re.sub(r"(?m)^[ \t]*https?://www\.khug\.or\.kr/[^\n]*$", "", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def common_metadata(source: str) -> dict:
    """모든 청크에 문서명·공식 출처·시행 또는 저장 날짜를 붙인다."""
    if source not in SOURCES:
        raise ValueError(f"등록되지 않은 문서: {source}")
    return {
        "source": source,
        **SOURCES[source],
        "registered_on": date.today().isoformat(),
    }


def split_piece(text: str, metadata: dict, heading: str = "") -> list[Document]:
    """긴 조항은 나누되 모든 조각에 조항 제목과 동일한 출처를 남긴다."""
    text = clean_text(text)
    if not text:
        return []

    parts = SPLITTER.split_text(text)
    result = []
    for index, part in enumerate(parts, start=1):
        if heading and not part.lstrip().startswith(heading):
            part = f"{heading}\n{part}"
        result.append(
            Document(
                page_content=part,
                metadata={**metadata, "part": index, "part_total": len(parts)},
            )
        )
    return result


def split_articles(documents: list[Document], is_terms: bool = False) -> list[Document]:
    """여러 PDF 페이지에 걸친 조항도 하나의 단위로 모아 분할한다."""
    if not documents:
        return []

    source = documents[0].metadata["source"]
    ordered = sorted(
        documents,
        key=lambda d: (d.metadata["page"], 0 if d.metadata.get("side") == "left" else 1),
    )

    text_parts: list[str] = []
    spans: list[tuple[int, int, Document]] = []
    offset = 0
    for document in ordered:
        content = clean_text(document.page_content) + "\n"
        spans.append((offset, offset + len(content), document))
        text_parts.append(content)
        offset += len(content)

    whole_text = "".join(text_parts)

    def locate(position: int) -> Document:
        for start, end, document in spans:
            if start <= position < end:
                return document
        return spans[-1][2]

    # 과거 부칙은 현행 조문과 섞이지 않게 별도 분류한다.
    addendum = ADDENDUM_RE.search(whole_text) if not is_terms else None
    main_end = addendum.start() if addendum else len(whole_text)
    main_text = whole_text[:main_end]
    starts = list(ARTICLE_RE.finditer(main_text))
    result: list[Document] = []

    for i, match in enumerate(starts):
        start = match.start()
        end = starts[i + 1].start() if i + 1 < len(starts) else main_end
        heading = match.group(1)
        first = locate(start)
        last = locate(max(start, end - 1))
        metadata = {
            **common_metadata(source),
            "section_type": "terms_article" if is_terms else "current_article",
            "article": heading,
            "page": first.metadata["page"],
            "page_end": last.metadata["page"],
        }
        if is_terms:
            metadata["side"] = first.metadata.get("side", "left")
            metadata["side_end"] = last.metadata.get("side", "right")
        result.extend(split_piece(main_text[start:end], metadata, heading=heading))

    # 약관 머리말에는 개정 내역이 있다. 검색 조건과 혼합하지 않고 제외한다.
    # 본문이 끝난 뒤의 부칙은 별도 청크로 만들어 '현행 조항'보다 우선하지 않도록 한다.
    if addendum:
        addenda_text = whole_text[main_end:]
        headings = list(ADDENDUM_RE.finditer(addenda_text))
        for i, match in enumerate(headings):
            start = match.start()
            end = headings[i + 1].start() if i + 1 < len(headings) else len(addenda_text)
            absolute_start = main_end + start
            absolute_end = main_end + end
            first = locate(absolute_start)
            last = locate(max(absolute_start, absolute_end - 1))
            first_line = addenda_text[start:end].splitlines()[0].strip()
            result.extend(
                split_piece(
                    addenda_text[start:end],
                    {
                        **common_metadata(source),
                        "section_type": "supplementary_provision",
                        "article": first_line,
                        "requires_date_check": True,
                        "page": first.metadata["page"],
                        "page_end": last.metadata["page"],
                    },
                    heading=first_line,
                )
            )

    return result


def validate_hug_rates(documents: list[Document]) -> None:
    """수동으로 정리한 24개 보증료율이 PDF 7~8쪽 숫자와 같은지 검증한다."""
    by_page = {d.metadata["page"]: d.page_content for d in documents}
    if 7 not in by_page or 8 not in by_page:
        raise ValueError("HUG 상품개요 7~8페이지가 없어 보증료율을 검증할 수 없습니다.")
    from_pdf = re.findall(r"연\s*(0\.\d+)\s*%", by_page[7])
    first_rate_page8 = re.search(r"연\s*(0\.\d+)\s*%", by_page[8])
    if first_rate_page8:
        from_pdf.append(first_rate_page8.group(1))
    expected = [rate for _amount, _housing, rates in HUG_RATE_TABLE for rate in rates]
    if from_pdf != expected:
        raise ValueError(
            "HUG PDF의 보증료율과 구조화 표가 다릅니다. "
            f"PDF: {from_pdf}, 코드: {expected}"
        )


def build_hug_rate_chunks(documents: list[Document]) -> list[Document]:
    """표의 '보증금 구간→주택유형→부채비율→요율' 관계를 보존한다."""
    validate_hug_rates(documents)
    result = []
    ratios = ["70% 이하", "70% 초과~80% 이하", "80% 초과"]

    for amount, housing, rates in HUG_RATE_TABLE:
        lines = [
            "HUG 전세보증금반환보증 연 보증료율표 (상품개요 PDF 7~8페이지)",
            f"보증금액: {amount}",
            f"주택유형: {housing}",
        ]
        lines.extend(
            f"부채비율 {ratio}: 연 {rate}%" for ratio, rate in zip(ratios, rates)
        )
        lines.append(
            "부채비율 = (선순위채권금액 + 전세보증금) / (주택가격 × 90%) × 100"
        )
        lines.append("보증대상 전세보증금은 수도권 7억 원, 그 외 지역 5억 원 이하")
        # 맨 마지막 요율(5억 초과·기타·80% 초과)은 8쪽으로 이어진다.
        page_end = 8 if amount == "5억 원 초과" and housing == "기타" else 7
        result.append(
            Document(
                page_content="\n".join(lines),
                metadata={
                    **common_metadata("hug_guarantee_overview.pdf"),
                    "section_type": "structured_premium_table",
                    "deposit_range": amount,
                    "housing_type": housing,
                    "page": 7,
                    "page_end": page_end,
                },
            )
        )
    return result


def split_general(documents: list[Document]) -> list[Document]:
    """안내서와 상품 설명은 페이지별 문맥을 유지하면서 문단 중심 분할한다."""
    result = []
    for document in sorted(documents, key=lambda d: d.metadata["page"]):
        source = document.metadata["source"]
        page = document.metadata["page"]
        text = clean_text(document.page_content)

        if source == "hug_guarantee_overview.pdf":
            if page == 7:
                continue  # 구조화한 보증료율 8개 청크로 대체
            if page == 8:
                marker = "보증료 할인 및 할증"
                if marker not in text:
                    raise ValueError("HUG 8쪽의 보증료 할인 구역을 찾지 못했습니다.")
                text = text[text.index(marker):]  # 8쪽 상단의 표 잔여부분 제거

        result.extend(
            split_piece(
                text,
                {
                    **common_metadata(source),
                    "section_type": "guide" if source == "jeonse_fraud_guide.pdf" else "product_info",
                    "page": page,
                    "page_end": page,
                },
            )
        )
    return result


def build_chunks(documents: list[Document] | None = None) -> list[Document]:
    """PDF 5개를 출처와 조항·표 조건이 보존된 검색용 청크로 변환한다."""
    if documents is None:
        documents = load_all_documents()

    grouped: dict[str, list[Document]] = defaultdict(list)
    for document in documents:
        grouped[document.metadata["source"]].append(document)

    chunks: list[Document] = []
    for source in SOURCES:
        group = grouped[source]
        if not group:
            raise ValueError(f"불러온 PDF 중 누락된 문서: {source}")
        if source in LEGAL_FILES:
            result = split_articles(group)
        elif source == "hug_guarantee_terms.pdf":
            result = split_articles(group, is_terms=True)
        else:
            result = split_general(group)
            if source == "hug_guarantee_overview.pdf":
                result.extend(build_hug_rate_chunks(group))
        chunks.extend(result)
        logger.info("%s: %d개 청크", source, len(result))

    logger.info("총 %d개 검색용 청크 생성", len(chunks))
    return chunks


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    result = build_chunks()
    examples = [chunk for chunk in result if chunk.metadata["section_type"] == "structured_premium_table"]
    logger.info("\n[HUG 보증료율 청크 예시]\n%s", examples[0].page_content)
