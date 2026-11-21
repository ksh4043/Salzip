"""외부 API 없이 search_law의 근거 선택과 반환 형식을 검증한다."""

import tools.law as law
from langchain_core.documents import Document


def make_doc(text, *, source, section_type, article="", page=1, **extra):
    return Document(
        page_content=text,
        metadata={
            "source": source,
            "title": "테스트 원문",
            "section_type": section_type,
            "article": article,
            "page": page,
            "source_date": "2026-11-16",
            "source_url": "https://example.org/sample",
            **extra,
        },
    )


class FakeStore:
    """테스트 중 임베딩 API를 호출하지 않는 대체 저장소."""

    def __init__(self, docs):
        self.docs = docs
        self.similarity_calls = 0

    def get(self, *, where=None, include=None):
        matches = [doc for doc in self.docs if doc.metadata["source"] == where["source"]]
        return {
            "documents": [doc.page_content for doc in matches],
            "metadatas": [doc.metadata for doc in matches],
        }

    def similarity_search(self, query, k, filter=None):
        self.similarity_calls += 1
        if filter:
            return [doc for doc in self.docs if doc.metadata["source"] == filter["source"]][:k]
        return self.docs[:k]


def test_exact_hug_rate(monkeypatch):
    table = make_doc(
        "보증금액: 1억 원 이하\n주택유형: 아파트\n"
        "부채비율 70% 이하: 연 0.097%\n"
        "부채비율 70% 초과~80% 이하: 연 0.117%\n"
        "부채비율 80% 초과: 연 0.137%",
        source=law.HUG_OVERVIEW_FILE,
        section_type="structured_premium_table",
        deposit_range="1억 원 이하",
        housing_type="아파트",
        page=7,
    )
    db = FakeStore([table])
    monkeypatch.setattr(law, "get_vector_store", lambda: db)
    result = law.search_law("보증금 1억원 이하 아파트 부채비율 75% 보증료율?")
    assert result["ok"] is True
    assert result["data"]["premium_rate_pct"] == 0.117
    assert result["data"]["deposit_won"] == 100_000_000
    assert result["data"]["evidence"][0]["page"] == 7
    assert db.similarity_calls == 0


def test_hug_rate_missing_conditions(monkeypatch):
    monkeypatch.setattr(law, "get_vector_store", lambda: FakeStore([]))
    result = law.search_law("HUG 보증료율은?")
    assert result["ok"] is True
    assert result["data"]["needs_more_information"] is True
    assert "보증금액" in result["data"]["required"]


def test_priority_law_uses_current_decree(monkeypatch):
    act10 = make_doc(
        "제10조 서울특별시: 5천500만원",
        source=law.DECREE_FILE,
        section_type="current_article",
        article="제10조(보증금 중 일정액의 범위 등)",
        page=3,
    )
    act11 = make_doc(
        "제11조 서울특별시: 1억6천500만원",
        source=law.DECREE_FILE,
        section_type="current_article",
        article="제11조(우선변제를 받을 임차인의 범위)",
        page=3,
    )
    old_guide = make_doc(
        "2018년 3천700만원",
        source=law.GUIDE_FILE,
        section_type="guide",
    )
    monkeypatch.setattr(law, "get_vector_store", lambda: FakeStore([old_guide, act10, act11]))
    result = law.search_law("서울특별시 소액임차인 최우선변제금액과 기준")
    assert result["ok"] is True
    assert len(result["data"]["evidence"]) == 2
    assert all(row["source"] == law.DECREE_FILE for row in result["data"]["evidence"])
    assert result["data"]["requires_collateral_date_check"] is True


def test_hug_signup_prefers_conditions_not_table(monkeypatch):
    table = make_doc("부채비율 70% 이하: 연 0.117%", source=law.HUG_OVERVIEW_FILE,
                     section_type="structured_premium_table", page=7)
    general = make_doc("보증한도는 주택가격과 선순위채권을 기준으로 계산", source=law.HUG_OVERVIEW_FILE,
                       section_type="product_info", page=2)
    monkeypatch.setattr(law, "get_vector_store", lambda: FakeStore([table, general]))
    result = law.search_law("HUG 가입 조건과 보증한도")
    assert result["ok"] is True
    assert result["data"]["evidence"][0]["page"] == 2
    assert len(result["data"]["evidence"]) == 1


def test_invalid_question(monkeypatch):
    result = law.search_law("  ")
    assert result["ok"] is False
    assert "error" in result


def test_store_errors_are_caught(monkeypatch):
    def broken():
        raise RuntimeError("가상 오류")
    monkeypatch.setattr(law, "get_vector_store", broken)
    result = law.search_law("계약갱신요구권이 무엇인가요?")
    assert result["ok"] is False
    assert "error" in result


def test_explicit_hug_range_is_not_misclassified():
    assert law._explicit_deposit_bucket("보증금 1억 초과~2억 이하") == "1억 원 초과~2억 원 이하"
    assert law._explicit_deposit_bucket("보증금 2억 원 초과~5억 원 이하") == "2억 원 초과~5억 원 이하"
    assert law._parse_deposit_won("보증금 1억 초과") is None
