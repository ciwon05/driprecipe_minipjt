"""RAG 파이프라인: data/docs 마크다운을 색인하고 BM25 + 벡터 하이브리드로 검색한다."""

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from kiwipiepy import Kiwi
from langchain_aws import BedrockEmbeddings
from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_text_splitters import MarkdownHeaderTextSplitter
from rank_bm25 import BM25Okapi

load_dotenv()

DOCS_DIR = Path(__file__).resolve().parent.parent / "data" / "docs"
RRF_K = 60
# BM25 토큰으로 쓸 품사: 명사, 동사·형용사 어간, 어근, 외국어, 숫자
KEEP_TAGS = ("NN", "VV", "VA", "XR", "SL", "SN")

_kiwi = Kiwi()


def _tokenize(text: str) -> list[str]:
    """한국어 문장을 형태소 분석해 BM25 검색용 토큰 목록으로 바꾼다."""
    return [t.form for t in _kiwi.tokenize(text) if t.tag.startswith(KEEP_TAGS)]


def load_chunks() -> list[Document]:
    """data/docs의 마크다운 문서를 '##'·'###' 절 단위 조각으로 나눈다.

    조각 본문 앞에 문서 제목을 붙여, 절만 떼어 봐도 어느 문서인지 알 수 있게 한다.
    metadata의 doc_id는 파일 이름(확장자 제외)이다.
    """
    splitter = MarkdownHeaderTextSplitter(
        headers_to_split_on=[("#", "title"), ("##", "section"), ("###", "subsection")],
        strip_headers=False,
    )
    chunks = []
    for path in sorted(DOCS_DIR.glob("*.md")):
        for part in splitter.split_text(path.read_text(encoding="utf-8")):
            title = part.metadata.get("title", "")
            text = part.page_content
            if title and not text.startswith("# "):
                text = f"# {title}\n{text}"
            part.page_content = text
            part.metadata["doc_id"] = path.stem
            chunks.append(part)
    return chunks


class HybridRetriever:
    """BM25(키워드)와 Chroma(의미) 검색 결과를 RRF로 합치는 검색기."""

    def __init__(self, chunks: list[Document]):
        """조각 목록으로 BM25 색인과 Chroma 벡터 색인을 만든다."""
        self.chunks = chunks
        self.bm25 = BM25Okapi([_tokenize(c.page_content) for c in chunks])
        embeddings = BedrockEmbeddings(model_id=os.environ["BEDROCK_EMBED_MODEL_ID"])
        self.vectorstore = Chroma.from_documents(
            chunks, embeddings, ids=[str(i) for i in range(len(chunks))]
        )

    def search(self, query: str, k: int = 4) -> list[Document]:
        """질의와 관련 높은 조각 k개를 돌려준다.

        두 검색기에서 각각 상위 후보를 뽑아, 순위의 역수(1 / (RRF_K + 순위))를 더한 점수로 다시 정렬한다.
        """
        pool = max(k * 3, 10)
        bm25_scores = self.bm25.get_scores(_tokenize(query))
        bm25_rank = sorted(range(len(self.chunks)), key=lambda i: -bm25_scores[i])[:pool]
        vector_rank = [int(d.id) for d in self.vectorstore.similarity_search(query, k=pool)]

        fused: dict[int, float] = {}
        for ranking in (bm25_rank, vector_rank):
            for rank, idx in enumerate(ranking):
                fused[idx] = fused.get(idx, 0.0) + 1.0 / (RRF_K + rank + 1)
        best = sorted(fused, key=lambda i: -fused[i])[:k]
        return [self.chunks[i] for i in best]


@lru_cache(maxsize=1)
def get_retriever() -> HybridRetriever:
    """프로세스당 한 번만 색인을 만들고 같은 검색기를 재사용한다."""
    return HybridRetriever(load_chunks())


def retrieve(query: str, k: int = 4) -> list[dict]:
    """질의로 문서를 검색해 API contexts 형식([{doc_id, text}])으로 돌려준다."""
    return [
        {"doc_id": d.metadata["doc_id"], "text": d.page_content}
        for d in get_retriever().search(query, k)
    ]
