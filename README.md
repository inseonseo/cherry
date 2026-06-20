# CHERRY
**Check Human Error, Review & Report with You**

금융투자상품 판매서류 자동점검 시스템

## 개요

영업점 상담 준비부터 금융소비자보호부 완전판매 검증까지 판매 프로세스 전 구간을 지원하는 AI 기반 자동점검 시스템입니다.

- **PDF 점검:** GPT-4o Vision + Azure Document Intelligence로 서류 분류 및 기재 여부 자동 판단
- **실시간 가이드:** AI 상담 도우미가 채팅/음성으로 필수 서류 및 작성 가이드 제공
- **오류 시각화:** VIA 좌표 기반 오류 위치 자동 표시
- **배치 처리:** 다중 고객 서류 묶음 자동 분리 및 고객별 점검 결과 생성

---

## 기술 스택

### Backend
- **Flask** (Python) — REST API 서버, Gunicorn으로 Azure App Service 배포
- **Azure OpenAI** — GPT-4o Vision (서류 분류), gpt-4o-transcribe (음성 인식)
- **Azure Document Intelligence** — 텍스트/체크박스 추출
- **Azure Cosmos DB** — 점검 결과 저장 및 대시보드 통계

### Frontend
- **Vanilla JavaScript** — 동적 UI, 실시간 진행 상황 표시
- **HTML/CSS** — 반응형 레이아웃

### Deployment
- **Azure App Service** — Linux (Python 3.11)
- **Blob Storage** — 원본 PDF 처리 중에만 메모리 저장 (즉시 삭제)

---

## 아키텍처

```
사용자 입력 (PDF 업로드)
    ↓
[Preprocessor] PDF → 페이지 이미지
    ↓
[Classifier] GPT-4o Vision → 서류ID 분류
    ↓
[Pipeline] 앵커 서류 기반 고객 경계 분리
    ↓
[Checker] DI 텍스트 추출 → GPT-4o 기재 여부 판단
    ↓
[Report] 결과 PDF 생성
    ↓
[Dashboard] Cosmos DB 통계 집계
```

### 핵심 설계 원칙

1. **상태 비저장:** 원본 PDF는 메모리에만 존재 → 처리 후 즉시 삭제
2. **PII 보호:** Checker 단계에서 이미지 대신 DI 추출 텍스트만 GPT-4o 전달
3. **보수적 판단:** 기재 여부 불명확 시 미기재로 처리 → False Negative 방지
4. **다중 검증:** 단일 AI 판단 X → DI 좌표로 재검증 레이어 추가

---

## 기능 상세

### 1. PDF 자동분류 (Classifier)
- **GPT-4o Vision** 페이지별 분류
- 판매체크리스트 오탐 대응: 2차 fallback 검증
- 결과: `page_num → 서류ID` 매핑

### 2. 고객 경계 감지 (Pipeline)
- **앵커 서류** (위험고지문, 가입신청서) 재등장 = 새 고객
- 판매체크리스트 4페이지 고정 스팬 보정
- 결과: 고객별 서류 세트 분리

### 3. 서류 점검 (Checker)
- **2단계:**
  1. Azure DI → 텍스트 + 체크박스 상태 추출
  2. GPT-4o (텍스트 모드) → 기재 여부 판단
- **규칙 적용:** 필수/선택, 조건부, 중복 선택 오류 탐지
- 결과: 오류 항목 + 위치 좌표

### 4. AI 상담 (Guide Agent)
- **Assistants API + Tool Calling:**
  - `get_required_documents()` — 상품/고객군별 필수 서류
  - `get_document_checklist()` — 서류 상세 항목
- **음성 지원:**
  - 실시간 녹음: 최대 10분
  - 파일 업로드: 최대 30분
  - gpt-4o-transcribe로 한국어 금융 용어 인식

### 5. 대시보드
- 최근 60건 점검 이력
- 총 고객수, 통과율, TOP 5 누락 서류
- Cosmos DB 단일 쿼리로 통합 집계

---

## 설치 및 실행

### 로컬 실행
```bash
pip install -r requirements.txt
python app.py
```
http://localhost:5001 에서 접속

### 환경 변수 (.env)
```
AZURE_OPENAI_ENDPOINT=https://xxx.openai.azure.com/
AZURE_OPENAI_KEY=xxx
AZURE_OPENAI_DEPLOYMENT=gpt-4o
AZURE_TRANSCRIBE_DEPLOYMENT=gpt-4o-transcribe
AZURE_DI_ENDPOINT=https://xxx.cognitiveservices.azure.com/
AZURE_DI_KEY=xxx
COSMOS_ENDPOINT=https://xxx.documents.azure.com:443/
COSMOS_KEY=xxx
```

---

## 포인트

### 성과
- ✅ 정형 서류를 AI + DI 하이브리드로 분류 → 정확도 높음
- ✅ PII 미저장 구조 → 금융 컴플라이언스 대응
- ✅ 배치 처리로 다중 고객 자동 분리
- ✅ 실시간 음성 가이드로 영업 현장 편의성

### 개선 포인트
- 분류 신뢰도 스코어 추가 예정
- 불확실한 항목 사람 검토 플래그 추가
- Durable Functions로 서버리스 아키텍처 전환

---

## 라이센스
내부 프로젝트
