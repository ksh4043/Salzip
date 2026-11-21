
"""RAG에 사용할 PDF 문서를 읽고 LangChain 문서로 변환한다."""

import logging
from pathlib import Path

import pdfplumber
import pymupdf as fitz
from langchain_core.documents import Document

logger = logging.getLogger(__name__)

# 프로젝트 및 PDF 폴더 경로
BASE_DIR = Path(__file__).resolve().parent.parent
DOCS_DIR = BASE_DIR / "data" / "docs"

# RAG에서 사용할 PDF 5개
PDF_FILES = [
    "housing_lease_act.pdf",
    "housing_lease_decree.pdf",
    "jeonse_fraud_guide.pdf",
    "hug_guarantee_overview.pdf",
    "hug_guarantee_terms.pdf",
]


def load_pdf(file_path: Path) -> list[Document]:
    """PDF를 읽고 페이지별 출처 정보를 포함한 문서로 변환한다."""
    documents: list[Document] = []

    # HUG 보증보험 약관은 좌우 2쪽으로 구성되어 있음
    # pdfplumber를 사용해 띄어쓰기와 문장 추출 정확도 개선
    if file_path.name == "hug_guarantee_terms.pdf":
        with pdfplumber.open(file_path) as pdf:
            for page_index, page in enumerate(pdf.pages):
                middle = page.width / 2

                regions = [
                    ("left", (0, 0, middle, page.height)),
                    ("right", (middle, 0, page.width, page.height)),
                ]

                for side, region in regions:
                    text = page.crop(region).extract_text() or ""
                    text = text.strip()

                    if not text:
                        continue

                    documents.append(
                        Document(
                            page_content=text,
                            metadata={
                                "source": file_path.name,
                                "page": page_index + 1,
                                "side": side,
                            },
                        )
                    )

        return documents

    # 나머지 PDF 4개는 PyMuPDF로 처리
    with fitz.open(file_path) as pdf:
        for page_index, page in enumerate(pdf):

            # HUG 상품개요의 마지막 설문조사 페이지 제외
            if (
                file_path.name == "hug_guarantee_overview.pdf"
                and page_index == len(pdf) - 1
            ):
                continue

            # PDF 텍스트 추출
            text = page.get_text(
                "text",
                sort=True,
            ).strip()

            # 표지 등 텍스트가 없는 페이지 제외
            if not text:
                continue

            documents.append(
                Document(
                    page_content=text,
                    metadata={
                        "source": file_path.name,
                        "page": page_index + 1,
                        "side": "full",
                    },
                )
            )

    return documents


def load_all_documents() -> list[Document]:
    """data/docs 폴더에서 PDF 5개를 모두 불러온다."""
    all_documents: list[Document] = []

    for filename in PDF_FILES:
        file_path = DOCS_DIR / filename

        # 파일 존재 여부 확인
        if not file_path.is_file():
            raise FileNotFoundError(
                f"PDF 파일을 찾을 수 없습니다: {file_path}"
            )

        # PDF 읽기
        documents = load_pdf(file_path)
        all_documents.extend(documents)

        logger.info(
            "%s: %d개 문서 추출",
            filename,
            len(documents),
        )

    return all_documents


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
    )

    result = load_all_documents()

    logger.info(
        "총 %d개 문서 추출 완료",
        len(result),
    )
