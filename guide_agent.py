"""
guide_agent.py
사전점검 Guide Orchestrator Agent
- Azure OpenAI Assistants API: 대화 오케스트레이션 + tool 호출
- gpt-4o-transcribe: 음성 청크 → 텍스트
"""

import json
import os
import time
import io

from openai import AzureOpenAI, BadRequestError

_checklist: dict = {}


def _load_checklist():
    global _checklist
    if not _checklist:
        with open("checklist.json", "r", encoding="utf-8-sig") as f:
            _checklist = json.load(f)


# ── 툴 구현 ──────────────────────────────────────────────────────────────
def _tool_get_required_documents(상품유형: str, 고객군: str, 고령투자자: bool) -> str:
    _load_checklist()
    prefix_map = {
        "ELS": "파생_ELS", "DLS": "파생_ELS",
        "ELB": "파생_ELB", "DLB": "파생_DLB",
        "펀드": "펀드", "단기사채": "단기사채",
    }
    prefix = prefix_map.get(상품유형)
    if not prefix:
        return json.dumps({"error": f"알 수 없는 상품유형: {상품유형}"}, ensure_ascii=False)

    if 고령투자자:
        suffix = "고령"
    elif "취약" in 고객군:
        suffix = "취약"
    elif "전문" in 고객군:
        suffix = "전문"
    elif "부적합" in 고객군:
        suffix = "부적합"
    else:
        suffix = "일반"

    key = f"{prefix}_{suffix}"
    docs = _checklist.get("_필요서류", {}).get(key, [])
    return json.dumps({
        "규칙키": key,
        "필요서류": [
            {"서류ID": d, "서류명": _checklist.get(d, {}).get("서류명", d)}
            for d in docs
        ],
    }, ensure_ascii=False)


def _tool_get_document_checklist(서류ID: str) -> str:
    _load_checklist()
    doc = _checklist.get(서류ID)
    if not doc:
        return json.dumps({"error": f"서류 없음: {서류ID}"}, ensure_ascii=False)
    return json.dumps({
        "서류ID": 서류ID,
        "서류명": doc.get("서류명", ""),
        "체크그룹": doc.get("체크그룹", []),
        "필기입력항목": doc.get("필기입력항목", []),
    }, ensure_ascii=False)


_TOOL_MAP = {
    "get_required_documents": lambda args: _tool_get_required_documents(**args),
    "get_document_checklist": lambda args: _tool_get_document_checklist(**args),
}

# ── OpenAI Assistant 툴 스펙 ─────────────────────────────────────────────
_TOOL_DEFS = [
    {
        "type": "function",
        "function": {
            "name": "get_required_documents",
            "description": "상품유형·고객군·고령투자자 여부로 필요 서류 목록을 조회합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "상품유형": {
                        "type": "string",
                        "enum": ["ELS", "DLS", "ELB", "DLB", "펀드", "단기사채"],
                    },
                    "고객군": {
                        "type": "string",
                        "enum": ["일반금융소비자", "취약금융소비자", "전문금융소비자", "부적합투자자"],
                    },
                    "고령투자자": {"type": "boolean"},
                },
                "required": ["상품유형", "고객군", "고령투자자"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_document_checklist",
            "description": "특정 서류의 체크그룹·필기항목 상세를 조회합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "서류ID": {"type": "string", "description": "예: 설명서교부_가입확인서"},
                },
                "required": ["서류ID"],
            },
        },
    },
]

_SYSTEM_PROMPT = """당신은 증권사 금융상품 판매 사전점검 가이드 전문가입니다.

## 역할
영업직원이 고객과 상담하는 동안 실시간으로 도와주는 AI 어시스턴트입니다.
- 상담 내용(텍스트 질문 또는 음성 녹취)을 분석하여 필요한 서류와 주의사항을 안내합니다
- 서류 작성 방법, 체크항목, 서명 위치 등을 구체적으로 알려줍니다
- 고객 특성(고령, 취약금융소비자, 전문금융소비자 등)에 따른 추가 절차를 안내합니다

## 도구
- get_required_documents: 상품유형·고객군 조합으로 필요 서류 목록 조회
- get_document_checklist: 특정 서류의 체크항목 상세 조회

## 응답 원칙
- 영업 현장에서 바로 쓸 수 있도록 간결하고 구체적으로 답변합니다
- 상담 중 파악된 정보(상품유형, 고객 연령, 투자성향 등)를 누적하여 활용합니다
- 음성 녹취가 들어오면 핵심 정보를 추출하고 가이드를 즉시 업데이트합니다
- 한국어로만 답변합니다"""


# ── Azure OpenAI 클라이언트 (Assistants용) ──────────────────────────────
_oc: AzureOpenAI | None = None


def _get_oc() -> AzureOpenAI:
    global _oc
    if _oc is None:
        _oc = AzureOpenAI(
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            api_key=os.environ["AZURE_OPENAI_KEY"],
            api_version="2024-05-01-preview",
        )
    return _oc


# ── Assistant 생명주기 ────────────────────────────────────────────────────
_guide_agent_id: str | None = None


def get_or_create_guide_agent(client=None) -> str:
    global _guide_agent_id
    if _guide_agent_id:
        return _guide_agent_id

    env_id = os.environ.get("GUIDE_AGENT_ID", "").strip()
    if env_id:
        _guide_agent_id = env_id
        print(f"[GUIDE] 기존 에이전트: {_guide_agent_id}")
        return _guide_agent_id

    oc = _get_oc()
    deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")
    assistant = oc.beta.assistants.create(
        model=deployment,
        name="사전점검_가이드",
        instructions=_SYSTEM_PROMPT,
        tools=_TOOL_DEFS,
    )
    _guide_agent_id = assistant.id
    print(f"[GUIDE] 새 에이전트 생성: {_guide_agent_id}")
    print(f"[GUIDE] .env에 추가 권장: GUIDE_AGENT_ID={_guide_agent_id}")
    return _guide_agent_id


def create_session(client=None) -> str:
    thread = _get_oc().beta.threads.create()
    return thread.id


# ── Agent 실행 루프 (tool calling 포함) ─────────────────────────────────
def _run_agent(client, thread_id: str, agent_id: str) -> str:
    oc = _get_oc()
    run = oc.beta.threads.runs.create(thread_id=thread_id, assistant_id=agent_id)

    while run.status in ("queued", "in_progress", "requires_action"):
        if run.status == "requires_action":
            outputs = []
            for tc in run.required_action.submit_tool_outputs.tool_calls:
                name = tc.function.name
                args = json.loads(tc.function.arguments)
                print(f"[GUIDE] 툴 호출: {name}({args})")
                result = _TOOL_MAP[name](args)
                outputs.append({"tool_call_id": tc.id, "output": result})
            run = oc.beta.threads.runs.submit_tool_outputs(
                thread_id=thread_id, run_id=run.id, tool_outputs=outputs
            )
        else:
            time.sleep(0.5)
            run = oc.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run.id)

    if run.status != "completed":
        raise RuntimeError(f"Agent 실행 실패: {run.status}")

    msgs = oc.beta.threads.messages.list(thread_id=thread_id, order="desc", limit=1)
    for msg in msgs.data:
        if msg.role == "assistant":
            for block in msg.content:
                if block.type == "text":
                    return block.text.value
    return ""


def chat(client, thread_id: str, agent_id: str, message: str) -> str:
    _get_oc().beta.threads.messages.create(thread_id=thread_id, role="user", content=message)
    return _run_agent(client, thread_id, agent_id)


# ── 음성 변환 ────────────────────────────────────────────────────────────
_transcribe_client: AzureOpenAI | None = None
_TRANSCRIBE_DEPLOYMENT = os.environ.get("AZURE_TRANSCRIBE_DEPLOYMENT", "gpt-4o-transcribe")


def _get_transcribe_client() -> AzureOpenAI:
    global _transcribe_client
    if _transcribe_client is None:
        _transcribe_client = AzureOpenAI(
            api_key=os.environ["AZURE_OPENAI_KEY"],
            api_version="2025-01-01-preview",
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        )
    return _transcribe_client


def transcribe_chunk(audio_bytes: bytes, mime_type: str = "audio/webm") -> str:
    # Some MediaRecorder chunks are too small or not standalone containers.
    # Skip them instead of failing the whole voice flow.
    if not audio_bytes or len(audio_bytes) < 256:
        return ""

    mime = (mime_type or "audio/webm").split(";")[0].strip().lower()
    ext_map = {
        "audio/webm": "webm", "audio/wav": "wav",
        "audio/mp4": "mp4", "audio/mpeg": "mp3", "audio/ogg": "ogg",
    }
    ext = ext_map.get(mime, "webm")
    buf = io.BytesIO(audio_bytes)
    buf.name = f"chunk.{ext}"

    try:
        resp = _get_transcribe_client().audio.transcriptions.create(
            model=_TRANSCRIBE_DEPLOYMENT,
            file=buf,
            language="ko",
        )
        return resp.text
    except BadRequestError as e:
        msg = str(e)
        # Common transient/format issue for partial chunks.
        if "Audio file might be corrupted or unsupported" in msg:
            return ""
        raise


def voice_chunk(client, thread_id: str, agent_id: str,
                audio_bytes: bytes, mime_type: str = "audio/webm") -> dict:
    transcript = transcribe_chunk(audio_bytes, mime_type)
    if not transcript.strip():
        return {"transcript": "", "guide": None}

    oc = _get_oc()
    msg = (
        f"[상담 내용 - 실시간 녹취]\n{transcript}\n\n"
        "위 내용을 바탕으로 필요한 서류나 주의사항이 있으면 간결하게 알려주세요."
    )
    oc.beta.threads.messages.create(thread_id=thread_id, role="user", content=msg)
    guide = _run_agent(client, thread_id, agent_id)
    return {"transcript": transcript, "guide": guide}
