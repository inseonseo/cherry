"""
pipeline.py
역할: 서류 점검 파이프라인 로직 (app.py, main.py, function_app.py에서 호출)
"""

from preprocessor import split_pdf
from classifier import classify_document
from checker import check_document

# 고객 경계를 나타내는 서류 — 각 고객 세트의 첫 장
_ANCHOR_DOCS = {
    "위험고지문_ELS", "위험고지문_ELB", "위험고지문_DLB",
    "가입신청서_펀드", "가입신청서_단기사채", "금융상품_판매체크리스트",
}

# 분류된 서류 → 상품 prefix 매핑
_DOC_TO_PREFIX = {
    "위험고지문_ELS":  ("파생_ELS", "ELS"),
    "위험고지문_ELB":  ("파생_ELB", "ELB"),
    "위험고지문_DLB":  ("파생_DLB", "DLB"),
    "가입신청서_펀드":  ("펀드",    "펀드"),
    "가입신청서_단기사채": ("단기사채", "단기사채"),
    "금융상품_판매체크리스트": ("단기사채", "단기사채"),
    "파생결합증권_청약신청서": ("파생_ELS", "ELS"),  # 위험고지문 없을 때 폴백
}

# 고객군 판별 서류
_고령_DOCS   = {"고령투자자_상담일지", "고령투자자_판매절차확인서"}
_부적합_DOCS = {"부적합_거래확인서"}

# 다페이지 서류 보정: 첫 페이지가 잡히면 지정한 장수만큼 연속 페이지를 같은 서류로 보정
_FIXED_SPAN_DOCS = {
    "금융상품_판매체크리스트": 4,
}


def _split_customers(page_서류_맵: dict) -> list:
    """분류 결과에서 고객 경계 감지. 앵커 서류 재등장 = 새 고객."""
    customers, current = [], []
    anchor_seen = False
    prev_doc = None

    for page_num in sorted(page_서류_맵.keys()):
        서류ID = page_서류_맵[page_num]
        is_anchor = 서류ID in _ANCHOR_DOCS

        if is_anchor:
            # 같은 앵커가 연속 페이지에 반복되는 경우는 동일 고객의 다페이지 서류로 본다.
            if anchor_seen and current and prev_doc != 서류ID:
                customers.append(current)
                current = []
            anchor_seen = True

        current.append(page_num)
        prev_doc = 서류ID

    if current:
        customers.append(current)

    return customers if customers else [list(sorted(page_서류_맵.keys()))]


def _infer_규칙키(classified_docs: set, checklist: dict) -> tuple[str, str, str, str]:
    """
    분류된 서류 목록으로 상품유형·고객군 자동 감지.
    반환: (규칙키, 상품유형표시, 고객군표시, 고객군단축)
    """
    prefix, 상품표시 = "파생_ELS", "ELS"

    # 우선순위 기반 상품 추론
    # 판매체크리스트가 있으면 단기사채/채권 케이스로 우선 판단한다.
    priority_docs = [
        "금융상품_판매체크리스트",
        "가입신청서_단기사채",
        "위험고지문_ELB",
        "위험고지문_DLB",
        "위험고지문_ELS",
        "가입신청서_펀드",
    ]

    for doc_id in priority_docs:
        if doc_id in classified_docs and doc_id in _DOC_TO_PREFIX:
            pfx, 표시 = _DOC_TO_PREFIX[doc_id]
            prefix, 상품표시 = pfx, 표시
            break
    else:
        for doc_id, (pfx, 표시) in _DOC_TO_PREFIX.items():
            if doc_id in classified_docs:
                prefix, 상품표시 = pfx, 표시
                break

    if classified_docs & _고령_DOCS:
        suffix, 고객군표시, 고객군단축 = "고령", "일반금융소비자 (고령투자자)", "일반"
    elif classified_docs & _부적합_DOCS:
        suffix, 고객군표시, 고객군단축 = "부적합", "부적합투자자", "부적합"
    else:
        suffix, 고객군표시, 고객군단축 = "일반", "일반금융소비자", "일반"

    규칙키 = f"{prefix}_{suffix}"
    if 규칙키 not in checklist.get("_필요서류", {}):
        규칙키 = f"{prefix}_일반"

    return 규칙키, 상품표시, 고객군표시, 고객군단축


def _apply_fixed_span_docs(page_서류_맵: dict) -> dict:
    """
    특정 서류가 한번 감지되면 고정 장수만큼 연속 페이지를 동일 서류로 보정.
    예: 금융상품_판매체크리스트는 4페이지(시작페이지 포함)로 간주.
    """
    if not page_서류_맵:
        return page_서류_맵

    pages_sorted = sorted(page_서류_맵.keys())
    page_set = set(pages_sorted)

    for p in pages_sorted:
        sid = page_서류_맵.get(p)
        span = _FIXED_SPAN_DOCS.get(sid)
        if not span:
            continue

        # 시작 페이지가 잡히면 뒤 연속 페이지는 강제로 동일 서류로 보정
        # (오분류로 anchor가 섞여도 판매체크리스트의 연속 장수를 우선 신뢰)
        for offset in range(1, span):
            nxt = p + offset
            if nxt not in page_set:
                break

            nxt_sid = page_서류_맵.get(nxt)
            if nxt_sid != sid:
                print(f"  {nxt}페이지: '{nxt_sid}' → '{sid}'으로 고정 장수 보정")
                page_서류_맵[nxt] = sid

    return page_서류_맵


def run_batch_pipeline(pdf_source, client, classifier_agent_name, checklist,
                       progress=None):
    """
    상품유형·고객군 자동 감지 버전.
    여러 고객 서류가 합쳐진 PDF → 고객별 자동 분리·점검.
    반환: [{"고객번호": int, "페이지": [...], "상품유형": str, "고객군": str, "결과": {...}}, ...]
    """
    def _p(stage, detail="", page_map=None):
        if progress:
            progress(stage, detail, page_map)

    _p("preprocessing", "PDF 분리 중")
    pages = split_pdf(pdf_source)
    total = len(pages)

    서류키_목록 = [
        {"서류ID": k, "서류명": v["서류명"], "판별키워드": v.get("판별키워드", [])}
        for k, v in checklist.items()
        if isinstance(v, dict) and "서류명" in v
    ]

    # 전체 페이지 분류
    _p("classifying", f"0/{total} 페이지")
    page_서류_맵 = {}
    for page_info in pages:
        n = page_info["page_num"]
        서류ID = classify_document(client, classifier_agent_name,
                                   page_info["image"], 서류키_목록)
        print(f"  {n}페이지 → {서류ID}")
        page_서류_맵[n] = 서류ID
        # 진행중 화면에는 분류 보정이 반영된 상태만 노출
        page_서류_맵 = _apply_fixed_span_docs(page_서류_맵)
        _p("classifying", f"{n}/{total} 페이지 분류 중", dict(page_서류_맵))

    # 다페이지 고정 장수 서류 보정(예: 판매체크리스트 4페이지)
    page_서류_맵 = _apply_fixed_span_docs(page_서류_맵)
    _p("classifying", f"{total}/{total} 완료", dict(page_서류_맵))

    # 고객 경계 감지
    customer_groups = _split_customers(page_서류_맵)
    total_customers = len(customer_groups)
    print(f"[배치] {total_customers}명 감지, 그룹: {customer_groups}")
    _p("classifying", f"{total_customers}명 고객 감지됨", dict(page_서류_맵))

    # 고객별 점검
    batch_results = []

    for idx, page_nums in enumerate(customer_groups):
        _p("checking", f"고객 {idx+1}/{total_customers} 점검 중")

        고객_맵 = {n: page_서류_맵[n] for n in page_nums}
        classified_docs = {sid for sid in 고객_맵.values() if sid != "unknown"}

        # 상품유형·고객군 자동 감지
        규칙키, 상품표시, 고객군표시, 고객군단축 = _infer_규칙키(classified_docs, checklist)
        필요서류_set = set(checklist.get("_필요서류", {}).get(규칙키, []))

        print(f"  [고객 {idx+1}] 감지: {상품표시} / {고객군표시} → 규칙키={규칙키}")

        # 연속 페이지 병합: unknown이거나 필요서류에 없는 오분류 페이지는 직전 유효 서류로 귀속
        if 필요서류_set:
            prev_valid = None
            sorted_pages = sorted(고객_맵.keys())
            for idx2, n in enumerate(sorted_pages):
                sid = 고객_맵[n]
                if sid in 필요서류_set:
                    prev_valid = sid
                elif sid == "unknown":
                    if prev_valid is not None:
                        print(f"  {n}페이지: '{sid}' → '{prev_valid}'으로 병합")
                        고객_맵[n] = prev_valid
                    else:
                        # 앞쪽 단서가 없으면, 뒤쪽 최초 유효 서류로 복원 시도
                        next_valid = None
                        for nn in sorted_pages[idx2 + 1:]:
                            nsid = 고객_맵[nn]
                            if nsid in 필요서류_set:
                                next_valid = nsid
                                break
                        if next_valid is not None:
                            print(f"  {n}페이지: '{sid}' → '{next_valid}'으로 전방 복원")
                            고객_맵[n] = next_valid
                            prev_valid = next_valid
                elif prev_valid is not None and sid not in 필요서류_set:
                    print(f"  {n}페이지: '{sid}' → '{prev_valid}'으로 병합")
                    고객_맵[n] = prev_valid

        # 병합 결과를 전역 page_map에 반영해 라이브 표시 갱신
        page_서류_맵.update(고객_맵)
        _p("checking", f"고객 {idx+1}/{total_customers} 점검 중", dict(page_서류_맵))

        서류별_페이지 = {}
        for n, sid in 고객_맵.items():
            if sid != "unknown":
                서류별_페이지.setdefault(sid, []).append(n)

        누락서류 = [s for s in checklist.get("_필요서류", {}).get(규칙키, [])
                   if s not in 서류별_페이지]

        # 고령투자자 감지 시 필수 보완서류 확인
        보완서류 = []
        is_elderly = "고령" in 고객군표시
        if is_elderly:
            for doc_id in _고령_DOCS:
                if doc_id not in 서류별_페이지:
                    보완서류.append(doc_id)

        전체_결과 = {}
        for 서류ID, p_nums in 서류별_페이지.items():
            서류_규칙 = checklist.get(서류ID)
            if not 서류_규칙 or not isinstance(서류_규칙, dict):
                continue
            이미지들 = [pages[p - 1]["image"] for p in p_nums if p <= len(pages)]
            결과 = check_document(이미지들, 서류_규칙, {"고객군": 고객군단축})
            전체_결과[서류ID] = 결과

        batch_results.append({
            "고객번호": idx + 1,
            "페이지": page_nums,
            "상품유형": 상품표시,
            "고객군": 고객군표시,
            "고령투자자": "고령" in 고객군표시,
            "결과": {
                "누락서류": 누락서류,
                "보완서류": 보완서류,
                "전체_결과": 전체_결과,
                "적용규칙": 규칙키,
                "서류별_페이지": 서류별_페이지,
            }
        })

    _p("reporting", "보고서 생성 중")
    return batch_results


def run_pipeline(pdf_source, 상품유형, 고객군, 고령투자자,
                 client, classifier_agent_name, checklist,
                 progress=None):
    """
    pdf_source:             파일 경로(str) 또는 PDF 바이트(bytes)
    client:                 AIProjectClient
    classifier_agent_name:  Foundry에 배포된 분류기 에이전트 이름
    checklist:              checklist.json dict
    progress:               진행상황 콜백 progress(stage, detail)

    반환: {누락서류, 전체_결과, 적용규칙_키, 서류별_페이지}
    """
    def _p(stage, detail="", page_map=None):
        if progress:
            progress(stage, detail, page_map)

    # 1단계: PDF 페이지 분리
    _p("preprocessing", "PDF 분리 중")
    pages = split_pdf(pdf_source)

    # 2단계: 서류 판별 (분류기 Agent)
    total = len(pages)
    _p("classifying", f"0/{total} 페이지")
    page_서류_맵 = {}

    서류키_목록 = [
        {"서류ID": k, "서류명": v["서류명"], "판별키워드": v.get("판별키워드", [])}
        for k, v in checklist.items()
        if isinstance(v, dict) and "서류명" in v
    ]

    for page_info in pages:
        n = page_info["page_num"]
        서류ID = classify_document(client, classifier_agent_name,
                                   page_info["image"], 서류키_목록)
        서류ID = _resolve_doc_id(서류ID, 상품유형)
        print(f"  {n}페이지 → {서류ID}")
        page_서류_맵[n] = 서류ID
        # 진행중 화면에는 분류 보정이 반영된 상태만 노출
        page_서류_맵 = _apply_fixed_span_docs(page_서류_맵)
        _p("classifying", f"{n}/{total} 페이지 분류 중", dict(page_서류_맵))

    # 다페이지 고정 장수 서류 보정(예: 판매체크리스트 4페이지)
    page_서류_맵 = _apply_fixed_span_docs(page_서류_맵)
    _p("classifying", f"{total}/{total} 완료", dict(page_서류_맵))

    # 연속 페이지 병합: 상품유형의 필요서류에 없는 서류로 분류된 페이지를
    # 직전 유효 서류에 귀속 (예: 가입신청서 뒷면이 파생결합증권_청약신청서로 오분류)
    _규칙_키_tmp = _resolve_필요서류_키(상품유형, 고객군, 고령투자자)
    _필요서류_set = set(checklist.get("_필요서류", {}).get(_규칙_키_tmp, []))
    if _필요서류_set:
        prev_valid = None
        for n in sorted(page_서류_맵.keys()):
            sid = page_서류_맵[n]
            if sid in _필요서류_set:
                prev_valid = sid
            elif sid != "unknown" and prev_valid is not None:
                print(f"  {n}페이지: '{sid}' → '{prev_valid}'으로 병합 (필요서류 불일치)")
                page_서류_맵[n] = prev_valid

    서류별_페이지 = {}
    for page_num, 서류ID in page_서류_맵.items():
        if 서류ID == "unknown":
            continue
        서류별_페이지.setdefault(서류ID, []).append(page_num)

    # 3단계: 필요서류 충족 여부
    적용규칙_키 = _resolve_필요서류_키(상품유형, 고객군, 고령투자자)
    필요서류_목록 = checklist.get("_필요서류", {}).get(적용규칙_키, []) if 적용규칙_키 else []

    누락서류 = [s for s in 필요서류_목록 if s not in 서류별_페이지]

    # 4단계: 서류별 체크박스/필기 점검 (GPT-4o Vision)
    docs = list(서류별_페이지.items())
    전체_결과 = {}
    고객군_단축 = _단축_고객군(고객군)

    for idx, (서류ID, page_nums) in enumerate(docs):
        서류_규칙 = checklist.get(서류ID)
        if not 서류_규칙 or not isinstance(서류_규칙, dict):
            continue
        서류명 = 서류_규칙.get("서류명", 서류ID)
        _p("checking", f"{idx+1}/{len(docs)} {서류명}")

        이미지들 = [pages[p - 1]["image"] for p in page_nums if p <= len(pages)]
        결과 = check_document(이미지들, 서류_규칙, {"고객군": 고객군_단축})
        전체_결과[서류ID] = 결과

    _p("reporting", "보고서 생성 중")

    return {
        "누락서류": 누락서류,
        "전체_결과": 전체_결과,
        "적용규칙": 적용규칙_키,
        "서류별_페이지": 서류별_페이지,
    }
