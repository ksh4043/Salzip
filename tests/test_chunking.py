"""API 호출 없이 RAG 청크와 중요한 숫자·출처를 검증한다."""

import pytest

from rag.chunking import HUG_RATE_TABLE, build_chunks


@pytest.fixture(scope="module")
def chunks():
    """원본 PDF 5개를 읽어 테스트용 청크를 만든다."""
    return build_chunks()


def test_all_five_sources(chunks):
    """RAG 대상 문서 5개가 모두 포함되는지 확인한다."""
    sources = {doc.metadata["source"] for doc in chunks}
    assert sources == {
        "housing_lease_act.pdf",
        "housing_lease_decree.pdf",
        "jeonse_fraud_guide.pdf",
        "hug_guarantee_overview.pdf",
        "hug_guarantee_terms.pdf",
    }
    assert all(doc.page_content.strip() for doc in chunks)
    assert all(doc.metadata.get("source_url") for doc in chunks)


def test_decree_articles_and_amounts(chunks):
    """시행령 제10·11조의 지역별 기준이 손상되지 않았는지 확인한다."""
    decree = [
        doc for doc in chunks
        if doc.metadata["source"] == "housing_lease_decree.pdf"
        and doc.metadata["section_type"] == "current_article"
    ]
    assert any(
        doc.metadata["article"].startswith("제10조")
        and "5천500만원" in doc.page_content
        for doc in decree
    )
    assert any(
        doc.metadata["article"].startswith("제11조")
        and "1억6천500만원" in doc.page_content
        for doc in decree
    )


def test_hug_rates_are_complete(chunks):
    """원본과 대조한 보증료율 24개가 8개 조건별 청크에 들어가는지 확인한다."""
    rows = [
        doc for doc in chunks
        if doc.metadata["section_type"] == "structured_premium_table"
    ]
    assert len(rows) == len(HUG_RATE_TABLE) == 8

    for amount, housing, rates in HUG_RATE_TABLE:
        matching = [
            doc for doc in rows
            if doc.metadata["deposit_range"] == amount
            and doc.metadata["housing_type"] == housing
        ]
        assert len(matching) == 1
        assert all(f"연 {rate}%" in matching[0].page_content for rate in rates)

    ending_row = next(
        doc for doc in rows
        if doc.metadata["deposit_range"] == "5억 원 초과"
        and doc.metadata["housing_type"] == "기타"
    )
    assert ending_row.metadata["page_end"] == 8
    assert "연 0.211%" in ending_row.page_content


def test_terms_exclusions_are_present(chunks):
    """HUG 약관의 면책 조항을 조항 단위로 검색할 수 있는지 확인한다."""
    assert any(
        doc.metadata["source"] == "hug_guarantee_terms.pdf"
        and doc.metadata["article"].startswith("제3조")
        for doc in chunks if "article" in doc.metadata
    )
