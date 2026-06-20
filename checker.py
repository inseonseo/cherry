"""
checker.py
서류 이미지(들)를 받아 체크박스/필기항목의 기재 여부를 읽어 반환.

1단계: Azure Document Intelligence로 텍스트 추출 (체크박스 :selected:/:unselected: 포함)
2단계: GPT-4o에 텍스트만 전달 → 이미지에 담긴 PII 미전달
폴백:  AZURE_DI_ENDPOINT 미설정 시 기존 GPT-4o Vision 방식 유지
"""

import os
import json
import base64
import re
import io
from urllib.parse import urlparse
from openai import AzureOpenAI

_oa_client: AzureOpenAI | None = None
_di_client = None
_di_disabled = False


def _normalize_di_endpoint(endpoint: str) -> str:
    """DI SDK가 기대하는 리소스 루트 endpoint 형태로 정규화."""
    ep = (endpoint or "").strip()
    if not ep:
        return ""

    parsed = urlparse(ep)
    # Allow users to provide either full URL or hostname only.
    if parsed.scheme and parsed.netloc:
        base = f"{parsed.scheme}://{parsed.netloc}"
    else:
        host = ep.split("/")[0]
        base = f"https://{host}"

    return base.rstrip("/")


def _get_oa_client() -> AzureOpenAI:
    global _oa_client
    if _oa_client is None:
        _oa_client = AzureOpenAI(
            api_key=os.environ["AZURE_OPENAI_KEY"],
            api_version="2024-02-01",
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        )
    return _oa_client


def _get_di_client():
    global _di_client, _di_disabled
    if _di_disabled:
        return None
    if _di_client is None:
        endpoint = os.environ.get("AZURE_DI_ENDPOINT", "")
        key = os.environ.get("AZURE_DI_KEY", "")
        if endpoint and key:
            from azure.ai.documentintelligence import DocumentIntelligenceClient
            from azure.core.credentials import AzureKeyCredential
            normalized_endpoint = _normalize_di_endpoint(endpoint)
            if normalized_endpoint != endpoint.strip().rstrip("/"):
                print(f"[checker] AZURE_DI_ENDPOINT 정규화 적용: {normalized_endpoint}")
            _di_client = DocumentIntelligenceClient(
                endpoint=normalized_endpoint,
                credential=AzureKeyCredential(key),
            )
    return _di_client


DEPLOYMENT = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")


def _build_system_prompt(doc_rule: dict, di_mode: bool = False) -> str:
    doc_name = doc_rule.get("서류명", "")

    checkbox_lines = []
    for group in doc_rule.get("체크그룹", []):
        group_id = group["그룹ID"]
        items = group["항목"]
        checkbox_lines.append(f'- "{group_id}": [{" / ".join(items)}] 중 체크된 항목들')

    handwriting_lines = []
    for item in doc_rule.get("필기입력항목", []):
        item_id = item["항목ID"]
        desc = item.get("설명", "")
        handwriting_lines.append(f'- "{item_id}": {desc} — 기재됨 여부')

    checkbox_block = "\n".join(checkbox_lines) if checkbox_lines else "(없음)"
    handwriting_block = "\n".join(handwriting_lines) if handwriting_lines else "(없음)"

    input_desc = (
        "Document Intelligence가 추출한 서류 텍스트 (체크박스는 :selected:/:unselected: 로 표시)"
        if di_mode else "서류 이미지"
    )

    return f"""당신은 증권사 금융상품 판매 서류를 읽는 전문가입니다.

## 역할
{input_desc}를 보고 지정된 항목들의 기재 여부를 읽어 보고합니다.
당신은 규칙 판단을 하지 않습니다. 오직 "있다 / 없다"만 읽습니다.

## 대상 서류
{doc_name}

## 읽어야 할 항목

### 체크박스 항목
{checkbox_block}

### 필기 입력 항목
{handwriting_block}

## 출력 규칙
- 반드시 아래 JSON 형식만 반환합니다. 설명이나 다른 텍스트는 절대 포함하지 않습니다.
- 체크박스: 체크된 항목은 true, 체크 안 된 항목은 false
- 필기항목: 내용이 기재된 경우 true, 비어있거나 공란인 경우 false
- 이미지가 여러 장이면 서류 전체를 통합해서 판단합니다

{{
  "체크박스": {{
    "그룹ID": {{"항목명": true/false, ...}},
    ...
  }},
  "필기": {{
    "항목ID": true/false,
    ...
  }}
}}"""


def _encode_image(image_input) -> str:
    if isinstance(image_input, bytes):
        return base64.b64encode(image_input).decode("utf-8")
    buf = io.BytesIO()
    image_input.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def _extract_di_text(images: list) -> tuple[str, list]:
    """DI로 텍스트 추출 + 체크박스 위치 반환.

    반환: (text, selection_marks)
    selection_marks: [{"state": "selected"|"unselected", "page": 1,
                       "x": 0.1, "y": 0.2, "w": 0.05, "h": 0.02}, ...]
    """
    di = _get_di_client()
    all_content = []
    all_marks = []

    for page_idx, img in enumerate(images):
        buf = io.BytesIO()
        if hasattr(img, "save"):
            img.save(buf, format="PNG")
        else:
            buf.write(img)
        img_bytes = buf.getvalue()

        poller = di.begin_analyze_document(
            "prebuilt-layout",
            body=img_bytes,
            content_type="image/png",
        )
        result = poller.result()
        all_content.append(f"[페이지 {page_idx + 1}]\n{result.content}")

        if not result.pages:
            continue
        page = result.pages[0]
        W = page.width or 1
        H = page.height or 1

        for sm in (page.selection_marks or []):
            poly = sm.polygon  # flat list [x1,y1,x2,y2,...] in pixels
            if not poly:
                continue
            xs = poly[0::2]
            ys = poly[1::2]
            is_selected = "UNSELECTED" not in str(sm.state).upper()
            all_marks.append({
                "state": "selected" if is_selected else "unselected",
                "page": page_idx + 1,
                "x": round(min(xs) / W, 4),
                "y": round(min(ys) / H, 4),
                "w": round((max(xs) - min(xs)) / W, 4),
                "h": round((max(ys) - min(ys)) / H, 4),
            })

    return "\n\n".join(all_content), all_marks


def _call_vision_di(images: list, system_prompt: str) -> dict:
    """DI 텍스트 추출 → GPT-4o 텍스트 모드 판단 + 실제 체크박스 좌표 포함"""
    di_text, selection_marks = _extract_di_text(images)

    response = _get_oa_client().chat.completions.create(
        model=DEPLOYMENT,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": (
                "아래는 Document Intelligence가 추출한 서류 내용입니다.\n"
                "체크박스는 :selected: (체크됨) / :unselected: (미체크) 로 표시됩니다.\n"
                "이를 기반으로 판단하세요.\n\n"
                f"{di_text}"
            )},
        ],
        max_tokens=1000,
        temperature=0,
    )

    raw = response.choices[0].message.content.strip()
    try:
        cleaned = re.sub(r"```(?:json)?|```", "", raw).strip()
        result = json.loads(cleaned)
    except json.JSONDecodeError:
        result = {"체크박스": {}, "필기": {}, "_parse_error": raw}

    if selection_marks:
        result["_di_marks"] = selection_marks
    return result


def _call_vision(images: list, system_prompt: str) -> dict:
    """GPT-4o Vision 방식"""
    image_contents = []
    for img in images:
        b64 = _encode_image(img)
        image_contents.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{b64}", "detail": "high"},
        })
    image_contents.append({
        "type": "text",
        "text": "위 서류 이미지에서 지정된 항목들을 읽어 JSON으로만 반환하세요.",
    })

    response = _get_oa_client().chat.completions.create(
        model=DEPLOYMENT,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": image_contents},
        ],
        max_tokens=1000,
        temperature=0,
    )

    raw = response.choices[0].message.content.strip()
    try:
        cleaned = re.sub(r"```(?:json)?|```", "", raw).strip()
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return {"체크박스": {}, "필기": {}, "_parse_error": raw}


def _apply_rules(vision_result: dict, doc_rule: dict, context: dict) -> list:
    errors = []
    checkbox_result = vision_result.get("체크박스", {})
    handwriting_result = vision_result.get("필기", {})

    # 체크박스 결과를 누적하여 within-doc conditional 판단에 활용
    checked_state: dict[str, list] = {}

    for group in doc_rule.get("체크그룹", []):
        group_id = group["그룹ID"]
        rule = group["규칙"]
        items = group.get("항목", [])
        group_data = checkbox_result.get(group_id, {})

        checked = [item for item in items if group_data.get(item) is True]
        checked_count = len(checked)
        checked_state[group_id] = checked

        if rule == "exactly_one":
            if checked_count == 0:
                errors.append({"항목": group_id, "오류": "필수 선택 항목 미기재"})
            elif checked_count > 1:
                errors.append({"항목": group_id, "오류": f"중복 선택 오류 ({', '.join(checked)})"})

        elif rule == "at_least_one":
            if checked_count == 0:
                errors.append({"항목": group_id, "오류": "하나 이상 선택 필요"})

        elif rule == "at_least_two":
            if checked_count < 2:
                errors.append({"항목": group_id, "오류": "두 항목 이상 선택 필요"})

        elif rule == "check":
            # 단순 체크 여부 — 항목 목록 없이 해당 그룹 자체가 체크됐는지
            any_checked = any(v for v in group_data.values()) if group_data else checked_count > 0
            if not any_checked:
                errors.append({"항목": group_id, "오류": "필수 확인 항목 미체크"})

        elif rule == "conditional":
            required_when_true = group.get("필수_조건일치시", True)
            error_when_mismatch = group.get("오기재_조건불일치시", False)
            msg_미기재 = group.get("오류메시지_미기재", "필수 항목 미기재")
            msg_오기재 = group.get("오류메시지_오기재", "불필요한 항목 기재됨 (오기재)")

            if "조건서류" in group:
                condition_docs = group.get("조건서류", [])
                condition_op = group.get("조건연산", "any_of")
                applied_docs = context.get("적용서류", [])
                if condition_op == "any_of":
                    condition_met = any(doc in applied_docs for doc in condition_docs)
                else:
                    condition_met = all(doc in applied_docs for doc in condition_docs)
            else:
                condition_field = group.get("조건필드", "")
                condition_value = group.get("조건값", "")
                actual_value = context.get(condition_field, "")
                if not actual_value:
                    lst = checked_state.get(condition_field, [])
                    actual_value = lst[0] if lst else ""
                condition_met = (actual_value == condition_value)

            if condition_met and required_when_true and checked_count == 0:
                errors.append({"항목": group_id, "오류": msg_미기재})
            elif not condition_met and error_when_mismatch and checked_count > 0:
                errors.append({"항목": group_id, "오류": msg_오기재})

    for item in doc_rule.get("필기입력항목", []):
        item_id = item["항목ID"]
        is_filled = handwriting_result.get(item_id, False)

        if "조건부" in item:
            cond = item["조건부"]
            cond_rule = cond.get("규칙", "conditional")
            required_when_true = cond.get("필수_조건일치시", True)
            error_when_mismatch = cond.get("오기재_조건불일치시", False)
            msg_미기재 = cond.get("오류메시지_미기재", f"{item_id} 필수 기재")
            msg_오기재 = cond.get("오류메시지_오기재", f"{item_id} 불필요한 기재 (오기재)")

            def _checked_first(field_key):
                lst = checked_state.get(field_key, [])
                return lst[0] if lst else ""

            if cond_rule == "any_of":
                condition_met = False
                for c in cond.get("조건들", []):
                    cf, cv = c.get("조건필드", ""), c.get("조건값", "")
                    val = context.get(cf, "") or _checked_first(cf)
                    if val == cv:
                        condition_met = True
                        break
            else:
                cf = cond.get("조건필드", "")
                cv = cond.get("조건값", "")
                val = context.get(cf, "") or _checked_first(cf)
                condition_met = (val == cv)

            if condition_met and required_when_true and not is_filled:
                errors.append({"항목": item_id, "오류": msg_미기재})
            elif not condition_met and error_when_mismatch and is_filled:
                errors.append({"항목": item_id, "오류": msg_오기재})

        elif item.get("필수", False) and not is_filled:
            errors.append({"항목": item_id, "오류": "필수 필기항목 미기재"})

    return errors


def _normalize_sales_checklist_bond_selection(vision_result: dict, doc_rule: dict) -> None:
    """판매체크리스트 상단 금융상품 체크를 DI 좌표로 보정.

    GPT 텍스트 판독이 '기타'로 오인하는 경우가 있어,
    page1 상단 1행(금융상품)에서 채권/채무증권 위치의 선택 마크를 우선 반영한다.
    """
    if doc_rule.get("서류명") != "금융투자상품 판매체크리스트":
        return

    checkbox = vision_result.get("체크박스") or {}
    product_group = checkbox.get("금융상품")
    if not isinstance(product_group, dict):
        return

    if product_group.get("채권(채무증권)") is True:
        return

    di_marks = vision_result.get("_di_marks") or []
    if not isinstance(di_marks, list):
        return

    # 템플릿 기준: 상단 금융상품 1행의 채권/채무증권 체크박스 영역
    top_row_selected = [
        m for m in di_marks
        if m.get("page") == 1
        and m.get("state") == "selected"
        and 0.17 <= float(m.get("y", 0)) <= 0.195
    ]
    has_bond_mark = any(0.38 <= float(m.get("x", 0)) <= 0.56 for m in top_row_selected)
    has_debt_mark = any(0.62 <= float(m.get("x", 0)) <= 0.78 for m in top_row_selected)

    if has_bond_mark or has_debt_mark:
        product_group["채권(채무증권)"] = True
        # exactly_one 규칙에서 오인된 '기타' 선택은 제거
        if product_group.get("기타") is True:
            product_group["기타"] = False


def check_document(images: list, doc_rule: dict, context: dict = None) -> dict:
    """
    images:   PIL Image 또는 bytes 리스트 (해당 서류 페이지들)
    doc_rule: checklist.json에서 꺼낸 단일 서류 규칙 (체크그룹, 필기입력항목 포함)
    context:  {"고객군": "취약" | "일반" | "전문"} — conditional 규칙에 사용

    반환:
      {"서류명": str, "vision_raw": dict, "errors": [...], "pass": bool}
    """
    if context is None:
        context = {}

    use_di = _get_di_client() is not None
    print(f"[checker] 모드: {'DI → GPT-4o 텍스트' if use_di else 'GPT-4o Vision (DI 미설정)'}")
    system_prompt = _build_system_prompt(doc_rule, di_mode=use_di)

    if use_di:
        try:
            vision_raw = _call_vision_di(images, system_prompt)
        except Exception as e:
            global _di_disabled, _di_client
            _di_disabled = True
            _di_client = None
            print(f"[checker] DI 실패({type(e).__name__}): {e}")
            print("[checker] DI를 비활성화하고 GPT-4o Vision 폴백으로 계속 진행")
            vision_raw = _call_vision(images, _build_system_prompt(doc_rule, di_mode=False))
    else:
        vision_raw = _call_vision(images, system_prompt)

    _normalize_sales_checklist_bond_selection(vision_raw, doc_rule)

    errors = _apply_rules(vision_raw, doc_rule, context)

    result = {
        "서류명": doc_rule.get("서류명", ""),
        "vision_raw": vision_raw,
        "errors": errors,
        "pass": len(errors) == 0,
    }
    # DI 모드에서 실제 체크박스 위치 포함
    if "_di_marks" in vision_raw:
        result["di_marks"] = vision_raw["_di_marks"]
    return result
