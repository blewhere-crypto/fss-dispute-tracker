# -*- coding: utf-8 -*-
"""
금융감독원(FSS) 분쟁조정사례 크롤러
- 목록: https://www.fss.or.kr/fss/bbs/B0000390/list.do
- 상세: https://www.fss.or.kr/fss/bbs/B0000390/view.do?nttId=...

사용법:
    python scraper.py                 # 기본 대상(금융투자 전체 + 은행ㆍ중소서민>기타) 크롤링 후 저장
    python scraper.py --max-pages 3   # 테스트용으로 앞 3페이지만
    python scraper.py --detail        # 상세 페이지(요약글 + hwp 첨부링크)까지 수집
    python scraper.py --category A --cl2 A3   # 특정 카테고리 하나만 크롤링

주의:
- 사이트 구조는 2026-07 기준으로 확인한 것이며, 금감원이 페이지 구조를 바꾸면
  parse_list()의 셀렉터를 다시 맞춰야 할 수 있습니다.
- 이 스크립트는 목록/요약/첨부파일 링크까지만 수집합니다. hwp 본문 텍스트 추출과
  AI 분석은 ai_analyze.py가 이어받아 처리합니다 (기존 분석 결과는 건드리지 않음).
"""

import argparse
import json
import re
import time
from datetime import datetime, timezone, timedelta

import requests
from bs4 import BeautifulSoup

BASE = "https://www.fss.or.kr/fss/bbs/B0000390"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

CATEGORY_MAP = {"": "전체", "A": "은행ㆍ중소서민", "B": "보험", "C": "금융투자"}
KST = timezone(timedelta(hours=9))

# 카테고리 인자 없이 실행할 때(정기 크롤링) 기본으로 긁어오는 대상.
# 금융투자 분야 전체 + 은행ㆍ중소서민 중 "기타"(DLF/KIKO/라임 등 복합금융상품 손해배상 사례가
# 여기 분류돼 있어 금융투자 분쟁조정사례 대장 취지에 맞음)
DEFAULT_CATEGORIES = [
    {"cl1Cd": "C", "cl2Cd": "", "label": "금융투자"},
    {"cl1Cd": "A", "cl2Cd": "A3", "label": "은행ㆍ중소서민-기타"},
]


def fetch_list_page(session: requests.Session, page_index: int = 1, cl1cd: str = "", cl2cd: str = "") -> str:
    params = {"viewType": "BODY", "pageIndex": page_index}
    if cl1cd:
        params["cl1Cd"] = cl1cd
    if cl2cd:
        params["cl2Cd"] = cl2cd
    resp = session.get(f"{BASE}/list.do", params=params, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    return resp.text


def get_total_pages(html: str) -> int:
    m = re.search(r"\[\s*\d+\s*/\s*(\d+)\s*페이지\s*\]", html)
    return int(m.group(1)) if m else 1


def parse_list(html: str):
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("table")
    rows = []
    if not table:
        return rows

    body = table.select_one("tbody") or table
    for tr in body.select("tr"):
        tds = tr.find_all("td")
        if len(tds) < 6:
            continue
        num = tds[0].get_text(strip=True)
        if not num.isdigit():
            continue
        region = tds[1].get_text(strip=True)
        case_type = tds[2].get_text(strip=True)
        title_cell = tds[3]
        title_tag = title_cell.find("a")
        title = (title_tag.get_text(strip=True) if title_tag else title_cell.get_text(strip=True))
        href = title_tag["href"] if title_tag and title_tag.has_attr("href") else ""
        ntt_match = re.search(r"nttId=(\d+)", href)
        ntt_id = ntt_match.group(1) if ntt_match else None
        dept = tds[4].get_text(strip=True)
        date = tds[5].get_text(strip=True)
        views = tds[-1].get_text(strip=True)

        rows.append({
            "num": num,
            "region": region,
            "type": case_type,
            "title": title,
            "nttId": ntt_id,
            "dept": dept,
            "date": date,
            "views": views,
            "url": (
                f"{BASE}/view.do?nttId={ntt_id}&viewType=BODY&pageIndex=1"
                if ntt_id else None
            ),
        })
    return rows


def fetch_detail(session: requests.Session, ntt_id: str):
    """상세 페이지에서 요약 문구 + hwp 첨부파일 다운로드 링크를 가져온다."""
    result = {"summary": "", "attachments": []}
    try:
        params = {"nttId": ntt_id, "viewType": "BODY", "pageIndex": 1}
        resp = session.get(f"{BASE}/view.do", params=params, headers=HEADERS, timeout=15)
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
    except Exception:
        pass
    return result


def crawl_all(max_pages=None, with_detail=False, delay=0.6, cl1cd="C", cl2cd=""):
    """total_pages 텍스트 파싱에 의존하지 않고, 빈 페이지가 나올 때까지 계속 다음
    페이지를 읽어온다 (사이트 문구가 바뀌어도 안전하게 동작하도록).
    cl1cd(1차 분류: ""=전체, "A"=은행ㆍ중소서민, "B"=보험, "C"=금융투자)와
    cl2cd(2차 분류, 예: "A3"=은행ㆍ중소서민>기타)로 특정 카테고리만 좁혀서 가져올 수 있다."""
    session = requests.Session()
    all_rows = []
    page = 1
    hard_cap = max_pages or 300  # 안전장치: 무한루프 방지용 상한
    while page <= hard_cap:
        html = fetch_list_page(session, page, cl1cd=cl1cd, cl2cd=cl2cd)
        rows = parse_list(html)
        if not rows:
            break
        all_rows.extend(rows)
        page += 1
        time.sleep(delay)

    if with_detail:
        for row in all_rows:
            if row["nttId"]:
                detail = fetch_detail(session, row["nttId"])
                row["summary"] = detail["summary"]
                row["attachments"] = detail["attachments"]
                time.sleep(delay)

    return all_rows


def build_stats(cases):
    by_region, by_type, by_month, keywords = {}, {}, {}, {}
    for c in cases:
        by_region[c["region"]] = by_region.get(c["region"], 0) + 1
        by_type[c["type"]] = by_type.get(c["type"], 0) + 1
        month = c["date"][:7] if c.get("date") else "unknown"
        by_month[month] = by_month.get(month, 0) + 1
        for kw in (c.get("analysis") or {}).get("keywords", []):
            keywords[kw] = keywords.get(kw, 0) + 1
    return {"by_region": by_region, "by_type": by_type, "by_month": by_month, "keywords": keywords}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-pages", type=int, default=None)
    parser.add_argument("--detail", action="store_true")
    parser.add_argument("--out", default="data/data.json")
    parser.add_argument("--category", default=None,
                         help='cl1Cd 값을 직접 지정해 이 카테고리 하나만 크롤링 '
                              '(""=전체, "A"=은행ㆍ중소서민, "B"=보험, "C"=금융투자). '
                              '지정하지 않으면 기본 대상(금융투자 전체 + 은행ㆍ중소서민>기타)을 모두 크롤링')
    parser.add_argument("--cl2", default="", help="--category와 함께 쓰는 2차 분류(cl2Cd) 값, 예: A3")
    args = parser.parse_args()

    import os
    existing = {}
    if os.path.exists(args.out):
        try:
            with open(args.out, "r", encoding="utf-8") as f:
                prev = json.load(f)
            for c in prev.get("cases", []):
                if c.get("nttId"):
                    existing[c["nttId"]] = c
        except Exception:
            pass

    categories = (
        [{"cl1Cd": args.category, "cl2Cd": args.cl2, "label": args.category or "전체"}]
        if args.category is not None
        else DEFAULT_CATEGORIES
    )

    cases = []
    seen_ntt = set()
    for cat in categories:
        rows = crawl_all(
            max_pages=args.max_pages, with_detail=args.detail,
            cl1cd=cat["cl1Cd"], cl2cd=cat["cl2Cd"],
        )
        added = 0
        for row in rows:
            ntt = row.get("nttId")
            if ntt and ntt in seen_ntt:
                continue
            if ntt:
                seen_ntt.add(ntt)
            cases.append(row)
            added += 1
        print(f"[{cat['label']}] {added}건 수집")

    for c in cases:
        old = existing.get(c.get("nttId"))
        if old and old.get("analysis"):
            c["analysis"] = old["analysis"]

    cases.sort(key=lambda c: c["date"], reverse=True)

    payload = {
        "updated_at": datetime.now(KST).isoformat(),
        "total": len(cases),
        "stats": build_stats(cases),
        "cases": cases,
    }

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"저장 완료: {args.out} ({len(cases)}건)")


if __name__ == "__main__":
    main()
