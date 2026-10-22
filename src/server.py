"""API 서버: POST /query 로 질문을 받아 answer, contexts, trace 를 돌려준다."""

from fastapi import FastAPI
from pydantic import BaseModel

from agent import run_query

app = FastAPI(title="메리드립 핸드드립 도우미")


class QueryRequest(BaseModel):
    """질의 요청 본문. thread_id는 멀티턴 맛 교정을 이어 갈 때만 넣는다."""

    question: str
    thread_id: str | None = None


class QueryResponse(BaseModel):
    """질의 응답 본문."""

    answer: str
    contexts: list[dict]
    trace: list[dict]


@app.post("/query", response_model=QueryResponse)
def query(req: QueryRequest) -> dict:
    """질문을 에이전트에 넘기고 결과를 그대로 돌려준다."""
    return run_query(req.question, req.thread_id)
