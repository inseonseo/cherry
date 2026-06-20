"""
db.py — Azure Cosmos DB 연동 (점검 결과 저장 및 대시보드 조회)
"""

import os
import uuid
from datetime import datetime, timezone

_cosmos_client = None
_container = None
_cosmos_available = None


def _get_container():
    global _cosmos_client, _container, _cosmos_available
    if _cosmos_available is False:
        return None
    if _container is None:
        try:
            from azure.cosmos import CosmosClient
        except ImportError:
            import subprocess, sys
            print("[DB] azure-cosmos 없음 — 자동 설치 시도")
            subprocess.run([sys.executable, "-m", "pip", "install", "azure-cosmos>=4.7.0", "-q"])
            try:
                from azure.cosmos import CosmosClient
            except ImportError:
                print("[DB] 설치 실패 — DB 기능 비활성화")
                _cosmos_available = False
                return None
        endpoint = os.environ.get("COSMOS_ENDPOINT", "")
        key = os.environ.get("COSMOS_KEY", "")
        if not endpoint or not key:
            return None
        _cosmos_client = CosmosClient(endpoint, credential=key)
        db = _cosmos_client.get_database_client("docfine")
        _container = db.get_container_client("runs")
        _cosmos_available = True
    return _container


def save_run(batch: list, total_pages: int) -> str | None:
    """점검 결과 저장. 저장된 run_id 반환."""
    container = _get_container()
    if not container:
        return None

    now = datetime.now(timezone.utc)
    run_id = str(uuid.uuid4())
    run_date = now.strftime("%Y-%m-%d")

    customers = []
    for item in batch:
        결과 = item["결과"]
        customers.append({
            "고객번호": item["고객번호"],
            "상품유형": item["상품유형"],
            "고객군": item["고객군"],
            "pass": len(결과["누락서류"]) == 0 and all(
                v.get("pass") for v in 결과["전체_결과"].values()
            ),
            "누락서류": 결과["누락서류"],
            "오류항목": [
                {"서류": doc_id, "오류": [e["오류"] for e in v.get("errors", [])]}
                for doc_id, v in 결과["전체_결과"].items()
                if not v.get("pass")
            ],
            "적용규칙": 결과["적용규칙"],
        })

    doc = {
        "id": run_id,
        "run_date": run_date,
        "run_timestamp": now.isoformat(),
        "total_pages": total_pages,
        "total_customers": len(customers),
        "pass_count": sum(1 for c in customers if c["pass"]),
        "fail_count": sum(1 for c in customers if not c["pass"]),
        "customers": customers,
    }

    container.create_item(doc)
    print(f"[DB] run 저장: {run_id} ({run_date}, {len(customers)}명)")
    return run_id


def get_dashboard() -> dict:
    """대시보드용 데이터 반환 — 쿼리 1회로 통합."""
    container = _get_container()
    if not container:
        return {"runs": [], "stats": {}}

    # 한 번에 전체 필드 조회 (customers 포함)
    items = list(container.query_items(
        query="SELECT * FROM c ORDER BY c.run_timestamp DESC OFFSET 0 LIMIT 60",
        enable_cross_partition_query=True,
    ))

    # 누락 서류 빈도 집계 (Python에서 처리)
    missing_freq: dict = {}
    for item in items:
        for cust in item.get("customers", []):
            for doc in cust.get("누락서류", []):
                missing_freq[doc] = missing_freq.get(doc, 0) + 1

    top_missing = sorted(missing_freq.items(), key=lambda x: -x[1])[:5]
    total_customers = sum(r["total_customers"] for r in items)
    pass_count = sum(r["pass_count"] for r in items)

    # 응답에서 customers 필드 제거 (이력 목록엔 불필요, 상세는 별도 API)
    runs = [
        {k: v for k, v in item.items() if k != "customers"}
        for item in items
    ]

    return {
        "runs": runs,
        "stats": {
            "total_runs": len(runs),
            "total_customers": total_customers,
            "pass_rate": round(pass_count / total_customers * 100, 1) if total_customers else 0,
            "top_missing": [{"서류": d, "건수": c} for d, c in top_missing],
        },
    }


def get_run_detail(run_id: str) -> dict | None:
    """특정 run의 전체 결과 반환."""
    container = _get_container()
    if not container:
        return None
    try:
        items = list(container.query_items(
            query="SELECT * FROM c WHERE c.id = @id",
            parameters=[{"name": "@id", "value": run_id}],
            enable_cross_partition_query=True,
        ))
        return items[0] if items else None
    except Exception:
        return None
