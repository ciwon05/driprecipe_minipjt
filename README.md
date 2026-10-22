# 미니 PJT: 메리드립 핸드드립 도우미 — 내 원두 맞춤 드립 레시피 · 맛 교정 Agent

## 무엇을 푸나
원두를 산 홈브루어가 "이 원두를 어떻게 내려야 하고, 내려 본 맛이 이상하면 무엇을 바꿔야 하는지" 몰라 원두 맛을 제대로 못 끌어내는 문제를, 로스터리 원두 스펙에 맞춘 레시피와 한 번에 변수 하나씩 바꾸는 맛 교정(최대 3회, 이후 원두 추천)으로 푼다.

- 로스터리는 가상의 "메리드립 로스터스"이고 원두 카탈로그는 8종이다.
- 그라인더는 홀츠클로츠 매버릭 2.0, 드리퍼는 하리오 V60 · 칼리타 웨이브 · 케멕스를 지원한다.
- 서비스 스펙은 [SERVICE.md](SERVICE.md), 도메인 용어는 [GLOSSARY.md](GLOSSARY.md), 주요 설계 결정은 [docs/adr/](docs/adr/)에 있다.

## 활용한 패턴 (Day 1~7)
- **Day 1: Pydantic 구조화 출력** — 맛 증상 분류(`classify_taste`)와 LLM-as-Judge 판정(`JudgeResult`)을 Pydantic 스키마로 받는다.
- **Day 2: 하이브리드 RAG** — BM25(kiwipiepy 형태소) + Chroma 벡터 검색을 RRF(k=60)로 합친다. 마크다운 제목(#, ##, ###) 기준으로 조각을 나누고, 조각마다 문서 제목을 붙여 출처 표기에 쓴다.
- **Day 3: ReAct** — `langchain.agents.create_agent`로 도메인 도구 6개를 결합하고, 모델이 질문에 따라 도구를 골라 부른다.
- **Day 4: 도구 다중 결합** — 한 질문에서 원두 조회 → 레시피 계산 → 문서 검색(교정은 맛 분류 → 교정 계산 → 문서 검색)을 이어서 부른다. 숫자는 모두 결정적 도구가 계산하고 모델은 계산하지 않는다.
- **Day 5: 가드레일 + 미들웨어** — `before_agent` 입력 가드레일이 프롬프트 인젝션과 건강 · 카페인 질문을 모델 호출 없이 정해진 답으로 끊는다. `after_agent` 출력 가드레일은 고정 안내 문구를 보장하고, 매버릭이 아닌 그라인더에 대한 클릭 수를 지운다.
- **Day 7: 단기 메모리** — LangGraph `InMemorySaver` 체크포인터와 선택 필드 `thread_id`로 교정 회차와 직전 레시피를 이어 간다([ADR-0003](docs/adr/0003-optional-thread-id-for-correction-rounds.md)).
- **Day 7: Trace** — API 응답의 `trace`에 입력, 도구 호출(입력 · 출력), 가드레일, 최종 답변을 순서대로 담는다. LangSmith에도 연동해 실행마다 에이전트 · 모델 호출 · 도구 호출 트레이스를 `driprecipe-minipjt` 프로젝트에 기록한다(`.env`의 `LANGSMITH_*` 설정, 코드 변경 없음).
- **Day 7: 평가** — 인-아웃 세트 20건을 세 가지로 채점한다. ① 기대 도구를 호출했는지 ② 답변 숫자가 도구 출력과 같은지 ③ LLM-as-Judge. 여기에 RAGAS 지표 4종을 더한다.

## 아키텍처

```
POST /query {question, thread_id?}            src/server.py
        │
        ▼
 input_guardrail (before_agent) ── 인젝션 · 건강 질문 → 정해진 답으로 종료
        │
        ▼
 ReAct 에이전트 (create_agent + ChatBedrockConverse, Claude Sonnet 4.5)
        │  도구 선택
        ├─ search_bean        원두 카탈로그 조회 (data/beans.json)
        ├─ calc_recipe        권장 / 일반 레시피 계산, 드리퍼 보정, 푸어 스케줄
        ├─ classify_taste     맛 표현 → 맛 증상 5종 (Claude Haiku 4.5, Pydantic)
        ├─ suggest_adjustment 변수 하나만 교정, 허용 범위 · 반대 증상 · 3회 한도 규칙
        ├─ recommend_bean     3회 교정 뒤 반대 맛 프로필 원두 추천
        └─ retrieve_docs      하이브리드 RAG (BM25 + Chroma, RRF) — data/docs/ 16개 문서
        │
        ▼
 output_guardrail (after_agent) ── 고정 문구 보장, 다른 그라인더면 클릭 수 제거
        │
        ▼
 {answer, contexts, trace}     contexts = 검색 문서 + 숫자 근거가 된 도구 출력
```

- 대화 상태는 `InMemorySaver`에 `thread_id`별로 저장한다. `thread_id`가 없으면 질문마다 새 대화가 된다.
- 임베딩은 `amazon.titan-embed-text-v2:0`을 쓴다.

## 실행 방법

```bash
# 1) 가상환경과 의존성 (Python 3.14에서 확인)
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt   # ragas 설치가 실패하면 requirements.txt의 주석대로 --no-deps로 설치

# 2) .env (코드에 비밀 값을 적지 않는다)
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_DEFAULT_REGION=us-east-1
BEDROCK_MODEL_ID=global.anthropic.claude-sonnet-4-5-20250929-v1:0
BEDROCK_FAST_MODEL_ID=us.anthropic.claude-haiku-4-5-20251001-v1:0
BEDROCK_EMBED_MODEL_ID=amazon.titan-embed-text-v2:0
# LangSmith 트레이스
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=...
LANGSMITH_PROJECT=driprecipe-minipjt

# 3) API 서버
uvicorn server:app --app-dir src --port 8000

# 4) 호출 예시
curl -X POST localhost:8000/query -H "Content-Type: application/json" \
  -d '{"question": "에티오피아 예가체프 워시드를 V60으로 내리려고 해요. 레시피 알려주세요."}'

# 맛 교정을 이어 갈 때는 같은 thread_id를 보낸다
curl -X POST localhost:8000/query -H "Content-Type: application/json" \
  -d '{"question": "내려 봤더니 너무 시큼해요", "thread_id": "user-1"}'

# 5) 자체 평가 (리포트: evaluation/roundN_report.md)
python evaluation/run_eval.py --round 3 --raw round3_raw.json
```

## RAGAS 평가 결과
3차(최종) 기준, 정답 답변이 있는 13건 평균이다.

- context_recall: 0.92
- context_precision: 0.75
- faithfulness: 0.77 (목표 0.80에 0.03 못 미침)
- answer_relevancy: 0.53

| 지표 | 1차 | 2차 | 3차 |
|---|---|---|---|
| context_recall | 0.77 | 0.91 | 0.92 |
| context_precision | 0.79 | 0.74 | 0.75 |
| faithfulness | 0.67 | 0.73 | 0.77 |
| answer_relevancy | 0.26 | 0.50 | 0.53 |

## 인-아웃 세트 통과율 (자체 평가)
- 1차 (Day 9 종료): 13 / 20 통과 — [round1_report.md](evaluation/round1_report.md)
- 2차 (Day 10 개선 후): 17 / 20 통과 — [round2_report.md](evaluation/round2_report.md)
- 3차 (Day 10 추가 개선): **19 / 20 통과** — [round3_report.md](evaluation/round3_report.md)
- 개선폭: +6건
  - 1차 → 2차 +4건: 이 중 3건(5 · 7 · 17)은 에이전트가 아니라 평가기의 숫자 검사가 맞는 답을 잘못 걸러 낸 것을 고친 효과다. 고친 평가기로 1차를 다시 채점하면 16 / 20이고, 에이전트 개선 효과는 +1건이다.
  - 2차 → 3차 +2건: 변수별 맛 영향을 묻는 질문에는 별도 절을 쓰고(4), 외부 원두로 보여도 카탈로그를 먼저 조회하도록(13) 프롬프트를 고쳤다.
  - 남은 실패 1건(1)은 Judge 오판이다. 컵노트 설명이 원두 스펙 시트 문장을 거의 그대로 옮긴 것인데, 문서 원문을 보지 못하는 Judge가 근거 없음으로 판정했다.

## 트라이앤에러 회고

### 시도했지만 실패한 접근 · 왜 실패했는가
- **맛 교정을 한 번에 끝내는 설계**([ADR-0001](docs/adr/0001-single-shot-taste-correction.md), 폐기): 교정 뒤 맛이 어떻게 바뀌었는지 이어서 볼 수 없었다. 결국 "3회까지 교정하고, 그래도 안 맞으면 원두 추천"으로 바꾸고 `thread_id` 대화 상태를 넣었다.
- **숫자를 모델이 직접 계산**: 드리퍼 보정(칼리타 +10클릭, 케멕스 +25클릭 · 원두량 ×2)과 푸어 스케줄을 모델에게 맡기면 계산이 틀렸다. 계산을 모두 `calc_recipe` · `suggest_adjustment`로 옮기고, 평가에서도 "답변 숫자 = 도구 출력"을 규칙으로 검사했다.
- **문서 통째 분할**: 용어 사전을 한 덩어리로 두니 "뜸 들이기" 같은 용어가 검색에 걸리지 않았다. 용어마다 `###` 제목을 달고 `###` 단위로도 나눠 해결했다.
- **평가기를 믿고 에이전트만 고치기**: 1차 실패 7건 중 3건은 평가기의 숫자 검사 버그였다. 예를 들어 교정 답변에서 바뀌기 전 값까지 검사했다. 평가기를 먼저 고친 뒤 1차를 다시 채점해서, 에이전트 개선 효과와 평가기 효과를 나눠 보고했다.
- **영어 기본 프롬프트로 RAGAS 채점**: 한국어 답변인데 answer_relevancy가 0.26으로 나왔다. 한국어로 바꾼 프롬프트(`adapt_prompts`)를 적용하자 0.50으로 올랐다.
- **환경 문제**
  - `us.` 프로필 Sonnet이 일일 토큰 한도에 걸려 `global.` 프로필로 바꿨다.
  - Python 3.14에서 ragas 의존성 빌드가 실패해 `--no-deps`로 설치하고 `langchain-community`를 0.4.1로 고정했다.
  - 처음 정한 로스터리 이름이 실제 카페와 겹쳐 "메리드립 로스터스"로 바꿨다.

### 최종 채택한 접근 · 왜 그것으로 갔는가
- **결정적 도구 + LLM 오케스트레이션**: 레시피 숫자와 교정 규칙(변수 하나만, 허용 범위, 반대 증상이면 절반 폭, 3회 한도)은 파이썬 함수로 두고, 모델은 의도 파악 · 도구 선택 · 설명만 맡는다. 숫자 정확도를 규칙으로 검증할 수 있다.
- **설명은 RAG 근거로만**: 맛 영향 · 용어 · 교정 이유 · 컵노트는 `retrieve_docs` 결과에 근거하고 문서 제목으로 출처를 단다. 3차에서 "문서 문장을 가깝게 옮기고 상식 · 비유를 덧붙이지 않기"를 넣자, 다른 그라인더 케이스의 faithfulness가 0.53에서 0.88로 올랐다.
- **고정 문구 · 위험 질문은 코드 가드레일로**: 프롬프트만으로는 "분쇄도는 비공식 시작값" 문구가 빠지거나 다른 그라인더에 클릭 수가 새는 경우가 있었다. 그래서 미들웨어로 강제한다.
- **매버릭 비공식 클릭 기준**([ADR-0002](docs/adr/0002-maverick-unofficial-click-baseline.md)): 제조사 공식 표가 없어 커뮤니티 수치(200~240클릭)를 기준으로 삼았다. 그 대신 모든 답변에 "비공식 시작값"임을 밝힌다.

### 남은 한계 · 향후 개선 방향
- **faithfulness 0.77**: 원두 추천 답변(0.57)에 권유 문장이 붙고, 용어 풀이 일부가 일반 상식으로 쓰인다.
  - 용어 풀이는 용어 사전에 있는 용어로만 한정한다.
  - 원두 추천은 기준 문장과 점수만 쓴다.
- **Judge의 한계**: Judge가 근거 문서를 보지 못해 출처 판단을 오판한다. Judge에 근거 문서 원문을 함께 넘기는 방식이 필요하다.
- **출력 가드레일 과잉**: 분쇄도를 바꾸지 않은 교정 답변에도 유지값 "190클릭" 때문에 분쇄도 고정 문구가 붙는다.
- **대화 상태**: `InMemorySaver`라 서버를 재시작하면 대화가 사라진다. 실서비스라면 영속 체크포인터가 필요하다.
- **확장**
  - 매버릭 외 메이저 그라인더(코만단테, 1Zpresso 등)의 클릭 환산표를 추가한다.
  - 카탈로그를 실제 로스터리 데이터로 연결한다.

## 핵심 코드 위치
- [src/agent.py:151](src/agent.py#L151) — 메인 에이전트 그래프 (`build_agent`: create_agent + 미들웨어 + 체크포인터)
- [src/agent.py:24](src/agent.py#L24) — 시스템 프롬프트 (작업 순서 · 답변 형식)
- [src/agent.py:100](src/agent.py#L100) · [src/agent.py:122](src/agent.py#L122) — 입력 · 출력 가드레일
- [src/agent.py:171](src/agent.py#L171) — `run_query`: answer · contexts · trace 조립
- [src/tools.py:358](src/tools.py#L358) — 도메인 도구 목록 (`calc_recipe` 166행, `suggest_adjustment` 241행, `recommend_bean` 306행)
- [src/retriever.py:53](src/retriever.py#L53) — 하이브리드 검색 (BM25 + Chroma, RRF)
- [src/server.py:26](src/server.py#L26) — `POST /query`
- [evaluation/run_eval.py](evaluation/run_eval.py) — 규칙 검사 · LLM-as-Judge · RAGAS 평가 스크립트
