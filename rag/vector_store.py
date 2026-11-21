
"""OpenAI 임베딩으로 RAG 문서를 ChromaDB에 저장한다."""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_openai import OpenAIEmbeddings

from rag.chunking import build_chunks

logger = logging.getLogger(__name__)

# 프로젝트 설정
BASE_DIR = Path(__file__).resolve().parent.parent
CHROMA_DIR = BASE_DIR / "data" / "chroma"

COLLECTION_NAME = "salzip_law_v1"
EMBEDDING_MODEL = "text-embedding-3-small"
BATCH_SIZE = 50


def get_vector_store() -> Chroma:
    """OpenAI 임베딩을 사용하는 로컬 ChromaDB에 연결한다."""
    load_dotenv(BASE_DIR / ".env")

    if not os.getenv("OPENAI_API_KEY"):
        raise ValueError(
            "OPENAI_API_KEY가 설정되지 않았습니다. .env를 확인하세요."
        )

    embeddings = OpenAIEmbeddings(
        model=EMBEDDING_MODEL,
    )

    return Chroma(
        collection_name=COLLECTION_NAME,
        embedding_function=embeddings,
        persist_directory=str(CHROMA_DIR),
    )


def make_document_id(document: Document) -> str:
    """문서 출처와 내용을 기준으로 고유 ID를 생성한다."""
    metadata = document.metadata

    unique_content = "|".join(
        [
            str(metadata.get("source", "")),
            str(metadata.get("page", "")),
            str(metadata.get("page_end", "")),
            str(metadata.get("section_type", "")),
            str(metadata.get("article", "")),
            document.page_content,
        ]
    )

    return hashlib.sha256(
        unique_content.encode("utf-8")
    ).hexdigest()


def build_vector_store() -> None:
    """검색용 청크를 임베딩하여 ChromaDB에 저장한다."""
    logger.info("PDF 문서 분할 시작")

    documents = build_chunks()

    # 내용이 같은 문서는 동일한 ID 사용
    documents_by_id = {
        make_document_id(doc): doc
        for doc in documents
    }

    logger.info(
        "임베딩 대상: %d개 청크",
        len(documents_by_id),
    )

    store = get_vector_store()

    # 기존 저장 내역 확인: 중복 임베딩 방지
    existing_ids = set(
        store.get(include=[])["ids"]
    )

    new_items = [
        (doc_id, document)
        for doc_id, document in documents_by_id.items()
        if doc_id not in existing_ids
    ]

    logger.info(
        "새로 임베딩할 청크: %d개",
        len(new_items),
    )

    # API 호출량과 중복 비용을 줄이도록 나누어 저장
    for start in range(0, len(new_items), BATCH_SIZE):
        batch = new_items[start:start + BATCH_SIZE]

        store.add_documents(
            documents=[doc for _, doc in batch],
            ids=[doc_id for doc_id, _ in batch],
        )

        logger.info(
            "임베딩 및 저장 완료: %d/%d",
            min(start + BATCH_SIZE, len(new_items)),
            len(new_items),
        )

    # 문서 수정으로 더 이상 사용하지 않는 청크 제거
    obsolete_ids = existing_ids - set(documents_by_id)

    if obsolete_ids:
        store.delete(ids=list(obsolete_ids))
        logger.info(
            "오래된 청크 %d개 삭제",
            len(obsolete_ids),
        )

    total = len(store.get(include=[])["ids"])

    logger.info(
        "ChromaDB 구축 완료: 총 %d개 청크 저장",
        total,
    )
    logger.info("DB 위치: %s", CHROMA_DIR)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
    )

    build_vector_store()
