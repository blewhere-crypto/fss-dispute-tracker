# -*- coding: utf-8 -*-
"""
금융감독원(FSS) 분쟁조정사례 크롤러
- 목록: https://www.fss.or.kr/fss/bbs/B0000390/list.do
- 상세: https://www.fss.or.kr/fss/bbs/B0000390/view.do?nttId=...

사용법:
    python scraper.py                 # 전체 크롤링 후 data/data.json 저장
    python scraper.py --max-pages 3   # 테스트용으로 앞 3페이지만
    python scraper.py --detail        # 상세 페이지(짧은 설명글)까지 수집 (느림)

주의:
- 사이트 구조는 2026-07 기준으로 확인한 것이며, 금감원이 페이지 구조를 바꾸면
  parse_list()의 셀렉터를 다시 맞춰야 할 수 있습니다.
- 실제 조정결정문 '전문'은 첨부된 .hwp 파일 안에 있습니다. 이 스크립트는
  목록 정보 + (옵션) 상세페이지 요약글만 수집합니다.
- 서버 부담을 줄이기 위해 요청 사이 delay를 꼭 유지하세요.
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

# 대분류 코드 (cl1Cd)
CATEGORY_MAP = {
    "": "전체",
    "A": "은행ㆍ중소서민",
    "B": "보험",
    "C": "금융투자",
}

KST = timezone(timedelta(hours=9))


def fetch_list_page(session: requests.Session, page_index: int = 1) -> str:
    params = {"viewType": "BODY", "pageIndex": page_index}
    resp = session.get(f"{BASE}/list.do", params=params, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    return resp.text


def get_total_pages(html: str) -> int:
    # "전체 812건 [ 1 /82페이지 ]" 형태에서 총 페이지 수 추출
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


def fetch_detail_summary(session: requests.Session, ntt_id: str) -> str:
    """상세 페이지의 짧은 안내 문구(요약)를 가져온다. 실패하면 빈 문자열."""
    try:
        params = {"nttId": ntt_id, "viewType": "BODY", "pageIndex": 1}
        resp = session.get(f"{BASE}/view.do", params=params, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.text, "html.parser")
        # 본문 요약 문단 후보를 찾는다 (사이트 구조 변경 시 조정 필요)
        candidates = soup.select("div, p")
        best = ""
        for c in candidates:
            text = c.get_text(" ", strip=True)
            if len(text) > 30 and ("금융분쟁조정위원회" in text or "조정결정" in text or "등록합니다" in text):
                best = text
                break
        return best
    except Exception:
        return ""


def crawl_all(max_pages=None, with_detail=False, delay=0.6):
    session = requests.Session()
    first_html = fetch_list_page(session, 1)
    total_pages = get_total_pages(first_html)
    if max_pages:
        total_pages = min(total_pages, max_pages)

    all_rows = parse_list(first_html)
    for page in range(2, total_pages + 1):
        html = fetch_list_page(session, page)
        all_rows.extend(parse_list(html))
        time.sleep(delay)

    if with_detail:
        for row in all_rows:
            if row["nttId"]:
                row["summary"] = fetch_detail_summary(session, row["nttId"])
                time.sleep(delay)

    return all_rows


def build_stats(cases):
    by_region, by_type, by_month = {}, {}, {}
    for c in cases:
        by_region[c["region"]] = by_region.get(c["region"], 0) + 1
        by_type[c["type"]] = by_type.get(c["type"], 0) + 1
        month = c["date"][:7] if c["date"] else "unknown"
        by_month[month] = by_month.get(month, 0) + 1
    return {"by_region": by_region, "by_type": by_type, "by_month": by_month}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-pages", type=int, default=None)
    parser.add_argument("--detail", action="store_true")
    parser.add_argument("--out", default="data/data.json")
    args = parser.parse_args()

    cases = crawl_all(max_pages=args.max_pages, with_detail=args.detail)
    cases.sort(key=lambda c: c["date"], reverse=True)

    payload = {
        "updated_at": datetime.now(KST).isoformat(),
        "total": len(cases),
        "stats": build_stats(cases),
        "cases": cases,
    }

    import os
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"저장 완료: {args.out} ({len(cases)}건)")


if __name__ == "__main__":
    main()
