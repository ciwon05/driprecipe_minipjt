"""자체 평가: test_queries.csv 로 에이전트를 돌려 규칙 검사 · LLM-as-Judge · RAGAS 지표를 내고 라운드 리포트를 쓴다.

실행: python evaluation/run_eval.py --round 1 [--raw 원본결과.json] [--ids 1,5,8]
재채점: python evaluation/run_eval.py --round 1 --raw 원본결과.json --rescore --out 결과.md
  (에이전트를 다시 돌리지 않고, 저장된 답변으로 규칙 검사 · Judge · RAGAS만 다시 계산한다)
"""

import argparse
import asyncio
import csv
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402
from langchain_aws import BedrockEmbeddings, ChatBedrockConverse  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from agent import GENERAL_NOTE, GRIND_NOTE, OTHER_GRINDERS, run_query  # noqa: E402

load_dotenv(ROOT / ".env")

EVAL_DIR = ROOT / "evaluation"


class JudgeResult(BaseModel):
    """LLM-as-Judge 판정 결과."""

    traits_missing: list[str] = Field(description="답변에 없거나 지켜지지 않은 expected_traits 항목")
    forbidden_found: list[str] = Field(description="답변에 나타난 forbidden 항목")
    passed: bool = Field(description="빠진 trait도 없고 forbidden도 없으면 true")
    reason: str = Field(description="한두 문장 판정 이유")


JUDGE_PROMPT = """너는 핸드드립 도우미의 답변을 채점하는 평가자다.
질문, 답변에 반드시 있어야 할 항목(expected_traits), 나오면 안 되는 항목(forbidden)을 보고 판정하라.
- expected_traits의 각 항목이 답변에 실제로 담겨 있는지 하나씩 확인한다. 표현이 달라도 의미가 같으면 담긴 것으로 본다.
- forbidden의 각 항목이 답변에 나타나는지 확인한다. 위반이 답변 문장으로 명백할 때만 forbidden_found에 넣고, 해석에 따라 갈리는 경우는 위반으로 보지 않는다.
- 숫자는 답변에 적힌 값 그대로 판단하고, 맞는 값인지 직접 계산하지 않는다(숫자 정확성은 별도 규칙 검사가 한다).
- "분쇄도 비공식 시작값 고정 문구"는 "{grind_note}" 문장이 답변에 있으면 충족이다.
- "일반 레시피 문구"는 "{general_note}" 문장이다.
- 출처 표기는 답변 어디에든 "출처:" 줄이 있으면 출처가 있는 것으로 본다. 설명 내용이 실제 문서와 맞는지는 판단하지 않는다(근거 일치는 RAGAS faithfulness가 따로 잰다).

[질문]
{question}

[expected_traits]
{traits}

[forbidden]
{forbidden}

[답변]
{answer}
"""


def split_items(text: str) -> list[str]:
    """세미콜론으로 구분된 CSV 필드를 항목 목록으로 바꾼다."""
    return [t.strip() for t in (text or "").split(";") if t.strip()]


def load_cases(ids: set[str] | None) -> list[dict]:
    """test_queries.csv 와 ragas_references.csv 를 읽어 케이스 목록을 만든다."""
    with open(EVAL_DIR / "test_queries.csv", encoding="utf-8") as f:
        cases = list(csv.DictReader(f))
    with open(EVAL_DIR / "ragas_references.csv", encoding="utf-8") as f:
        refs = {r["id"]: r["reference"] for r in csv.DictReader(f)}
    for c in cases:
        c["reference"] = refs.get(c["id"])
    return [c for c in cases if not ids or c["id"] in ids]


def tool_outputs(trace: list[dict], name: str) -> list[dict]:
    """trace 에서 특정 도구의 JSON 출력들을 꺼낸다."""
    outs = []
    for t in trace:
        if t["step"] == name:
            try:
                outs.append(json.loads(t["output"]))
            except (json.JSONDecodeError, TypeError):
                pass
    return outs


def has_number(answer: str, value, unit: str = "") -> bool:
    """답변에 숫자 값이 (단위와 함께) 들어 있는지 확인한다."""
    num = str(int(value)) if float(value).is_integer() else str(value)
    return re.search(rf"(?<![\d.]){re.escape(num)}\s*{re.escape(unit)}", answer) is not None


def check_numbers(case: dict, answer: str, trace: list[dict]) -> tuple[bool, list[str]]:
    """답변의 숫자가 그 실행에서 도구가 돌려준 값과 일치하는지 검사한다.

    맛 교정 답변이면 suggest_adjustment 결과의 새 값만, 레시피 답변이면 마지막 calc_recipe 결과의
    원두량 · 물량 · 물 온도 · 분쇄도가 답변에 있어야 한다. 교정 답변에서 calc_recipe는 현재 레시피 확인용이라 검사하지 않는다.
    매버릭이 아닌 그라인더를 쓰는 질문이면 분쇄도 클릭은 검사하지 않는다.
    """
    problems = []
    other_grinder = OTHER_GRINDERS.search(case["input"]) is not None
    adjustments = [o for o in tool_outputs(trace, "suggest_adjustment") if o.get("result") == "교정"]
    for out in [] if adjustments else tool_outputs(trace, "calc_recipe")[-1:]:
        if "error" in out:
            continue
        checks = [(out["dose_g"], "g"), (out["water_g"], "g"), (out["water_temp_c"], "")]
        if not other_grinder:
            checks.append((out["grind_clicks"], ""))
        for value, unit in checks:
            if not has_number(answer, value, unit):
                problems.append(f"calc_recipe 값 {value}{unit} 누락")
    for out in adjustments:
        if not has_number(answer, out["after"]):
            problems.append(f"suggest_adjustment 새 값 {out['after']} 누락")
    return not problems, problems


def check_tools(case: dict, trace: list[dict]) -> tuple[bool, list[str]]:
    """expected_tools 의 도구가 모두 호출됐는지 검사한다."""
    called = {t["step"] for t in trace}
    missing = [t for t in split_items(case["expected_tools"]) if t not in called]
    return not missing, missing


def judge(case: dict, answer: str) -> JudgeResult:
    """LLM-as-Judge 로 expected_traits · forbidden 충족 여부를 판정한다."""
    llm = ChatBedrockConverse(model=os.environ["BEDROCK_MODEL_ID"], temperature=0, max_tokens=1024)
    prompt = JUDGE_PROMPT.format(
        grind_note=GRIND_NOTE,
        general_note=GENERAL_NOTE,
        question=case["input"],
        traits="\n".join(f"- {t}" for t in split_items(case["expected_traits"])),
        forbidden="\n".join(f"- {t}" for t in split_items(case["forbidden"])) or "- (없음)",
        answer=answer,
    )
    return llm.with_structured_output(JudgeResult).invoke(prompt)


def run_cases(cases: list[dict], raw_path: Path | None, rescore: bool = False) -> list[dict]:
    """케이스마다 에이전트를 실행하고 규칙 검사와 Judge 판정을 붙인다.

    raw_path 가 있으면 이미 끝난 케이스는 건너뛴다. rescore 면 저장된 답변을 그대로 쓰고
    규칙 검사와 Judge 판정만 다시 하며, 원본 파일은 덮어쓰지 않는다.
    """
    done = {}
    if raw_path and raw_path.exists():
        done = {r["id"]: r for r in json.loads(raw_path.read_text(encoding="utf-8"))}

    results = []
    for case in cases:
        prev = done.get(case["id"])
        if prev and "error" not in prev and not rescore:
            results.append(prev)
            continue
        print(f"[{case['id']}] {case['input'][:40]}", flush=True)
        row = {"id": case["id"], "category": case["category"], "input": case["input"], "reference": case["reference"]}
        try:
            if rescore and prev and "answer" in prev:
                res = {k: prev[k] for k in ("answer", "contexts", "trace")}
                row.update(res, latency_s=prev.get("latency_s"))
            else:
                start = time.time()
                res = run_query(case["input"])
                row.update(res, latency_s=round(time.time() - start, 1))
            row["tools_ok"], row["tools_missing"] = check_tools(case, res["trace"])
            row["numbers_ok"], row["number_problems"] = check_numbers(case, res["answer"], res["trace"])
            j = judge(case, res["answer"])
            row["judge"] = j.model_dump()
            row["passed"] = row["tools_ok"] and row["numbers_ok"] and j.passed
        except Exception as e:  # 한 케이스 실패가 전체 평가를 멈추지 않게 한다
            row["error"] = f"{type(e).__name__}: {str(e)[:300]}"
            row["passed"] = False
        results.append(row)
        if raw_path and not rescore:
            merged = {**done, **{r["id"]: r for r in results}}
            raw_path.write_text(json.dumps(list(merged.values()), ensure_ascii=False, indent=1), encoding="utf-8")
    return results


def run_ragas(results: list[dict]) -> dict:
    """정답(reference)이 있는 케이스로 RAGAS 4개 지표를 계산해 평균과 케이스별 점수를 돌려준다."""
    from ragas import EvaluationDataset, evaluate
    from ragas.embeddings import LangchainEmbeddingsWrapper
    from ragas.llms import LangchainLLMWrapper
    from ragas.metrics import AnswerRelevancy, Faithfulness, LLMContextPrecisionWithReference, LLMContextRecall
    from ragas.run_config import RunConfig

    rows = [r for r in results if r.get("reference") and r.get("answer") and r.get("contexts")]
    if not rows:
        return {}
    dataset = EvaluationDataset.from_list([
        {
            "user_input": r["input"],
            "response": r["answer"],
            "retrieved_contexts": [c["text"] for c in r["contexts"]],
            "reference": r["reference"],
        }
        for r in rows
    ])
    llm = LangchainLLMWrapper(ChatBedrockConverse(model=os.environ["BEDROCK_MODEL_ID"], temperature=0, max_tokens=8192))
    emb = LangchainEmbeddingsWrapper(BedrockEmbeddings(model_id=os.environ["BEDROCK_EMBED_MODEL_ID"]))
    # 답변이 한국어라 질문 생성 프롬프트를 한국어로 맞춰야 원래 질문과 같은 언어로 비교된다
    relevancy = AnswerRelevancy(strictness=1)
    relevancy.set_prompts(**asyncio.run(relevancy.adapt_prompts(language="korean", llm=llm)))
    metrics = [LLMContextRecall(), LLMContextPrecisionWithReference(), Faithfulness(), relevancy]
    scores = evaluate(dataset, metrics=metrics, llm=llm, embeddings=emb, run_config=RunConfig(max_workers=2, timeout=300))
    df = scores.to_pandas()
    names = ["context_recall", "llm_context_precision_with_reference", "faithfulness", "answer_relevancy"]
    per_case = {r["id"]: {n: df.iloc[i].get(n) for n in names} for i, r in enumerate(rows)}
    means = {n: float(df[n].mean(skipna=True)) for n in names if n in df}
    return {"means": means, "per_case": per_case, "n": len(rows)}


def fmt(v) -> str:
    """점수를 소수 둘째 자리 문자열로 바꾼다. 값이 없으면 '-'."""
    try:
        return "-" if v is None or v != v else f"{float(v):.2f}"
    except (TypeError, ValueError):
        return "-"


def write_report(round_no: int, results: list[dict], ragas: dict, out: Path | None = None) -> Path:
    """라운드 리포트 마크다운을 out(기본값 evaluation/round{N}_report.md)에 쓴다."""
    total = len(results)
    passed = sum(r["passed"] for r in results)
    by_cat = {}
    for r in results:
        c = by_cat.setdefault(r["category"], [0, 0])
        c[0] += r["passed"]
        c[1] += 1

    lines = [
        f"# {round_no}차 자체 평가 리포트",
        "",
        f"- 실행 시각: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"- 에이전트 모델: `{os.environ['BEDROCK_MODEL_ID']}` · Judge/RAGAS 모델: 같음 · 임베딩: `{os.environ['BEDROCK_EMBED_MODEL_ID']}`",
        "- 판정: 케이스는 ① expected_tools 호출 ② 답변 숫자 = 도구 출력 ③ LLM-as-Judge(expected_traits · forbidden) 세 가지를 모두 통과해야 pass",
        "",
        "## 요약",
        "",
        f"- **인-아웃 세트 통과: {passed} / {total}** (완성 기준 17 / 20)",
        "- 카테고리별: " + " · ".join(f"{k} {v[0]}/{v[1]}" for k, v in by_cat.items()),
    ]
    if ragas:
        m = ragas["means"]
        lines += [
            "",
            f"## RAGAS 지표 (정답이 있는 {ragas['n']}건 평균)",
            "",
            f"- context_recall: {fmt(m.get('context_recall'))}",
            f"- context_precision: {fmt(m.get('llm_context_precision_with_reference'))}",
            f"- faithfulness: {fmt(m.get('faithfulness'))} (완성 기준 0.80)",
            f"- answer_relevancy: {fmt(m.get('answer_relevancy'))}",
        ]
    lines += [
        "",
        "## 케이스별 결과",
        "",
        "| id | 분류 | 도구 | 숫자 | Judge | 결과 | 지연(초) | faithfulness | 메모 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        j = r.get("judge", {})
        notes = []
        if r.get("error"):
            notes.append(r["error"])
        if r.get("tools_missing"):
            notes.append("도구 누락: " + ", ".join(r["tools_missing"]))
        if r.get("number_problems"):
            notes.append("; ".join(r["number_problems"]))
        if j.get("traits_missing"):
            notes.append("빠진 항목: " + ", ".join(j["traits_missing"]))
        if j.get("forbidden_found"):
            notes.append("금지 항목: " + ", ".join(j["forbidden_found"]))
        faith = ragas.get("per_case", {}).get(r["id"], {}).get("faithfulness") if ragas else None
        ok = lambda v: "-" if v is None else ("✅" if v else "❌")  # noqa: E731
        lines.append(
            f"| {r['id']} | {r['category']} | {ok(r.get('tools_ok'))} | {ok(r.get('numbers_ok'))} | "
            f"{ok(j.get('passed'))} | {'✅' if r['passed'] else '❌'} | {r.get('latency_s', '-')} | {fmt(faith)} | "
            f"{' / '.join(notes).replace('|', '/') or '-'} |"
        )
    path = out or EVAL_DIR / f"round{round_no}_report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main():
    """명령행 인자를 읽어 평가를 실행하고 리포트를 쓴다."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--round", type=int, required=True)
    parser.add_argument("--raw", type=Path, help="케이스별 원본 결과 JSON 경로(중간 저장 · 이어 하기용)")
    parser.add_argument("--ids", help="일부 케이스만 실행할 때 쉼표로 구분한 id")
    parser.add_argument("--skip-ragas", action="store_true")
    parser.add_argument("--rescore", action="store_true", help="저장된 답변으로 채점만 다시 한다(--raw 필요)")
    parser.add_argument("--out", type=Path, help="리포트 경로(기본값 evaluation/round{N}_report.md)")
    args = parser.parse_args()
    if args.rescore and not (args.raw and args.raw.exists()):
        parser.error("--rescore 에는 기존 --raw 파일이 필요합니다")

    cases = load_cases(set(args.ids.split(",")) if args.ids else None)
    results = run_cases(cases, args.raw, args.rescore)
    ragas = {} if args.skip_ragas else run_ragas(results)
    path = write_report(args.round, results, ragas, args.out)
    print(f"통과 {sum(r['passed'] for r in results)} / {len(results)} · 리포트: {path}")


if __name__ == "__main__":
    main()
