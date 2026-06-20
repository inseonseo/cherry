#!/usr/bin/env python3
"""
setup_previews.py

docs/PNG/*.png → static/previews/{서류ID}_p{n}.png (리사이즈, 최대 1500px)
static/annotations/*.json (VIA 형식) → pages/fields 형식으로 변환
  - 좌표 기준: docs/PNG 원본 크기 사용 → 정확한 0-1 비율 계산

실행: python3 setup_previews.py
"""

import json
import pathlib
from PIL import Image

DOCS_PNG   = pathlib.Path("docs/PNG")
PREVIEWS   = pathlib.Path("static/previews")
ANNOTATIONS = pathlib.Path("static/annotations")
MAX_WIDTH  = 1500  # preview 최대 가로 픽셀

# ── docs/PNG 파일명 → (서류ID, 페이지번호) ─────────────────────────────────
PNG_MAP = {
    "파생결합증권(ELS) 위험고지문.png":                          ("위험고지문_ELS",            1),
    "주가연계파생결합사채(ELB) 위험고지문.png":                   ("위험고지문_ELB",            1),
    "기타파생결합사채(DLB) 위험고지문.png":                       ("위험고지문_DLB",            1),
    "파생결합증권_청약신청서.png":                                ("파생결합증권_청약신청서",     1),
    "설명서교부 및 금융상품 가입확인서.png":                       ("설명서교부_가입확인서",       1),
    "현재 투자자금성향 정보 확인서[투자성상품].png":               ("투자자금성향_정보확인서",     1),
    "투자자정보 확인서[투자성상품]_1.png":                        ("투자자정보확인서",           1),
    "투자자정보 확인서[투자성상품]_2.png":                        ("투자자정보확인서",           2),
    "금융투자상품 가입신청서_펀드전문_1.png":                     ("가입신청서_펀드_전문",        1),
    "금융투자상품 가입신청서_펀드전문_2.png":                     ("가입신청서_펀드_전문",        2),
    "고령투자자보호를 위한 강화된 판매 절차 확인서_1.png":         ("고령투자자_판매절차확인서",   1),
    "고령투자자보호를 위한 강화된 판매 절차 확인서_2.png":         ("고령투자자_판매절차확인서",   2),
    "고령투자자보호를 위한 강화된 판매 절차 확인서_3.png":         ("고령투자자_판매절차확인서",   3),
}

# ── static/annotations VIA 파일명 → 서류ID ────────────────────────────────
ANN_MAP = {
    "파생결합증권(ELS) 위험고지문.json":              "위험고지문_ELS",
    "주가연계파생결합사채(ELB) 위험고지문.json":       "위험고지문_ELB",
    "기타파생결합사채(DLB) 위험고지문.json":           "위험고지문_DLB",
    "설명서교부 및 금융상품 가입확인서.json":           "설명서교부_가입확인서",
    "현재 투자자금성향 정보 확인서(투자성상품).json":   "투자자금성향_정보확인서",
    # _1/_2 스타일로 올린 경우도 자동 처리 (아래 와일드카드 스캔에서 처리)
}

FIELD_COLORS = {"자필기재": "#E53935", "서명": "#2E7D32", "날짜": "#1565C0", "종목명": "#E53935"}

def get_color(name: str) -> str:
    for k, v in FIELD_COLORS.items():
        if k in name:
            return v
    return "#E53935"


PREVIEWS.mkdir(parents=True, exist_ok=True)
orig_sizes: dict[tuple, tuple] = {}  # (서류ID, page) → (orig_w, orig_h)

# ── STEP 1: docs/PNG → static/previews 리사이즈 ───────────────────────────
print("=" * 60)
print("STEP 1: PNG 리사이즈 → static/previews/")
print("=" * 60)

for fname, (doc_id, page_num) in PNG_MAP.items():
    src = DOCS_PNG / fname
    if not src.exists():
        print(f"  SKIP (없음): {fname}")
        continue

    dest = PREVIEWS / f"{doc_id}_p{page_num}.png"

    with Image.open(src) as img:
        orig_w, orig_h = img.size
        orig_sizes[(doc_id, page_num)] = (orig_w, orig_h)

        if orig_w > MAX_WIDTH:
            ratio = MAX_WIDTH / orig_w
            new_size = (MAX_WIDTH, int(orig_h * ratio))
            resized = img.resize(new_size, Image.LANCZOS)
        else:
            resized = img.copy()
            new_size = (orig_w, orig_h)

        resized.save(dest, "PNG")

    print(f"  OK  {fname}")
    print(f"      → {dest.name}  ({orig_w}×{orig_h} → {new_size[0]}×{new_size[1]})")


# ── STEP 2: VIA annotation → pages/fields 형식 변환 ──────────────────────
print()
print("=" * 60)
print("STEP 2: VIA annotation → pages/fields 변환")
print("=" * 60)

def via_to_pages(raw: dict, doc_id: str, orig_sizes: dict) -> dict | None:
    """VIA 프로젝트 JSON → pages/fields 형식."""
    via_meta = raw.get("_via_img_metadata") or {
        k: v for k, v in raw.items() if isinstance(v, dict) and "regions" in v
    }
    if not via_meta:
        return None

    pages_out = []
    page_num = 0

    for img_key, img_info in via_meta.items():
        regions = img_info.get("regions", [])
        if not regions:
            continue  # regions 없는 항목은 페이지 번호 증가 없이 스킵
        page_num += 1

        orig_w, orig_h = orig_sizes.get((doc_id, page_num), (0, 0))

        # fallback: 직접 원본 PNG 파일 크기 읽기
        if not orig_w:
            for fname, (did, pn) in PNG_MAP.items():
                if did == doc_id and pn == page_num:
                    src = DOCS_PNG / fname
                    if src.exists():
                        with Image.open(src) as img:
                            orig_w, orig_h = img.size
                    break

        if not orig_w:
            print(f"    WARNING: {doc_id} p{page_num} 원본 크기 불명 — 좌표 보정 불가")

        preview_path = f"/static/previews/{doc_id}_p{page_num}.png"
        fields = []

        for r in regions:
            sa = r.get("shape_attributes", {})
            ra = r.get("region_attributes", {})
            if sa.get("name") != "rect":
                continue

            px = sa.get("x", 0)
            py = sa.get("y", 0)
            pw = sa.get("width", 0)
            ph = sa.get("height", 0)

            # 라벨: region_attributes 에서 비어있지 않은 첫 값
            label = ""
            for v in ra.values():
                if isinstance(v, str) and v.strip():
                    label = v.strip(); break
            if not label:
                label = list(ra.keys())[0] if ra else "항목"

            if orig_w and orig_h:
                fx = px / orig_w
                fy = py / orig_h
                fw = pw / orig_w
                fh = ph / orig_h
            else:
                fx, fy, fw, fh = px, py, pw, ph

            fields.append({
                "항목ID": label,
                "설명": label,
                "color": get_color(label),
                "x": round(max(0.0, min(0.99, fx)), 4),
                "y": round(max(0.0, min(0.99, fy)), 4),
                "w": round(max(0.005, min(1.0, fw)), 4),
                "h": round(max(0.002, min(1.0, fh)), 4),
            })

        if fields:
            pages_out.append({"page": page_num, "image": preview_path, "fields": fields})

        page_num += 1

    if not pages_out:
        return None

    return {"서류ID": doc_id, "서류명": doc_id, "pages": pages_out}


for ann_fname, doc_id in ANN_MAP.items():
    src = ANNOTATIONS / ann_fname
    dest = ANNOTATIONS / f"{doc_id}.json"

    if not src.exists():
        print(f"  SKIP (없음): {ann_fname}")
        continue

    with open(src, encoding="utf-8") as f:
        raw = json.load(f)

    # 이미 pages/fields 형식이면 원본 크기로 좌표 재보정
    if "pages" in raw:
        # 원본 크기 기반으로 새로 정규화
        print(f"  이미 pages/fields: {ann_fname} → 좌표 재보정")

    result = via_to_pages(raw, doc_id, orig_sizes)
    if not result:
        print(f"  SKIP (regions 없음): {ann_fname}")
        continue

    with open(dest, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    total_fields = sum(len(p["fields"]) for p in result["pages"])
    print(f"  OK  {ann_fname}")
    print(f"      → {dest.name}  ({len(result['pages'])}페이지, {total_fields}개 필드)")


# ── STEP 3: 새로 올린 _1/_2 스타일 파일 자동 처리 ─────────────────────────
print()
print("=" * 60)
print("STEP 3: 미처리 파일 스캔 (_1/_2 스타일)")
print("=" * 60)

import re
processed = set(ANN_MAP.keys())

for ann_file in sorted(ANNOTATIONS.glob("*.json")):
    if ann_file.name in processed:
        continue

    with open(ann_file, encoding="utf-8") as f:
        try:
            raw = json.load(f)
        except Exception:
            continue

    if "pages" in raw:
        continue  # 이미 변환됨

    is_via = "_via_img_metadata" in raw or any(
        isinstance(v, dict) and "regions" in v for v in raw.values()
    )
    if not is_via:
        continue

    # 파일명에서 서류ID 추정 (_1, _2 suffix 제거)
    stem = re.sub(r"[_\s]*[12]$", "", ann_file.stem).strip()
    # 대응하는 PNG 원본 탐색
    candidates = list(DOCS_PNG.glob(f"{stem}*.png"))
    if not candidates:
        print(f"  SKIP (원본 PNG 없음): {ann_file.name}")
        continue

    orig_w, orig_h = 0, 0
    with Image.open(candidates[0]) as img:
        orig_w, orig_h = img.size

    page_num = 1
    preview_path = f"/static/previews/{stem}_p1.png"
    via_meta = raw.get("_via_img_metadata") or {
        k: v for k, v in raw.items() if isinstance(v, dict) and "regions" in v
    }

    fields = []
    for img_key, img_info in via_meta.items():
        for r in (img_info.get("regions") or []):
            sa = r.get("shape_attributes", {})
            ra = r.get("region_attributes", {})
            if sa.get("name") != "rect":
                continue
            label = next((v for v in ra.values() if isinstance(v, str) and v), list(ra.keys())[0] if ra else "항목")
            fields.append({
                "항목ID": label, "설명": label, "color": get_color(label),
                "x": round(max(0.0, min(0.99, sa.get("x", 0) / orig_w)), 4) if orig_w else 0,
                "y": round(max(0.0, min(0.99, sa.get("y", 0) / orig_h)), 4) if orig_h else 0,
                "w": round(max(0.005, min(1.0, sa.get("width", 0) / orig_w)), 4) if orig_w else 0,
                "h": round(max(0.002, min(1.0, sa.get("height", 0) / orig_h)), 4) if orig_h else 0,
            })

    if fields:
        result = {"서류ID": stem, "서류명": stem, "pages": [{"page": 1, "image": preview_path, "fields": fields}]}
        out_path = ANNOTATIONS / f"{stem}.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"  OK  {ann_file.name} → {out_path.name} ({len(fields)}개 필드)")


print()
print("완료! 서버를 재시작하면 반영됩니다.")
