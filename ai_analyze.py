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
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup
from google import genai

DATA_PATH = "data/data.json"
MODEL = "gemini-flash-lite-latest"  # 무료 티어에서 일일 한도가 가장 넉넉한 모델
BASE = "https://www.fss.or.kr/fss/bbs/B0000390"

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
    """hwp 파일을 다운로드해 LibreOffice로 텍스트 추출. 실패 시 빈 문자열(원인은 로그에 남김).
    (예전에는 pyhwp/hwp5txt를 썼으나, 파이썬 3.11 환경에서 계속 깨져서 LibreOffice 방식으로 교체)"""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        with tempfile.TemporaryDirectory() as tmpdir:
            hwp_path = os.path.join(tmpdir, "input.hwp")
            with open(hwp_path, "wb") as f:
                f.write(resp.content)
            out = subprocess.run(
                ["soffice", "--headless", "--convert-to", "txt:Text", "--outdir", tmpdir, hwp_path],
                capture_output=True, timeout=60
            )
            txt_path = os.path.join(tmpdir, "input.txt")
            if os.path.exists(txt_path):
                with open(txt_path, "r", encoding="utf-8", errors="ignore") as f:
                    return f.read()
            else:
                print(f"LibreOffice 변환 실패 (code={out.returncode}): {out.stderr.decode('utf-8', errors='ignore')[:300]}")
    except Exception as e:
        print(f"hwp 다운로드/변환 실패 ({url}): {e}")
    return ""


def fetch_detail_live(ntt_id: str):
    """저장된 데이터에 첨부파일 정보가 없을 때, 상세페이지에서 즉시 다시 가져온다."""
    result = {"summary": "", "attachments": []}
    try:
        params = {"nttId": ntt_id, "viewType": "BODY", "pageIndex": 1}
        resp = requests.get(f"{BASE}/view.do", params=params, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.text, "html.parser")

        for c in soup.select("div, p"):
            text = c.get_text(" ", strip=True)
            if len(text) > 30 and ("금융분쟁조정위원회" in text or "조정결정" in text or "등록합니다" in text):
                result["summary"] = text
                break

        for a in soup.select("a[href*='fileDown.do']"):
            href = a["href"]
            name = a.get_text(strip=True)
            if href.startswith("/"):
                href = "https://www.fss.or.kr" + href
            if name and href not in [x["url"] for x in result["attachments"]]:
                result["attachments"].append({"name": name, "url": href})
    except Exception as e:
        print(f"상세페이지 재조회 실패 (nttId={ntt_id}): {e}")
    return result


def get_source_text(case: dict) -> str:
    attachments = case.get("attachments", [])
    summary = case.get("summary", "")

    # 저장된 데이터에 첨부파일 정보가 없으면, 재분석 시 상세페이지에서 즉시 다시 가져온다
    # (analyze-single.yml처럼 크롤링 없이 분석만 도는 경우 이게 없으면 제목만 갖고 분석하게 됨)
    if not attachments and case.get("nttId"):
        live = fetch_detail_live(case["nttId"])
        attachments = live["attachments"]
        summary = summary or live["summary"]

    for att in attachments:
        if att["url"].lower().endswith(".hwp") or "fileDown" in att["url"]:
            text = extract_hwp_text(att["url"])
            if len(text.strip()) > 50:
                return text[:12000]  # 상세 분석을 위해 앞부분을 더 넉넉히 사용
    if summary:
        return summary
    return case["title"]


def analyze_case(client, case: dict) -> dict:
    text = get_source_text(case)
    prompt = (
        f"{SYSTEM_PROMPT}\n\n"
        f"[사례 제목] {case['title']}\n[유형] {case['region']} / {case['type']}\n\n"
        f"[본문 또는 요약]\n{text}"
    )
    resp = None
    last_err = None
    for attempt in range(3):
        try:
            resp = client.models.generate_content(model=MODEL, contents=prompt)
            break
        except Exception as e:
            last_err = e
            if "UNAVAILABLE" in str(e) or "503" in str(e):
                wait = 10 * (attempt + 1)
                print(f"Gemini 서버 과부하, {wait}초 후 재시도 ({attempt+1}/3)…")
                time.sleep(wait)
            else:
                raise
    if resp is None:
        raise last_err
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
    parser.add_argument("--since-days", type=int, default=None, help="최근 N일 이내 등록된 사례만 대상으로 함 (예: 30)")
    parser.add_argument("--ntt-id", default=None, help="이 nttId 하나만 강제로 재분석 (다른 필터 무시)")
    args = parser.parse_args()

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise SystemExit("GEMINI_API_KEY 환경변수가 설정되어 있지 않습니다.")
    client = genai.Client(api_key=api_key)

    with open(DATA_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    if args.ntt_id:
        todo = [c for c in data["cases"] if str(c.get("nttId")) == str(args.ntt_id)]
        if not todo:
            raise SystemExit(f"nttId={args.ntt_id} 인 사례를 찾을 수 없습니다.")
    elif args.reanalyze:
        todo = data["cases"]
    else:
        # 예전 형식(issue/decision/consumer_lesson만 있음)도 다시 채우도록 background 유무로 판단
        todo = [c for c in data["cases"] if not c.get("analysis") or "background" not in c["analysis"]]

    # 무료 API 한도에 걸려 중간에 멈추더라도 최근 사례가 항상 먼저 채워지도록 최신순 정렬
    todo.sort(key=lambda c: c.get("date", ""), reverse=True)

    if args.since_days:
        cutoff = (datetime.now() - timedelta(days=args.since_days)).strftime("%Y-%m-%d")
        todo = [c for c in todo if c.get("date", "") >= cutoff]

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
