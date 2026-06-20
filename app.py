"""
app.py — Flask 웹 UI 진입점
실행: python app.py
"""

import io
import json
import os
import re
import threading
import traceback
import uuid
import pathlib

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, jsonify, render_template, request, send_file, send_from_directory

from pipeline import run_pipeline, run_batch_pipeline
from report import generate_report
from guide_agent import get_or_create_guide_agent, create_session, chat, voice_chunk
from db import save_run, get_dashboard, get_run_detail

app = Flask(__name__)

# ── 설정 ─────────────────────────────────────────────────────
CLASSIFIER_AGENT_NAME = os.environ.get("CLASSIFIER_AGENT_NAME", "docfine-classifier")

with open("checklist.json", "r", encoding="utf-8-sig") as f:
    _checklist = json.load(f)

_client = None  # guide_agent 함수 시그니처 호환용 (실제 미사용)

# ── 인메모리 작업 저장소 ──────────────────────────────────────
_jobs: dict = {}


_ANN_DIR = pathlib.Path("static/annotations")


def _normalize_doc_key(name: str) -> str:
    s = (name or "").strip().lower()
    # 파일명 오타(예: .jason)도 최대한 흡수
    s = s.replace(".json", "").replace(".jason", "")
    return re.sub(r"[^0-9a-z가-힣]", "", s)


def _resolve_annotation_path(doc_id: str) -> pathlib.Path | None:
    """doc_id/서류명을 annotation 파일명으로 유연하게 매칭."""
    direct = _ANN_DIR / f"{doc_id}.json"
    if direct.exists():
        return direct

    candidates = [doc_id]
    rule = _checklist.get(doc_id)
    if isinstance(rule, dict):
        if rule.get("서류명"):
            candidates.append(rule["서류명"])
        for kw in rule.get("판별키워드", []):
            if kw:
                candidates.append(kw)

    # 공백/구분자 변형도 후보에 추가
    expanded = []
    for c in candidates:
        expanded.append(c)
        expanded.append(c.replace("_", " "))
        expanded.append(c.replace("/", " "))
    candidates = expanded

    files = [p for p in _ANN_DIR.glob("*.json") if p.is_file()]

    # 1) 정확 stem 매칭
    stems = {p.stem: p for p in files}
    for c in candidates:
        if c in stems:
            return stems[c]

    # 2) 정규화 키 매칭
    norm_to_path = {_normalize_doc_key(p.stem): p for p in files}
    for c in candidates:
        n = _normalize_doc_key(c)
        if n in norm_to_path:
            return norm_to_path[n]

    return None


def _list_annotation_docs() -> list[dict]:
    """체크리스트 기준으로 실제 annotation 파일이 존재하는 서류 목록 반환."""
    items = []
    for doc_id, rule in _checklist.items():
        if not isinstance(rule, dict):
            continue
        doc_name = rule.get("서류명", doc_id)
        ann_path = _resolve_annotation_path(doc_id)
        if ann_path:
            items.append({"doc_id": doc_id, "doc_name": doc_name})

    # 화면 표시 안정성을 위해 서류명 기준 정렬
    items.sort(key=lambda x: x["doc_name"])
    return items


def _run_job(job_id, pdf_bytes):
    def progress(stage, detail="", page_map=None):
        _jobs[job_id]["stage"] = stage
        _jobs[job_id]["detail"] = detail
        if page_map is not None:
            _jobs[job_id]["page_map"] = page_map

    try:
        batch = run_batch_pipeline(
            pdf_source=pdf_bytes,
            client=None,
            classifier_agent_name=CLASSIFIER_AGENT_NAME,
            checklist=_checklist,
            progress=progress,
        )
        progress("reporting", "보고서 생성 중")

        reports = []
        for item in batch:
            결과 = item["결과"]
            report_pdf = generate_report(
                **결과,
                checklist=_checklist,
                상품유형=item["상품유형"],
                고객군=item["고객군"],
                고령투자자=item["고령투자자"],
            )
            reports.append({
                "고객번호": item["고객번호"],
                "report": report_pdf,
                "결과": 결과,
                "상품유형": item["상품유형"],
                "고객군": item["고객군"],
                "고령투자자": item["고령투자자"],
            })

        _jobs[job_id].update({"status": "done", "stage": "done", "batch": reports})

        # Cosmos DB에 결과 저장 (실패해도 job 결과에는 영향 없음)
        try:
            total_pages = sum(len(item.get("페이지", [])) for item in batch)
            run_id = save_run(batch, total_pages)
            if run_id:
                _jobs[job_id]["run_id"] = run_id
        except Exception as db_err:
            print(f"[DB SAVE ERROR] {db_err}")
    except Exception as e:
        tb = traceback.format_exc()
        print(f"[JOB ERROR] {e}\n{tb}")
        _jobs[job_id].update({"status": "error", "error": str(e), "traceback": tb})


# ── 라우트 ────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/checklist")
def api_checklist():
    return jsonify(_checklist)


@app.route("/api/check", methods=["POST"])
def api_check():
    pdf_file = request.files.get("pdf")
    if not pdf_file:
        return jsonify({"error": "PDF 파일이 없습니다"}), 400

    pdf_bytes = pdf_file.read()
    job_id = str(uuid.uuid4())
    _jobs[job_id] = {"status": "running", "stage": "preprocessing", "detail": "시작 중", "pdf_bytes": pdf_bytes}

    t = threading.Thread(target=_run_job, args=(job_id, pdf_bytes), daemon=True)
    t.start()
    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def api_status(job_id):
    job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": "없는 작업"}), 404
    return jsonify({
        "status": job["status"],
        "stage":  job.get("stage", ""),
        "detail": job.get("detail", ""),
        "total_customers": len(job.get("batch") or []),
        "error":  job.get("error", ""),
        "page_map": job.get("page_map", {}),
    })


@app.route("/api/page/<job_id>/<int:page_num>")
def api_page(job_id, page_num):
    job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": "없는 작업"}), 404
    pdf_bytes = job.get("pdf_bytes")
    if not pdf_bytes:
        return jsonify({"error": "원본 없음"}), 404

    cache = job.setdefault("page_cache", {})
    if page_num not in cache:
        from preprocessor import split_pdf
        for p in split_pdf(pdf_bytes):
            buf = io.BytesIO()
            p["image"].save(buf, format="PNG")
            cache[p["page_num"]] = buf.getvalue()

    img_bytes = cache.get(page_num)
    if not img_bytes:
        # split_pdf 경로에서 특정 페이지가 누락되면 원본 PDF에서 직접 렌더링 폴백
        try:
            import fitz

            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            if 1 <= page_num <= len(doc):
                page = doc[page_num - 1]
                pix = page.get_pixmap(matrix=fitz.Matrix(150 / 72, 150 / 72))
                img_bytes = pix.tobytes("png")
                cache[page_num] = img_bytes
            else:
                return jsonify({"error": "페이지 없음"}), 404
        except Exception as e:
            print(f"[api_page] 렌더링 폴백 실패: page={page_num}, err={e}")
            return jsonify({"error": "페이지 렌더링 실패"}), 500

    if not img_bytes:
        return jsonify({"error": "페이지 없음"}), 404
    return send_file(io.BytesIO(img_bytes), mimetype="image/png")


@app.route("/api/result/<job_id>")
def api_result(job_id):
    job = _jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "결과 없음"}), 404

    batch = job["batch"]
    if len(batch) == 1:
        r = batch[0]
        결과 = r["결과"]
        return jsonify({
            "누락서류":     결과["누락서류"],
            "보완서류":     결과.get("보완서류", []),
            "전체_결과":    결과["전체_결과"],
            "적용규칙":     결과["적용규칙"],
            "서류별_페이지": 결과["서류별_페이지"],
            "상품유형":     r["상품유형"],
            "고객군":       r["고객군"],
            "고령투자자":   r["고령투자자"],
            "총_고객수":    1,
        })
    else:
        return jsonify({
            "총_고객수": len(batch),
            "고객별_결과": [
                {
                    "고객번호":  r["고객번호"],
                    "상품유형":  r["상품유형"],
                    "고객군":    r["고객군"],
                    "누락서류":  r["결과"]["누락서류"],
                    "보완서류":  r["결과"].get("보완서류", []),
                    "pass": all(v.get("pass") for v in r["결과"]["전체_결과"].values()),
                }
                for r in batch
            ],
        })


@app.route("/api/report/<job_id>")
def api_report(job_id):
    import zipfile
    job = _jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "보고서 없음"}), 404

    batch = job["batch"]
    if len(batch) == 1:
        # 단건: PDF 바로 반환
        return send_file(
            io.BytesIO(batch[0]["report"]),
            mimetype="application/pdf",
            as_attachment=True,
            download_name="점검결과.pdf",
        )
    else:
        # 다건: ZIP으로 묶어서 반환
        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for r in batch:
                zf.writestr(f"고객{r['고객번호']:03d}_점검결과.pdf", r["report"])
        zip_buf.seek(0)
        return send_file(
            zip_buf,
            mimetype="application/zip",
            as_attachment=True,
            download_name="배치점검결과.zip",
        )


# ── 사전점검 Guide Agent ──────────────────────────────────────────────────
_GUIDE_AGENT_ID: str | None = None


def _get_guide_agent_id() -> str:
    global _GUIDE_AGENT_ID
    if _GUIDE_AGENT_ID is None:
        _GUIDE_AGENT_ID = get_or_create_guide_agent(_client)
    return _GUIDE_AGENT_ID


@app.route("/api/guide/session", methods=["POST"])
def api_guide_session():
    try:
        agent_id = _get_guide_agent_id()
        thread_id = create_session(_client)
        return jsonify({"session_id": thread_id, "agent_id": agent_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/guide/chat", methods=["POST"])
def api_guide_chat():
    data = request.get_json()
    session_id = data.get("session_id", "")
    message = data.get("message", "").strip()
    if not session_id or not message:
        return jsonify({"error": "session_id와 message 필요"}), 400
    try:
        reply = chat(_client, session_id, _get_guide_agent_id(), message)
        return jsonify({"reply": reply})
    except Exception as e:
        tb = traceback.format_exc()
        print(f"[GUIDE CHAT ERROR] {e}\n{tb}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/guide/voice-chunk", methods=["POST"])
def api_guide_voice_chunk():
    session_id = request.form.get("session_id", "")
    audio_file = request.files.get("audio")
    if not session_id or not audio_file:
        return jsonify({"error": "session_id와 audio 필요"}), 400
    try:
        audio_bytes = audio_file.read()
        mime_type = audio_file.content_type or "audio/webm"
        result = voice_chunk(_client, session_id, _get_guide_agent_id(), audio_bytes, mime_type)
        return jsonify(result)
    except Exception as e:
        tb = traceback.format_exc()
        print(f"[GUIDE VOICE ERROR] {e}\n{tb}")
        return jsonify({"error": str(e)}), 500



@app.route("/api/dashboard")
def api_dashboard():
    try:
        return jsonify(get_dashboard())
    except Exception as e:
        tb = traceback.format_exc()
        print(f"[DASHBOARD ERROR] {e}\n{tb}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/dashboard/run/<run_id>")
def api_dashboard_run(run_id):
    detail = get_run_detail(run_id)
    if not detail:
        return jsonify({"error": "없음"}), 404
    return jsonify(detail)


def _load_annotation(path: pathlib.Path) -> dict | None:
    """
    annotation JSON 로드 후 좌표 자동 정규화.

    지원하는 입력 형식:
      1) 커스텀 pages/fields 형식  – 좌표가 0-1 비율이어야 하지만
         inches(300 DPI) 또는 픽셀/원본폭으로 저장된 경우가 있어 자동 보정.
      2) VIA(VGG Image Annotator) 프로젝트 형식 (_via_img_metadata 키 포함)
         → pages/fields 형식으로 변환 후 반환.
      3) 구형 단일-key VIA 형식 ({"파일명_크기": {..., "regions": ...}} 구조)
         → 위와 동일하게 변환.
    """
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return None

    # ── 1) 커스텀 pages/fields 형식 ─────────────────────────────
    if "pages" in raw:
        return _fix_pages_coords(raw)

    # ── 2) VIA 프로젝트 형식 ────────────────────────────────────
    via_meta = raw.get("_via_img_metadata") or {}
    if not via_meta:
        # 구형 단일-key 형식: 최상위 키가 "filename_size" 패턴
        via_meta = {k: v for k, v in raw.items()
                    if isinstance(v, dict) and "regions" in v}

    if not via_meta:
        return None

    doc_id_guess = path.stem  # 파일명으로 서류ID 추정
    pages_out = []
    # 이미지 진입 순서대로 pages 생성 (regions 있는 것만)
    for img_key, img_info in via_meta.items():
        regions = img_info.get("regions", [])
        filename = img_info.get("filename", "")
        # preview 이미지 경로 추정: /static/previews/{서류ID}_p{n}.png
        page_num = len(pages_out) + 1
        preview_path = f"/static/previews/{doc_id_guess}_p{page_num}.png"
        abs_preview = pathlib.Path(preview_path.lstrip("/"))

        if not abs_preview.exists():
            # 동일 폴더에서 패턴 탐색
            candidates = list(pathlib.Path("static/previews").glob(f"{doc_id_guess}*.png"))
            if candidates:
                preview_path = "/" + str(sorted(candidates)[min(page_num - 1, len(candidates) - 1)])

        # 이미지 크기 얻기 (픽셀 좌표 → 비율 변환에 필요)
        img_w, img_h = _get_img_size(preview_path.lstrip("/"))

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
            label = ra.get("name") or ra.get("자필기재", "") or list(ra.values())[0] if ra else ""
            if img_w and img_h:
                fx = px / img_w
                fy = py / img_h
                fw = pw / img_w
                fh = ph / img_h
            else:
                fx, fy, fw, fh = px, py, pw, ph
            fields.append({
                "항목ID": label,
                "설명": label,
                "color": "#E53935",
                "x": max(0.0, min(1.0, fx)),
                "y": max(0.0, min(1.0, fy)),
                "w": max(0.0, min(1.0, fw)),
                "h": max(0.0, min(1.0, fh)),
            })

        if fields or abs_preview.exists():
            pages_out.append({"page": page_num, "image": preview_path, "fields": fields})

    if not pages_out:
        return None

    return {
        "서류ID": doc_id_guess,
        "서류명": raw.get("서류명", doc_id_guess),
        "pages": pages_out,
    }


def _get_img_size(rel_path: str) -> tuple[int, int]:
    try:
        from PIL import Image as PILImage
        with PILImage.open(rel_path) as img:
            return img.size  # (width, height)
    except Exception:
        return (0, 0)


def _fix_pages_coords(data: dict) -> dict:
    """
    pages/fields 형식의 좌표를 0-1 비율로 정규화.

    좌표 저장 방식 자동 감지:
      • max(w, h) > 1.5  →  inches(300 DPI)로 저장됨.
      • y >= 1.0 필드    →  다음 페이지 항목. y -= 1 해서 별도 page 엔트리로 분리.
    """
    import copy
    from collections import defaultdict
    data = copy.deepcopy(data)

    existing_page_nums = {p.get("page") for p in data.get("pages", [])}
    result_pages = []

    for page in data.get("pages", []):
        fields = page.get("fields", [])
        if not fields:
            result_pages.append(page)
            continue

        max_dim = max((max(abs(f.get("w", 0)), abs(f.get("h", 0))) for f in fields), default=0)
        base_img = page.get("image", "")
        img_w, img_h = _get_img_size(base_img.lstrip("/"))

        if max_dim > 1.5 and img_w and img_h:
            sx = 300.0 / img_w
            sy = 300.0 / img_h
            for f in fields:
                f["x"] = f.get("x", 0) * sx
                f["y"] = f.get("y", 0) * sy
                f["w"] = f.get("w", 0) * sx
                f["h"] = f.get("h", 0) * sy

        # y >= 1.0 필드를 page offset 별로 버킷 분리
        # e.g. y=1.165 → page+1, local_y=0.165
        buckets: dict = defaultdict(list)
        for f in fields:
            y_val = f.get("y", 0)
            offset = int(y_val) if y_val >= 0 else 0
            fc = dict(f)
            fc["y"] = y_val - offset
            buckets[offset].append(fc)

        base_page_num = page.get("page", 1)

        for offset in sorted(buckets.keys()):
            p_num = base_page_num + offset
            # 이미 별도 페이지 엔트리가 있으면 건너뜀 (중복 방지)
            if offset > 0 and p_num in existing_page_nums:
                continue

            if offset == 0:
                p_img = base_img
            else:
                # /static/previews/서류_p1.png → /static/previews/서류_p2.png
                p_img = re.sub(
                    r'(_p)(\d+)(\.png)$',
                    lambda m: f'{m.group(1)}{int(m.group(2)) + offset}{m.group(3)}',
                    base_img
                )

            clamped = []
            for f in buckets[offset]:
                x = max(0.0, min(0.98, f.get("x", 0)))
                y = max(0.0, min(0.98, f.get("y", 0)))
                w = max(0.01, min(1.0 - x, f.get("w", 0)))
                h = max(0.005, min(1.0 - y, f.get("h", 0)))
                f["x"], f["y"], f["w"], f["h"] = x, y, w, h
                clamped.append(f)

            result_pages.append({"page": p_num, "image": p_img, "fields": clamped})

    result_pages.sort(key=lambda p: p.get("page", 0))
    data["pages"] = result_pages
    return data


@app.route("/api/annotations/<doc_id>")
def api_annotations(doc_id):
    path = _resolve_annotation_path(doc_id)
    if not path or not path.exists():
        return jsonify({"error": "없음"}), 404
    data = _load_annotation(path)
    if not data:
        return jsonify({"error": "파싱 실패"}), 404
    # 이미지 파일이 실제로 존재하는 페이지만 반환
    data["pages"] = [
        p for p in data.get("pages", [])
        if p.get("image") and pathlib.Path(p["image"].lstrip("/")).exists()
    ]
    if not data["pages"]:
        return jsonify({"error": "미리보기 이미지 없음"}), 404
    return jsonify(data)


@app.route("/api/annotations")
def api_annotations_index():
    return jsonify({"docs": _list_annotation_docs()})


@app.route("/docs/demo3/<path:filename>")
def serve_demo3(filename):
    return send_from_directory("docs/demo3", filename)


if __name__ == "__main__":
    app.run(debug=True, port=5001)
