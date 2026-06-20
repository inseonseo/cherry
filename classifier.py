"""
classifier.py
역할: PDF 페이지 이미지를 받아서 어떤 서류인지 판별
Azure OpenAI GPT-4o Vision 직접 사용 (AIProjectClient 불필요)
"""

import os
import json
import base64
import io

from openai import AzureOpenAI
from PIL import Image

_client: AzureOpenAI | None = None


def _get_client() -> AzureOpenAI:
    global _client
    if _client is None:
        _client = AzureOpenAI(
            api_key=os.environ["AZURE_OPENAI_KEY"],
            api_version="2024-02-01",
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        )
    return _client


DEPLOYMENT = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")


def _pil_to_base64(image: Image.Image) -> str:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode()


def _detect_sales_checklist(image_b64: str) -> bool:
    """
    금융투자상품 판매체크리스트 전용 2차 판별.
    1차 분류가 unknown일 때만 호출해 오탐 위험을 줄인다.
    """
    prompt = (
        "이미지가 '금융투자상품 판매체크리스트'인지 예/아니오로 판별하세요.\n"
        "다음 유니크 단서가 보이면 예로 판단합니다:\n"
        "- 제목: 금융투자상품 판매체크리스트\n"
        "- 문구: 채권(채무증권)\n"
        "- 체크리스트 형식의 연속 체크박스 표\n"
        "JSON으로만 응답: {\"is_sales_checklist\": true|false}"
    )

    resp = _get_client().chat.completions.create(
        model=DEPLOYMENT,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {
                    "url": f"data:image/png;base64,{image_b64}",
                    "detail": "high",
                }},
            ],
        }],
        max_tokens=60,
        temperature=0,
    )

    raw = resp.choices[0].message.content.strip()
    try:
        clean = raw.replace("```json", "").replace("```", "").strip()
        data = json.loads(clean)
        return bool(data.get("is_sales_checklist", False))
    except Exception:
        return False


def classify_document(client, agent_name: str, image: Image.Image, 서류목록) -> str:
    """
    GPT-4o Vision으로 이미지 서류 판별
    - client, agent_name: 하위 호환성 유지용 (미사용)
    - image: PIL 이미지
    - 서류목록: checklist에서 넘어온 전체 서류 목록
    """
    image_b64 = _pil_to_base64(image)

    doc_lines = "\n".join(
        f'- "{d["서류ID"]}": 키워드 → {", ".join(d.get("판별키워드", []))}'
        for d in 서류목록
    )
    prompt = (
        f"아래 서류 목록 중 이미지에 해당하는 서류ID 하나를 골라 JSON으로만 응답하세요.\n"
        f"해당 없으면 \"unknown\".\n\n"
        f"중요: '금융투자상품 판매체크리스트'는 아래 단서가 보이면 반드시 '금융상품_판매체크리스트'로 분류하세요.\n"
        f"단서: '금융투자상품 판매체크리스트', '채권(채무증권)', 체크리스트 표 형식.\n\n"
        f"{doc_lines}\n\n"
        f"반드시 JSON으로만 응답: {{\"서류ID\": \"...\"}}"
    )

    resp = _get_client().chat.completions.create(
        model=DEPLOYMENT,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {
                    "url": f"data:image/png;base64,{image_b64}",
                    "detail": "high",
                }},
            ],
        }],
        max_tokens=100,
        temperature=0,
    )

    raw = resp.choices[0].message.content.strip()
    try:
        clean = raw.replace("```json", "").replace("```", "").strip()
        result = json.loads(clean)
        doc_id = result.get("서류ID", "unknown")
        if doc_id == "unknown" and _detect_sales_checklist(image_b64):
            return "금융상품_판매체크리스트"
        return doc_id
    except Exception:
        if _detect_sales_checklist(image_b64):
            return "금융상품_판매체크리스트"
        return "unknown"
