# IncidentDashboard

IncidentDashboard는 국내외 보안 사고(침해사고, 개인정보 유출, 해킹 이슈) 관련 뉴스를 주기적으로 수집하여 Notion 데이터베이스에 적재하는 자동화 프로젝트입니다.  
보안 담당자가 반복적으로 수행하는 사고 정보 수집 및 정리 업무를 줄이고, 보고서 초안 작성에 필요한 근거 자료를 빠르게 확보하는 것을 목표로 합니다.

---

## 개요

침해사고 대응 업무에서는 신속한 상황 파악과 내부 공유 체계 유지가 중요합니다.  
그러나 실제 업무 환경에서는 사고 관련 정보가 여러 기사로 분산되어 있고, 동일 사건이 반복 보도되며, 시간 차를 두고 추가 정보가 계속 생성되는 특성이 있습니다.  
결과적으로 담당자는 링크를 수집하고 내용을 읽고 정리한 뒤, 조직 내부 보고용 문서로 다시 구성하는 작업을 반복하게 됩니다.

본 프로젝트는 위 반복 작업을 자동화합니다.  
정해진 주기로 기사를 수집하고, 제목·출처·링크·발행일·요약 등 보고서 작성에 필요한 핵심 항목을 표준화된 형태로 Notion DB에 누적 저장합니다.  
운영자는 Notion의 필터와 정렬을 기반으로 사고 목록을 정리하고, 보고서 초안을 보다 빠르게 작성할 수 있습니다.

---

## 목적

본 프로젝트의 목적은 단순한 뉴스 스크랩이 아닙니다.  
침해사고 보고 및 공유에 필요한 최소 단위 데이터를 안정적으로 축적하고, 사고 대응 문서화의 효율을 높이는 데 있습니다.

특히 다음과 같은 환경에서 활용도가 높습니다.

- 보안 사고 정보를 매일 수집하고 정리해야 하는 조직
- 사고 보고서 또는 경과 공유를 위한 체계를 지속적으로 유지해야 하는 조직
- 담당자에 따라 정리 품질이 달라지는 환경

---

## 동작 방식

IncidentDashboard는 다음 절차로 동작합니다.

1) 수집 스크립트가 뉴스 데이터를 조회합니다.  
2) 기사 내용을 정제하고 필요한 정보를 추출합니다.  
3) Notion 데이터베이스에 표준화된 형태로 저장합니다.  
4) GitHub Actions가 위 과정을 일정 주기로 자동 실행합니다.

운영 환경에서 불필요한 업데이트를 최소화하기 위해, 기존 페이지를 매번 수정하지 않도록 옵션을 분리해두었습니다.  
기본 동작은 중복 생성 방지 중심이며, 이미 존재하는 페이지는 업데이트하지 않습니다.

---

## 구성 파일

- ingest_news_to_notion.py  
  뉴스 수집 및 Notion 적재 로직을 포함한 메인 스크립트

- requirements.txt  
  실행에 필요한 파이썬 의존성 목록

- .github/workflows/ingest.yml  
  30분 주기로 실행되는 GitHub Actions 워크플로우 파일

---

## 설치 및 로컬 실행

### 1) 레포지토리 클론

```bash
[git clone https://github.com/<YOUR_ID>/incidentdashboard.git](https://github.com/park1NG/incidentDashboard.git)
cd incidentdashboard
```

### 2) 가상환경 구성 및 의존성 설치

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3) 환경변수 설정

로컬 실행 환경에서는 .env 파일을 사용하는 방식이 편리합니다.
.env에는 토큰 및 API 키가 포함되므로 커밋하지 않습니다.

```bash
touch .env
```

예시:
```env
NOTION_TOKEN=secret_xxx
ARTICLES_DB_ID=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
NAVER_CLIENT_ID=xxxxxxxx
NAVER_CLIENT_SECRET=xxxxxxxx

UPDATE_EXISTING=0
DEBUG_DUMP=0
```

### 4) 실행
```bash
python ingest_news_to_notion.py
```
---

## GitHub Actions 자동 실행(30분 주기)

수동 실행이 정상 동작한다면, 동일한 설정으로 자동 실행을 운영할 수 있습니다.
자동 실행은 워크플로우의 schedule 트리거와 GitHub Secrets 설정이 필수 조건입니다.

### 1) 워크플로우 스케줄 설정 확인

.github/workflows/ingest.yml에 아래 설정이 포함되어 있어야 합니다.

```yaml
on:
  schedule:
    - cron: "*/30 * * * *"
  workflow_dispatch:
```
cron은 UTC 기준으로 동작합니다.
다만 30분 간격 실행은 지역 시간과 무관하게 동일한 주기로 운영됩니다.
GitHub Actions 특성상 실제 실행 시각이 몇 분 지연될 수 있으며 이는 정상 동작 범위입니다.

### 2) Repository Secrets 등록
GitHub 저장소에서 다음 경로로 이동하여 Secrets를 등록합니다.

Settings → Secrets and variables → Actions → New repository secret

필수 등록 항목은 다음과 같습니다.

- NOTION_TOKEN
- ARTICLES_DB_ID
- NAVER_CLIENT_ID
- NAVER_CLIENT_SECRET

---

## 실행 옵션(UPDATE_EXISTING, DEBUG_DUMP)

운영 환경에서 불필요한 변경과 API 호출을 줄이기 위해 동작 옵션을 제공합니다.

### UPDATE_EXIST
- UPDATE_EXISTING=0 (기본값)
이미 Notion에 페이지가 존재하는 경우, 해당 페이지를 업데이트하지 않습니다.
주기 실행 환경에서 불필요한 수정 이력과 API 호출을 줄이는 데 목적이 있습니다.

- UPDATE_EXISTING=1
기존 페이지도 주기적으로 갱신합니다.
분류 로직 개선 또는 데이터 정규화가 필요한 시점에 한시적으로 사용하는 것을 권장합니다.

### Notion 데이터베이스 권장 속성
Notion DB는 운영 효율을 위해 표준 속성 구조를 권장합니다.
실제 속성명은 수집 스크립트 로직과 일치해야 정상 적재가 가능합니다.

권장 예시는 다음과 같습니다.

- Title (Title)
- Source (Select)
- Link (URL)
- PublishedAt (Date)
- Summary (Rich text)
- IsIncident (Checkbox)
- AttackType (Select)
- VictimOrg (Rich text)
- Impact (Select)
- Tags (Multi-select)
- Timeline (Rich text)

중복 판별은 Link 값을 기준으로 처리하는 방식이 가장 안정적입니다.

---

## 운영 가이드

자동 수집이 활성화되면 데이터 누적 속도가 빠르게 증가합니다.
Notion에서는 운영 목적에 맞는 뷰를 구성하면 활용도가 높아집니다.

예시로는 다음이 있습니다.

- 최근 24시간 신규 적재 목록 뷰
- Impact가 High 이상인 항목만 필터링한 뷰
- 피해기업 기준 필터링 뷰
- 사고 유형(AttackType) 기준 그룹핑 뷰

UPDATE_EXISTING을 기본값(0)으로 유지하는 이유는 명확합니다.
주기 실행 시 동일 페이지가 반복 수정되면 변경 이력이 불필요하게 쌓이고, 협업 환경에서 노이즈가 발생하기 때문입니다.


