"""Salzip RAG 실제 검색 30문항 품질 검사.

실행: python -m tests.rag_accuracy_check
출력: test_results/rag_accuracy_report.txt, test_results/rag_accuracy_report.json

주의:
- 기존 ChromaDB를 조회하고, 일부 일반 문장 검색에서 질문 임베딩 API를 호출합니다.
- LLM의 최종 답변 품질이나 실제 계약의 법률 적용까지 검증하지 않습니다.
- 정답 기준은 프로젝트에 저장된 2026년 법령 및 2026-11-16 HUG 상품개요입니다.
"""

from __future__ import annotations

import json
import re
import sys
import traceback
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from tools.law import search_law

PROJECT_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_DIR / "test_results"
ACT = "housing_lease_act.pdf"
DECREE = "housing_lease_decree.pdf"
GUIDE = "jeonse_fraud_guide.pdf"
HUG = "hug_guarantee_overview.pdf"
TERMS = "hug_guarantee_terms.pdf"


def citation_case(case_id: str, group: str, question: str, source: str,
                  *, article: str | None = None, keywords: tuple[str, ...] = (),
                  pages: tuple[int, ...] = ()) -> dict[str, Any]:
    """같은 근거 문서에 기대 조항과 핵심 문구가 함께 있는지 검사한다."""
    return {
        "id": case_id, "group": group, "question": question,
        "kind": "citation", "expected": {
            "source": source, "article_prefix": article,
            "text_any": list(keywords), "allowed_pages": list(pages),
        },
    }


def rate_case(case_id: str, question: str, *, rate: str, bucket: str,
              housing: str, ratio: str) -> dict[str, Any]:
    """구조화된 HUG 요율의 선택 결과를 비교한다."""
    return {
        "id": case_id, "group": "HUG 요율", "question": question,
        "kind": "rate", "expected": {
            "premium_rate_pct": rate,
            "deposit_range": bucket,
            "housing_type": housing,
            "debt_ratio_range": ratio,
            "source": HUG,
        },
    }


CASES: list[dict[str, Any]] = [
    # 시행령과 주택임대차보호법: 최신 조문에서 근거 검색
    citation_case("L01", "법령", "서울특별시 소액임차인 최우선변제금액은 얼마인가요?", DECREE,
                  article="제10조", keywords=("5천500만원",)),
    citation_case("L02", "법령", "서울특별시 최우선변제 대상 소액임차인의 보증금 기준은?", DECREE,
                  article="제11조", keywords=("1억6천500만원",)),
    citation_case("L03", "법령", "서울 제외 수도권 과밀억제권역 최우선변제액은?", DECREE,
                  article="제10조", keywords=("4천800만원",)),
    citation_case("L04", "법령", "광역시의 소액임차인 보증금 기준은?", DECREE,
                  article="제11조", keywords=("8천500만원",)),
    citation_case("L05", "법령", "그 밖의 지역 최우선변제 금액은?", DECREE,
                  article="제10조", keywords=("2천500만원",)),
    citation_case("L06", "법령", "주택임대차보호법에서 전입신고 후 대항력은 언제 발생하나요?", ACT,
                  article="제3조(", keywords=("그 다음 날부터",)),
    citation_case("L07", "법령", "주택임대차보호법 계약갱신요구권은 몇 번 행사할 수 있나요?", ACT,
                  article="제6조의3", keywords=("1회에 한하여",)),
    citation_case("L08", "법령", "주택임대차보호법 차임과 보증금 증액청구 상한은 얼마인가요?", ACT,
                  article="제7조(", keywords=("20분의 1",)),
    # 2026-11-16 HUG 상품개요 7~8쪽의 24개 요율 가운데 12가지 대표/경계 사례
    rate_case("R01", "보증금 1억원 이하 아파트 부채비율 70% HUG 보증료율은?",
              rate="0.097", bucket="1억 원 이하", housing="아파트", ratio="70% 이하"),
    rate_case("R02", "보증금 1억원 이하 아파트 부채비율 75% HUG 보증료율은?",
              rate="0.117", bucket="1억 원 이하", housing="아파트", ratio="70% 초과~80% 이하"),
    rate_case("R03", "보증금 1억원 이하 아파트 부채비율 81% HUG 보증료율은?",
              rate="0.137", bucket="1억 원 이하", housing="아파트", ratio="80% 초과"),
    rate_case("R04", "보증금 1억원 이하 빌라 부채비율 70% HUG 보증료율은?",
              rate="0.111", bucket="1억 원 이하", housing="기타", ratio="70% 이하"),
    rate_case("R05", "보증금 1억원 이하 기타 주택 부채비율 80% HUG 보증료율은?",
              rate="0.142", bucket="1억 원 이하", housing="기타", ratio="70% 초과~80% 이하"),
    rate_case("R06", "보증금 2억원 아파트 부채비율 75% HUG 보증료율은?",
              rate="0.124", bucket="1억 원 초과~2억 원 이하", housing="아파트", ratio="70% 초과~80% 이하"),
    rate_case("R07", "보증금 2억원 기타 주택 부채비율 75% HUG 보증료율은?",
              rate="0.151", bucket="1억 원 초과~2억 원 이하", housing="기타", ratio="70% 초과~80% 이하"),
    rate_case("R08", "보증금 3억원 아파트 부채비율 81% HUG 보증료율은?",
              rate="0.154", bucket="2억 원 초과~5억 원 이하", housing="아파트", ratio="80% 초과"),
    rate_case("R09", "보증금 3억원 다세대주택 부채비율 81% HUG 보증료율은?",
              rate="0.197", bucket="2억 원 초과~5억 원 이하", housing="기타", ratio="80% 초과"),
    rate_case("R10", "보증금 6억원 아파트 부채비율 70% HUG 보증료율은?",
              rate="0.113", bucket="5억 원 초과", housing="아파트", ratio="70% 이하"),
    rate_case("R11", "보증금 6억원 기타 주택 부채비율 81% HUG 보증료율은?",
              rate="0.211", bucket="5억 원 초과", housing="기타", ratio="80% 초과"),
    rate_case("R12", "보증금 1억 5천만원 아파트 부채비율 75% HUG 보증료율은?",
              rate="0.124", bucket="1억 원 초과~2억 원 이하", housing="아파트", ratio="70% 초과~80% 이하"),
    # 상품개요: 단순 요율표보다 정확한 신청/한도 근거를 찾는지
    citation_case("H01", "HUG 가입·한도", "HUG 전세보증금반환보증 가입 조건과 전입신고 요건은?", HUG,
                  keywords=("전입신고", "확정일자", "보증조건"), pages=(2, 3)),
    citation_case("H02", "HUG 가입·한도", "HUG 전세보증금반환보증 보증한도 계산식에서 선순위채권은?", HUG,
                  keywords=("선순위채권", "주택가격"), pages=(2, 3)),
    citation_case("H03", "HUG 가입·한도", "HUG 전세보증금반환보증 신청기한은 전세계약 기간의 언제까지인가요?", HUG,
                  keywords=("1/2", "2분의 1"), pages=(1, 2, 4)),
    # HUG 약관: 원문 조항 검색 여부
    citation_case("T01", "HUG 약관", "HUG 약관 제6조 보증사고 발생 기준은?", TERMS,
                  article="제6조", keywords=("보증사고",)),
    citation_case("T02", "HUG 약관", "HUG 약관 제3조 면책사항과 대항력 상실은?", TERMS,
                  article="제3조", keywords=("대항력", "면책")),
    citation_case("T03", "HUG 약관", "HUG 약관 제7조 이행청구 절차는?", TERMS,
                  article="제7조", keywords=("보증채무", "청구")),
    citation_case("T04", "HUG 약관", "HUG 약관 제8조 이행청구 제출서류는 무엇인가요?", TERMS,
                  article="제8조", keywords=("이행청구서", "신분증")),
    # 피해예방 안내서: 일반 안내 문서 검색
    citation_case("G01", "전세사기 예방", "전세사기 예방 체크리스트 등기사항증명서를 확인해야 하나요?", GUIDE,
                  keywords=("등기사항증명서", "등기부")),
    citation_case("G02", "전세사기 예방", "전세사기 신탁사기 수법과 주의사항은?", GUIDE,
                  keywords=("신탁",)),
    # 조건 부족 시 거짓 요율을 답하지 않고 추가 정보를 요청하는지
    {"id": "I01", "group": "정보 부족", "question": "HUG 보증료율이 얼마예요?",
     "kind": "missing_info", "expected": {"needs_more_information": True,
                                              "required": ["보증금액", "주택유형", "부채비율"]}},
]

assert len(CASES) == 30, f"테스트 건수 확인 필요: {len(CASES)}"


def normalize(value: object) -> str:
    """원문 PDF 추출 때 생기는 공백 차이를 제거하고 비교한다."""
    return re.sub(r"\s+", "", str(value)).casefold()


def check_result(case: dict[str, Any], result: dict[str, Any]) -> tuple[bool, str]:
    """근거의 출처·조항·수치까지 함께 검사하며, 요약 문자열만으로는 합격시키지 않는다."""
    if not isinstance(result, dict):
        return False, "반환값이 dict가 아님"
    if result.get("ok") is not True:
        return False, f"검색 실패: {result.get('error', result)}"

    data = result.get("data") or {}
    expected = case["expected"]

    if case["kind"] == "missing_info":
        if data.get("needs_more_information") is not True:
            return False, "필수 정보가 부족한데 추가 정보 요청으로 처리하지 않음"
        required = set(data.get("required") or [])
        missing = sorted(set(expected["required"]) - required)
        if missing:
            return False, "누락된 필수 항목: " + ", ".join(missing)
        return True, "정보 부족을 감지하고 필요한 3가지 조건을 요청함"

    evidence = data.get("evidence") or []
    if not evidence:
        return False, "출처를 포함한 근거(evidence)가 없음"

    if case["kind"] == "rate":
        if data.get("needs_more_information") is True:
            return False, "보증금·주택유형·비율을 제공했는데 추가 정보가 필요하다고 응답함"
        try:
            actual_rate = Decimal(str(data.get("premium_rate_pct")))
        except (ValueError, TypeError, ArithmeticError):
            return False, f"보증료율이 숫자가 아님: {data.get('premium_rate_pct')!r}"
        correct_rate = Decimal(expected["premium_rate_pct"])
        if actual_rate != correct_rate:
            return False, f"보증료율 불일치: 실제 {actual_rate}% / 기대 {correct_rate}%"
        for key in ("deposit_range", "housing_type", "debt_ratio_range"):
            if normalize(data.get(key, "")) != normalize(expected[key]):
                return False, f"{key} 불일치: 실제 {data.get(key)!r} / 기대 {expected[key]!r}"
        if not any(row.get("source") == expected["source"]
                   and row.get("section_type") == "structured_premium_table"
                   for row in evidence):
            return False, "정확한 구조화 HUG 표 출처가 없음"
        return True, f"연 {actual_rate}% 및 구간·주택유형·부채비율·출처 확인"

    source_rows = [row for row in evidence if row.get("source") == expected["source"]]
    if not source_rows:
        actual_sources = sorted({str(row.get('source')) for row in evidence})
        return False, f"잘못된 출처: 기대 {expected['source']}, 실제 {actual_sources}"

    article = expected.get("article_prefix")
    if article:
        source_rows = [row for row in source_rows
                       if str(row.get("article", "")).startswith(article)]
        if not source_rows:
            return False, f"원하는 조항({article})이 검색 결과에 없음"

    pages = expected.get("allowed_pages") or []
    if pages:
        source_rows = [row for row in source_rows if row.get("page") in pages]
        if not source_rows:
            return False, f"예상 페이지({pages})의 근거가 없음"

    any_words = expected.get("text_any") or []
    if any_words and not any(
        any(normalize(word) in normalize(row.get("text", "")) for word in any_words)
        for row in source_rows
    ):
        return False, f"근거 본문에서 핵심 문구를 찾지 못함: {any_words}"
    return True, f"원문 출처·조항/페이지·핵심 문구 확인 ({len(source_rows)}개 후보)"


def short_evidence(result: dict[str, Any]) -> list[str]:
    if not isinstance(result, dict):
        return []
    rows = (result.get("data") or {}).get("evidence") or []
    return [
        f"{row.get('source','?')} p.{row.get('page','?')} "
        f"{row.get('article','')} | {str(row.get('text',''))[:130].replace(chr(10), ' ')}"
        for row in rows[:3]
    ]


def run() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    outcome: list[dict[str, Any]] = []
    print(f"\nSalzip RAG 실제 검색 30문항 검사 시작 (기존 ChromaDB 사용)\n")
    for case in CASES:
        try:
            response = search_law(case["question"])
            passed, note = check_result(case, response)
            status = "PASS" if passed else "FAIL"
        except Exception as exc:  # 여러 테스트 중 하나가 오류 나도 나머지를 계속 수행
            response = {"ok": False, "error": str(exc), "traceback": traceback.format_exc()}
            status, note = "ERROR", f"검사 도중 예외: {type(exc).__name__}: {exc}"

        item = {
            **case,
            "status": status,
            "diagnosis": note,
            "top_evidence": short_evidence(response),
            "response": response,
        }
        outcome.append(item)
        print(f"[{status:5}] {case['id']:3} | {case['group']:10} | {case['question']}")

    counts = Counter(row["status"] for row in outcome)
    category_counts: dict[str, Counter] = {}
    for row in outcome:
        category_counts.setdefault(row["group"], Counter())[row["status"]] += 1

    summary = {
        "executed_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "total": len(outcome),
        "passed": counts["PASS"],
        "failed": counts["FAIL"],
        "errors": counts["ERROR"],
        "pass_rate_pct": round(100 * counts["PASS"] / len(outcome), 1),
        "important_scope": (
            "원문 검색 근거·출처 및 구조화 요율 자동 일치율입니다. "
            "LLM 최종 답변의 정확도, 계약별 소급 적용, 최신 법규 유효성은 별도 검증해야 합니다."
        ),
    }
    report = {"summary": summary, "groups": {name: dict(n) for name, n in category_counts.items()}, "cases": outcome}
    json_path = OUTPUT_DIR / "rag_accuracy_report.json"
    text_path = OUTPUT_DIR / "rag_accuracy_report.txt"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    lines = [
        "Salzip RAG 30문항 실제 검색 검사 결과", "=" * 60,
        f"검사 시각: {summary['executed_at']}",
        f"전체 {summary['total']} / 통과 {summary['passed']} / 실패 {summary['failed']} / 오류 {summary['errors']}",
        f"근거 일치율: {summary['pass_rate_pct']}%", "",
        "검사 범위: 기존 PDF에서 검색된 근거의 문서명·조항·핵심 문구, HUG 요율의 수치·구간 일치",
        "주의: LLM 최종 답변 품질, 실제 계약 시점별 법규 적용, 최신성의 보증은 아님.", "",
        "[그룹별 결과]",
    ]
    for name, stat in category_counts.items():
        lines.append(f"- {name}: {stat['PASS']} PASS, {stat['FAIL']} FAIL, {stat['ERROR']} ERROR")
    lines.extend(["", "[문항별 결과]", ""])
    for row in outcome:
        lines.extend([
            f"[{row['status']}] {row['id']} ({row['group']})",
            f"질문: {row['question']}",
            f"기대: {json.dumps(row['expected'], ensure_ascii=False)}",
            f"판정: {row['diagnosis']}",
            f"요약: {str((row['response'] or {}).get('summary', (row['response'] or {}).get('error','')))}",
        ])
        for evidence_line in row["top_evidence"]:
            lines.append(f"근거: {evidence_line}")
        lines.append("")
    text_path.write_text("\n".join(lines), encoding="utf-8")

    print("\n" + "=" * 60)
    print(f"완료: {summary['passed']}/{summary['total']} 통과 ({summary['pass_rate_pct']}%)")
    print(f"실패 {summary['failed']}개 / 실행 오류 {summary['errors']}개")
    print(f"TXT 보고서: {text_path}")
    print(f"JSON 상세 결과: {json_path}")
    print("실패 항목은 개선 대상이며, 보고서의 수치가 서비스의 실제 정답률을 뜻하지는 않습니다.")
    # 실패가 있어도 보고서를 읽을 수 있도록 스크립트 자체는 정상 종료한다.
    return 0


if __name__ == "__main__":
    sys.exit(run())
