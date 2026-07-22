# -*- coding: utf-8 -*-
"""
data/data.json의 각 사례에 대해:
  1) hwp 첨부파일을 다운로드해 본문 텍스트 추출 (실패하면 목록 요약글로 대체)
  2) Gemini 무료 API(gemini-flash-lite-latest)로 상세 분석(사건개요/양측주장/판단근거/
     관련법령/결정요지/시사점/키워드) 생성
  3) 결과를 case["analysis"]에 저장

이미 analysis가 있는 사례는 건너뜁니다 (호출 절약 + 재실행 안전).

무료 API 키 발급: https://aistudio.google.com/apikey (카드 등록 불필요)

환경변수:
    GEMINI_API_KEY   Gemini API 키 (필수)

사용법:
    python ai_analyze.py                # data/data.json 전체 처리
    python ai_analyze.py --limit 20      # 테스트로 20건만
    python ai_analyze.py --reanalyze     # 기존 analysis도 새 형식으로 다시 생성
"""

import argparse
import json
import os
import re
import subprocess
import tempfile
import time

import requests
from google import genai

DATA_PATH = "data/data.json"
MODEL = "gemini-flash-lite-latest"  # 무료 티어에서 일일 한도가 가장 넉넉한 모델

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

SYSTEM_PROMPT = """당신은 금융 분쟁조정 사례를 분석하는 어시스턴트입니다.
주어진 금융감독원 분쟁조정 사례 텍스트를 읽고 아래 JSON 형식으로만, 최대한 상세하고
구체적으로 답하세요. 다른 설명이나 마크다운 코드블록 없이 순수 JSON만 출력하세요.

{
  "background": "사건 개요를 3~4문장으로. 누가(신청인 유형), 언제, 어떤 금융상품/거래를 했고 무슨 문제가 발생했는지 구체적으로",
  "claimant_argument": "신청인(금융소비자) 측 주장을 2~3문장으로 구체적으로",
  "respondent_argument": "피신청인(금융회사) 측 주장 또는 항변을 2~3문장으로 구체적으로",
  "issue": "핵심 쟁점을 2~3문장으로, 무엇이 법적/사실적으로 다투어졌는지",
  "reasoning": "조정위원회가 어떤 근거로 판단했는지 3~5문장으로. 관련 법령·약관·판례가 언급되어 있다면 반드시 포함",
  "decision": "최종 결정 요지를 2~3문장으로. 배상비율이나 금액이 언급되어 있다면 반드시 포함",
  "related_law": "관련 법령, 약관 조항, 감독규정 등을 알 수 있는 범위에서 나열 (모르면 빈 문자열)",
  "consumer_lesson": "일반 금융소비자가 얻을 수 있는 실질적 시사점을 2~3문장으로",
  "keywords": ["키워드1", "키워드2", "키워드3", "키워드4"]
}

keywords는 4~6개, 명사형 짧은 단어(예: "불완전판매", "청약철회", "보이스피싱", "설명의무위반")로 작성하세요.
텍스트에 없는 내용은 절대 지어내지 말고, "본문에서 확인되지 않음" 등으로 명시하세요.
텍스트가 너무 짧아 특정 항목을 채울 수 없으면 해당 항목은 빈 문자열로 두세요.
"""


def extract_hwp_text(url: str) -> str:
    """hwp 파일을 다운로드해 hwp5txt로 텍스트 추출. 실패 시 빈 문자열."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        with tempfile.NamedTemporaryFile(suffix=".hwp", delete=False) as tf:
            tf.write(resp.content)
            tmp_path = tf.name
        try:
            out = subprocess.run(["hwp5txt", tmp_path], capture_output=True, timeout=30)
            if out.returncode == 0:
                return out.stdout.decode("utf-8", errors="ignore")
        finally:
            os.unlink(tmp_path)
    except Exception:
        pass
    return ""


def get_source_text(case: dict) -> str:
    for att in case.get("attachments", []):
        if att["url"].lower().endswith(".hwp") or "fileDown" in att["url"]:
            text = extract_hwp_text(att["url"])
            if len(text.strip()) > 50:
                return text[:12000]  # 상세 분석을 위해 앞부분을 더 넉넉히 사용
    if case.get("summary"):
        return case["summary"]
    return case["title"]


def analyze_case(client, case: dict) -> dict:
    text = get_source_text(case)
    prompt = (
        f"{SYSTEM_PROMPT}\n\n"
        f"[사례 제목] {case['title']}\n[유형] {case['region']} / {case['type']}\n\n"
        f"[본문 또는 요약]\n{text}"
    )
    resp = client.models.generate_content(model=MODEL, contents=prompt)
    raw = (resp.text or "").strip()
    raw = re.sub(r"^```json|```$", "", raw, flags=re.MULTILINE).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {
            "background": "", "claimant_argument": "", "respondent_argument": "",
            "issue": "", "reasoning": "", "decision": "", "related_law": "",
            "consumer_lesson": "", "keywords": [], "raw": raw,
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="처리할 최대 건수 (테스트용)")
    parser.add_argument("--delay", type=float, default=4.2, help="무료 티어 분당 요청수 제한(RPM) 보호용 딜레이(초)")
    parser.add_argument("--reanalyze", action="store_true", help="기존 analysis가 있어도 새 형식으로 다시 생성")
    args = parser.parse_args()

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise SystemExit("GEMINI_API_KEY 환경변수가 설정되어 있지 않습니다.")
    client = genai.Client(api_key=api_key)

    with open(DATA_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    if args.reanalyze:
        todo = data["cases"]
    else:
        # 예전 형식(issue/decision/consumer_lesson만 있음)도 다시 채우도록 background 유무로 판단
        todo = [c for c in data["cases"] if not c.get("analysis") or "background" not in c["analysis"]]

    if args.limit:
        todo = todo[:args.limit]

    print(f"분석 대상: {len(todo)}건 (전체 {len(data['cases'])}건 중)")

    for i, case in enumerate(todo, 1):
        try:
            case["analysis"] = analyze_case(client, case)
            print(f"[{i}/{len(todo)}] 완료: {case['title'][:40]}")
        except Exception as e:
            print(f"[{i}/{len(todo)}] 실패: {case['title'][:40]} ({e})")
        time.sleep(args.delay)

        if i % 10 == 0:
            with open(DATA_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

    keywords = {}
    for c in data["cases"]:
        for kw in (c.get("analysis") or {}).get("keywords", []):
            keywords[kw] = keywords.get(kw, 0) + 1
    data.setdefault("stats", {})["keywords"] = keywords

    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print("전체 저장 완료")


if __name__ == "__main__":
    main()
