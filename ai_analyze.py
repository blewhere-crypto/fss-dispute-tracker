# -*- coding: utf-8 -*-
"""
data/data.json의 각 사례에 대해:
  1) hwp 첨부파일을 다운로드해 본문 텍스트 추출 (실패하면 목록 요약글로 대체)
  2) Gemini 무료 API(gemini-2.5-flash-lite)로 쟁점/결정요지/소비자 시사점/키워드 생성
  3) 결과를 case["analysis"]에 저장

이미 analysis가 있는 사례는 건너뜁니다 (호출 절약 + 재실행 안전).

무료 API 키 발급: https://aistudio.google.com/apikey (카드 등록 불필요)

환경변수:
    GEMINI_API_KEY   Gemini API 키 (필수)

사용법:
    python ai_analyze.py                # data/data.json 전체 처리
    python ai_analyze.py --limit 20      # 테스트로 20건만
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
MODEL = "gemini-flash-lite-latest"  # 구글이 최신 버전으로 자동 연결해주는 별칭

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

SYSTEM_PROMPT = """당신은 금융 분쟁조정 사례를 분석하는 어시스턴트입니다.
주어진 금융감독원 분쟁조정 사례 텍스트를 읽고 아래 JSON 형식으로만 답하세요.
다른 설명이나 마크다운 코드블록 없이 순수 JSON만 출력하세요.

{
  "issue": "쟁점을 1~2문장으로 (무엇이 다투어졌는지)",
  "decision": "조정위원회의 결정 요지를 1~2문장으로",
  "consumer_lesson": "일반 금융소비자가 얻을 수 있는 시사점 1문장",
  "keywords": ["키워드1", "키워드2", "키워드3"]
}

keywords는 3~5개, 명사형 짧은 단어(예: "불완전판매", "청약철회", "보이스피싱", "실손보험금")로 작성하세요.
텍스트가 너무 짧거나 정보가 부족하면 알 수 있는 범위 내에서만 작성하고, 모르는 내용은 지어내지 마세요.
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
                return text[:6000]
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
        return {"issue": "", "decision": "", "consumer_lesson": "", "keywords": [], "raw": raw}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="처리할 최대 건수 (테스트용)")
    parser.add_argument("--delay", type=float, default=4.2, help="무료 티어 분당 요청수 제한(RPM) 보호용 딜레이(초)")
    args = parser.parse_args()

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise SystemExit("GEMINI_API_KEY 환경변수가 설정되어 있지 않습니다.")
    client = genai.Client(api_key=api_key)

    with open(DATA_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    todo = [c for c in data["cases"] if not c.get("analysis")]
    if args.limit:
        todo = todo[:args.limit]

    print(f"분석 대상: {len(todo)}건 (전체 {len(data['cases'])}건 중 미분석)")

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
