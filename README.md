# 금융분쟁 조정사례 대장

금융감독원(FSS) 분쟁조정사례를 매일 자동 수집해서 검색·필터링할 수 있는 웹 대시보드입니다.

```
fss-dispute-tracker/
├── scraper.py                       # 크롤러
├── requirements.txt
├── data/data.json                   # 크롤링 결과 (초기 샘플 10건 포함)
├── index.html                       # 대시보드 (검색/필터/통계)
└── .github/workflows/daily-scrape.yml  # 매일 자동 실행
```

## 1) 로컬에서 먼저 테스트해보기

```bash
pip install -r requirements.txt

# 3페이지만 빠르게 테스트 (약 30건)
python scraper.py --max-pages 3

# 로컬 서버로 열어보기 (fetch가 file:// 에서는 막힐 수 있어서 서버로 띄우는 게 안전합니다)
python -m http.server 8000
# 브라우저에서 http://localhost:8000 접속
```

전체 812건을 다 받으려면 `python scraper.py --detail` (약 10~15분 소요, 서버 부담을 줄이기 위해 요청 사이 0.6초 delay가 들어가 있습니다).

## 2) 매일 자동 업데이트 배포 (무료, GitHub Pages + GitHub Actions)

1. GitHub에 새 저장소를 만들고 이 폴더를 통째로 push합니다.
2. 저장소 **Settings → Pages**에서 Source를 `main` 브랜치 `/ (root)`로 설정합니다.
   → `https://<아이디>.github.io/<저장소명>/` 로 접속 가능해집니다.
3. **Settings → Actions → General → Workflow permissions**에서
   "Read and write permissions"를 선택합니다. (크롤러가 결과를 커밋하기 위해 필요)
4. 그대로 두면 매일 한국시간 오전 8시에 자동으로 크롤링 → `data/data.json` 갱신 → 커밋됩니다.
   지금 바로 한 번 돌려보고 싶으면 저장소의 **Actions** 탭 → `Daily FSS Dispute Case Scrape` → **Run workflow** 를 누르세요.

이후로는 그냥 GitHub Pages 주소를 즐겨찾기 해두고 열 때마다 최신 데이터를 보면 됩니다.

## 알아두어야 할 제약사항

- **본문 전문은 .hwp 첨부파일 안에 있습니다.** 목록 페이지엔 제목/유형/날짜/짧은 안내문 정도만 나오고, 실제 조정결정문 전체 내용은 각 사례의 hwp 파일을 열어야 확인할 수 있어요. 지금 버전은 hwp를 파싱하지 않고 원문 링크만 제공합니다.
- **사이트 구조가 바뀌면 크롤러가 깨질 수 있습니다.** `scraper.py`의 `parse_list()` 셀렉터를 다시 맞춰야 할 수 있어요.
- **서버 부담을 고려해 요청 간 delay를 유지하고, 과도하게 자주 돌리지 마세요** (하루 1회면 충분합니다). 금감원 홈페이지 이용약관/robots.txt도 한 번 확인해보시는 걸 권장드려요.

## 다음에 추가하면 좋은 기능

- hwp 첨부파일까지 다운받아 텍스트를 뽑아내면(예: `pyhwp`, 또는 GitHub Actions에 LibreOffice 설치 후 headless 변환) 조정결정문 본문 검색도 가능해집니다.
- 본문 텍스트가 확보되면 Claude API로 사례별 요약/키워드 자동 태깅을 붙일 수 있어요.
- 신규 사례 등록 시 이메일/슬랙 알림을 붙이는 것도 GitHub Actions에서 어렵지 않게 추가할 수 있습니다.
