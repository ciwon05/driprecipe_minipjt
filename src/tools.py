"""도메인 도구: 원두 조회, 레시피 계산, 맛 증상 분류, 맛 교정, 원두 추천, 문서 검색."""

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from langchain_aws import ChatBedrockConverse
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from retriever import retrieve

load_dotenv()

BEANS_PATH = Path(__file__).resolve().parent.parent / "data" / "beans.json"

# 드리퍼별 V60 대비 보정값과 푸어 방법 (data/docs/dripper_*.md 와 같은 값)
DRIPPERS = {
    "V60": {"aliases": ("v60", "하리오", "브이60"), "grind": 0, "time": 0, "dose_x": 1, "pours": 3, "interval": 30},
    "칼리타 웨이브": {"aliases": ("칼리타", "kalita", "웨이브"), "grind": 10, "time": 30, "dose_x": 1, "pours": 3, "interval": 30},
    "케멕스": {"aliases": ("케멕스", "케맥스", "chemex"), "grind": 25, "time": 90, "dose_x": 2, "pours": 4, "interval": 45},
}

# 외부 원두용 로스팅 정도별 V60 일반 레시피 (data/docs/dripper_v60.md 와 같은 값)
GENERAL_RECIPES = {
    "라이트": {"dose_g": 15, "ratio": 16, "water_temp_c": 93, "grind_clicks": 205, "total_time_s": 150},
    "미디엄라이트": {"dose_g": 15, "ratio": 16, "water_temp_c": 92, "grind_clicks": 212, "total_time_s": 160},
    "미디엄": {"dose_g": 15, "ratio": 16, "water_temp_c": 91, "grind_clicks": 218, "total_time_s": 165},
    "미디엄다크": {"dose_g": 15, "ratio": 15, "water_temp_c": 89, "grind_clicks": 225, "total_time_s": 180},
    "다크": {"dose_g": 15, "ratio": 15, "water_temp_c": 88, "grind_clicks": 225, "total_time_s": 180},
}

BLOOM_S = 30
FRESH_BLOOM_S = 45
GRIND_RANGE = (190, 250)
TEMP_RANGE = (85, 96)
RATIO_RANGE = (13, 18)
MAX_ROUNDS = 3

Symptom = Literal["신맛 과다", "쓴맛 과다", "떫음", "밍밍함", "너무 진함"]
OPPOSITE = {"신맛 과다": "쓴맛 과다", "쓴맛 과다": "신맛 과다", "밍밍함": "너무 진함", "너무 진함": "밍밍함"}
# 맛 증상별 기본 교정: (변수, 바꿀 양)
BASE_STEP = {
    "신맛 과다": ("grind_clicks", -10),
    "쓴맛 과다": ("grind_clicks", +10),
    "떫음": ("grind_clicks", +10),
    "밍밍함": ("ratio", -1),
    "너무 진함": ("ratio", +1),
}
# 분쇄도가 허용 범위 끝일 때 넘어가는 물 온도 교정
TEMP_STEP = {"신맛 과다": +2, "쓴맛 과다": -2, "떫음": -2}
LIMITS = {"grind_clicks": GRIND_RANGE, "water_temp_c": TEMP_RANGE, "ratio": RATIO_RANGE}
LABELS = {"grind_clicks": "분쇄도(클릭)", "water_temp_c": "물 온도(℃)", "ratio": "비율(1:N의 N)"}


@lru_cache(maxsize=1)
def load_beans() -> list[dict]:
    """원두 카탈로그 JSON을 읽어 원두 목록을 돌려준다."""
    return json.loads(BEANS_PATH.read_text(encoding="utf-8"))["beans"]


def find_bean(name: str) -> dict | list[dict] | None:
    """원두 이름이나 별칭으로 카탈로그 원두를 찾는다.

    질의 안에 들어 있는 가장 긴 이름·별칭을 가진 원두를 고른다.
    같은 길이로 여러 원두가 걸리면 후보 목록을, 하나도 없으면 None을 돌려준다.
    """
    query = name.replace(" ", "")
    best_len, matches = 0, []
    for bean in load_beans():
        for label in (bean["name"], *bean["aliases"]):
            key = label.replace(" ", "")
            if key and (key in query or query == key):
                if len(key) > best_len:
                    best_len, matches = len(key), [bean]
                elif len(key) == best_len and bean not in matches:
                    matches.append(bean)
    if not matches:
        return None
    return matches[0] if len(matches) == 1 else matches


def normalize_dripper(dripper: str) -> str | None:
    """드리퍼 이름을 표준 이름(V60 · 칼리타 웨이브 · 케멕스)으로 바꾸고, 지원하지 않으면 None을 돌려준다."""
    text = dripper.lower().replace(" ", "")
    for name, spec in DRIPPERS.items():
        if any(a in text for a in spec["aliases"]):
            return name
    return None


def format_time(seconds: int) -> str:
    """초를 '분:초' 문자열로 바꾼다."""
    return f"{seconds // 60}:{seconds % 60:02d}"


def build_recipe(v60: dict, dripper: str, roast_days_ago: int | None) -> dict:
    """V60 기준 레시피를 드리퍼에 맞게 보정하고 푸어 스케줄을 만든다."""
    spec = DRIPPERS[dripper]
    dose = v60["dose_g"] * spec["dose_x"]
    water = dose * v60["ratio"]
    bloom_water = dose * 2
    bloom_s = FRESH_BLOOM_S if roast_days_ago is not None and roast_days_ago < 4 else BLOOM_S

    schedule = [{"time": "0:00", "step": "뜸 들이기", "pour_g": bloom_water, "total_g": bloom_water}]
    remaining, total = water - bloom_water, bloom_water
    for i in range(spec["pours"]):
        pour = round(remaining / spec["pours"]) if i < spec["pours"] - 1 else water - total
        total += pour
        schedule.append({
            "time": format_time(bloom_s + i * spec["interval"]),
            "step": f"{i + 1}차 푸어",
            "pour_g": pour,
            "total_g": total,
        })

    notes = []
    if dripper != "V60":
        minutes, seconds = divmod(spec["time"], 60)
        added = " ".join(p for p in (f"{minutes}분" if minutes else "", f"{seconds}초" if seconds else "") if p)
        parts = [f"분쇄도 +{spec['grind']}클릭", f"목표 총 추출 시간 +{added}"]
        if spec["dose_x"] > 1:
            parts.append(f"원두량 ×{spec['dose_x']}({spec['dose_x']}인분)")
        notes.append(f"V60 권장 레시피에서 {dripper} 보정값({', '.join(parts)})을 적용했습니다.")
    if roast_days_ago is not None:
        if roast_days_ago < 4:
            notes.append("볶은 지 4일 미만이라 가스가 많으니 뜸 들이기를 45초로 늘렸습니다.")
        elif roast_days_ago > 42:
            notes.append("볶은 지 6주가 넘어 향이 약해졌을 수 있습니다.")

    return {
        "dripper": dripper,
        "dose_g": dose,
        "water_g": water,
        "ratio": f"1:{v60['ratio']}",
        "water_temp_c": v60["water_temp_c"],
        "grind_clicks": v60["grind_clicks"] + spec["grind"],
        "total_time": format_time(v60["total_time_s"] + spec["time"]),
        "bloom_s": bloom_s,
        "pour_schedule": schedule,
        "notes": notes,
    }


@tool
def search_bean(name: str) -> str:
    """원두 이름(별칭 포함)으로 메리드립 로스터스 원두 카탈로그를 조회한다.

    찾으면 산지·가공 방식·로스팅 정도·컵노트·맛 프로필·V60 기준 권장 레시피를 돌려준다.
    카탈로그에 없으면 '카탈로그에 없음'을 돌려주며, 이때 그 원두는 외부 원두로 다룬다.
    """
    found = find_bean(name)
    if found is None:
        return json.dumps({"result": "카탈로그에 없음", "query": name}, ensure_ascii=False)
    if isinstance(found, list):
        return json.dumps(
            {"result": "후보 여러 개", "candidates": [b["name"] for b in found]}, ensure_ascii=False
        )
    return json.dumps({"result": "찾음", "bean": found}, ensure_ascii=False)


@tool
def calc_recipe(
    dripper: str,
    bean_name: str | None = None,
    roast_level: str | None = None,
    roast_days_ago: int | None = None,
) -> str:
    """원두와 드리퍼에 맞는 레시피(원두량·물량·비율·물 온도·분쇄도 클릭·목표 총 추출 시간)와 푸어 스케줄을 계산한다.

    카탈로그 원두는 bean_name을, 외부 원두는 roast_level(라이트·미디엄라이트·미디엄·미디엄다크·다크)을 넘긴다.
    roast_days_ago는 볶은 지 며칠 지났는지이며, 모르면 비워 둔다.
    분쇄도는 홀츠클로츠 매버릭 2.0 기준 비공식 시작값이다.
    """
    name = normalize_dripper(dripper)
    if name is None:
        return json.dumps(
            {"error": f"지원하지 않는 드리퍼: {dripper}", "supported": list(DRIPPERS)}, ensure_ascii=False
        )

    if bean_name:
        bean = find_bean(bean_name)
        if isinstance(bean, dict):
            recipe = build_recipe(bean["recipe_v60"], name, roast_days_ago)
            return json.dumps(
                {"type": "권장 레시피", "bean": bean["name"], "source_doc": bean["doc_id"], **recipe},
                ensure_ascii=False,
            )
        if not roast_level:
            return json.dumps(
                {"error": "카탈로그에 없는 원두입니다. 일반 레시피를 만들려면 로스팅 정도가 필요합니다."},
                ensure_ascii=False,
            )

    level = (roast_level or "").replace(" ", "")
    if level not in GENERAL_RECIPES:
        return json.dumps(
            {"error": "로스팅 정도를 알 수 없습니다.", "supported": list(GENERAL_RECIPES)}, ensure_ascii=False
        )
    recipe = build_recipe(GENERAL_RECIPES[level], name, roast_days_ago)
    return json.dumps(
        {"type": "일반 레시피", "roast_level": level, "source_doc": "dripper_v60", **recipe}, ensure_ascii=False
    )


class TasteClassification(BaseModel):
    """맛 증상 분류 결과."""

    symptom: Symptom | Literal["분류 불가"] = Field(description="맛 증상 5종 중 하나, 해당 없으면 '분류 불가'")
    reason: str = Field(description="그렇게 판단한 짧은 이유")


@tool
def classify_taste(description: str) -> str:
    """홈브루어의 맛 표현을 맛 증상 5종(신맛 과다·쓴맛 과다·떫음·밍밍함·너무 진함) 중 하나로 분류한다.

    어느 것에도 해당하지 않으면 '분류 불가'를 돌려준다. 이때는 교정하지 말고 어떤 맛인지 되물어야 한다.
    """
    llm = ChatBedrockConverse(model=os.environ["BEDROCK_FAST_MODEL_ID"], temperature=0)
    result = llm.with_structured_output(TasteClassification).invoke(
        "핸드드립 커피를 마신 사람의 맛 표현을 아래 다섯 가지 중 하나로 분류하라. "
        "시큼함·새콤함은 신맛 과다, 씀·탄 맛은 쓴맛 과다, 텁텁함·입이 마르는 느낌은 떫음, "
        "물 같음·연함은 밍밍함, 너무 진하고 무거움은 너무 진함이다. "
        "어느 것으로도 판단할 수 없으면 '분류 불가'로 하라.\n\n"
        f"맛 표현: {description}"
    )
    return result.model_dump_json()


def _move(recipe: dict, var: str, delta: float) -> float | None:
    """레시피 변수 하나를 delta만큼 옮긴 새 값을 돌려주고, 허용 범위를 벗어나면 None을 돌려준다."""
    low, high = LIMITS[var]
    new = recipe[var] + delta
    return new if low <= new <= high else None


@tool
def suggest_adjustment(
    symptom: Symptom,
    grind_clicks: int,
    water_temp_c: float,
    ratio: float,
    correction_round: int = 0,
    previous_symptom: Symptom | None = None,
    previous_variable: Literal["grind_clicks", "water_temp_c", "ratio"] | None = None,
) -> str:
    """맛 증상과 현재 레시피를 받아, 다음 추출에서 바꿀 추출 변수 하나와 새 값을 정한다.

    ratio는 1:N의 N 값이다. correction_round는 이 원두로 이미 받은 교정 횟수다.
    직전 교정에서 바꾼 변수(previous_variable)와 직전 맛 증상(previous_symptom)을 알면 함께 넘긴다.
    직전 맛 증상과 반대 증상이면 직전 변수를 절반 폭으로 되돌린다.
    교정 횟수가 이미 3회면 '교정 한도 도달'을 돌려주며, 이때는 recommend_bean으로 원두를 추천한다.
    """
    if correction_round >= MAX_ROUNDS:
        return json.dumps({"result": "교정 한도 도달", "correction_round": correction_round}, ensure_ascii=False)

    recipe = {"grind_clicks": grind_clicks, "water_temp_c": water_temp_c, "ratio": ratio}
    var, delta, reason = None, 0.0, ""

    if previous_symptom and previous_variable and OPPOSITE.get(previous_symptom) == symptom:
        base_var, base_delta = BASE_STEP[symptom]
        if previous_variable == "water_temp_c":
            full = TEMP_STEP[symptom]
        elif previous_variable == base_var:
            full = base_delta
        else:
            full = BASE_STEP[previous_symptom][1] * -1
        var, delta = previous_variable, full / 2
        reason = f"직전 교정 뒤 맛 증상이 {previous_symptom}에서 {symptom}(으)로 반대로 바뀌어, 직전에 바꾼 변수를 절반 폭으로 되돌립니다."
    else:
        var, delta = BASE_STEP[symptom]
        reason = f"{symptom}의 기본 교정입니다."
        if var == "grind_clicks" and _move(recipe, var, delta) is None:
            var, delta = "water_temp_c", TEMP_STEP[symptom]
            reason = f"분쇄도가 허용 범위({GRIND_RANGE[0]}~{GRIND_RANGE[1]}클릭) 끝이라 물 온도로 교정합니다."

    new = _move(recipe, var, delta)
    if new is None:
        return json.dumps(
            {"result": "허용 범위 초과", "variable": LABELS[var], "current": recipe[var]}, ensure_ascii=False
        )
    if isinstance(new, float) and new.is_integer():
        new = int(new)
    return json.dumps(
        {
            "result": "교정",
            "symptom": symptom,
            "variable": var,
            "variable_label": LABELS[var],
            "before": recipe[var],
            "after": new,
            "reason": reason,
            "unchanged": {LABELS[k]: v for k, v in recipe.items() if k != var},
            "correction_round": correction_round + 1,
            "max_rounds": MAX_ROUNDS,
            "round_label": f"교정 회차 {correction_round + 1}/{MAX_ROUNDS}회",
        },
        ensure_ascii=False,
    )


@tool
def recommend_bean(current_bean: str, symptom: Symptom) -> str:
    """교정 3회 뒤에도 맛 증상이 남을 때, 맛 프로필이 반대 방향인 카탈로그 원두 1종을 고른다.

    신맛 과다는 산미가 더 낮은 원두, 쓴맛 과다·떫음·너무 진함은 바디가 더 낮은 원두,
    밍밍함은 바디가 더 높은 원두를 고르며, 후보가 여럿이면 점수 차이가 가장 큰 원두를 고른다.
    조건에 맞는 원두가 없으면 '추천할 원두 없음'을 돌려준다.
    """
    current = find_bean(current_bean)
    if not isinstance(current, dict):
        return json.dumps({"result": "현재 원두가 카탈로그에 없음", "query": current_bean}, ensure_ascii=False)

    if symptom == "신맛 과다":
        axis, sign = "acidity", -1
    elif symptom == "밍밍함":
        axis, sign = "body", +1
    else:
        axis, sign = "body", -1

    base = current["flavor_profile"][axis]
    candidates = [
        (sign * (b["flavor_profile"][axis] - base), b)
        for b in load_beans()
        if b["id"] != current["id"] and sign * (b["flavor_profile"][axis] - base) > 0
    ]
    if not candidates:
        return json.dumps({"result": "추천할 원두 없음", "symptom": symptom}, ensure_ascii=False)
    diff, best = max(candidates, key=lambda c: c[0])
    return json.dumps(
        {
            "result": "추천",
            "bean": best["name"],
            "axis": "산미" if axis == "acidity" else "바디",
            "current_score": base,
            "recommended_score": best["flavor_profile"][axis],
            "cup_notes": best["cup_notes"],
            "source_doc": best["doc_id"],
        },
        ensure_ascii=False,
    )


@tool(response_format="content_and_artifact")
def retrieve_docs(query: str) -> tuple[str, list[dict]]:
    """메리드립 로스터스의 문서(원두 스펙 시트, 드리퍼 가이드, 매버릭 분쇄도 가이드, 추출 변수와 맛, 맛 교정 원리, 로스팅 경과일, 용어 사전)를 검색한다.

    컵노트 설명, 추출 변수가 맛에 주는 영향, 용어 풀이, 맛 교정의 이유는 반드시 이 도구가 찾은 문서를 근거로 쓴다.
    """
    contexts = retrieve(query)
    text = "\n\n".join(f"[{c['doc_id']}]\n{c['text']}" for c in contexts)
    return text, contexts


TOOLS = [search_bean, calc_recipe, classify_taste, suggest_adjustment, recommend_bean, retrieve_docs]
