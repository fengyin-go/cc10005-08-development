"""失败重放队列接口：按到期时间列出卡片，支持冻结、解除冻结、人工立即领取与上报。

路由层只做参数转发与错误信息，状态流转全部在 ``app.services.replay_queue`` 里。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.replay_queue import (
    CARD_ACTIONS,
    CARD_STATUSES,
    CLAIM_MODES,
    ReplayQueueService,
    replay_queue,
)

router = APIRouter(prefix="/api/replay", tags=["失败重放队列"])

service: ReplayQueueService = replay_queue


@router.get("/overview", response_model=dict)
def overview() -> dict[str, Any]:
    """队列概况：各状态计数、当前可领取数量与租约配置。"""
    return service.overview()


@router.get("", response_model=PageResult[dict])
def list_cards(
    keyword: str | None = Query(default=None, description="按作业编号或卡片编号检索"),
    status: str | None = Query(default=None, description="、".join(CARD_STATUSES)),
    page: int = 1,
    size: int = 50,
) -> PageResult[dict]:
    """按到期时间升序返回卡片；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    payload = service.list_cards(keyword=keyword, status=status, page=page, size=size)
    return PageResult(**payload)


@router.get("/late-receipts", response_model=dict)
def late_receipts() -> dict[str, Any]:
    """迟到回执：租约过期后才到达的执行结果，只作为证据留存，不改变卡片状态。"""
    items = service.late_receipts()
    return {"items": items, "total": len(items)}


@router.post("/cards", response_model=ActionResult)
def create_card(payload: EntryPayload) -> ActionResult:
    """为采集作业建一张重放卡片；已经排好的等待承诺按建卡时的退避配置快照。"""
    entry, message = service.create_card(payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/cards/{card_id}", response_model=dict)
def get_card(card_id: int) -> dict:
    """读取单张卡片：阶段序列、剩余次数、下次允许领取时刻与当前租约。"""
    card = service.get_card(card_id)
    if card is None:
        raise HTTPException(status_code=404, detail=f"重放卡片 {card_id} 不存在或已归档")
    return card


@router.post("/cards/{card_id}/actions", response_model=ActionResult)
def run_action(card_id: int, payload: EntryPayload) -> ActionResult:
    """执行冻结卡片、解除冻结、领取尝试、上报结果、回收过期租约。"""
    action = str(payload.values.get("action") or "").strip()
    card, message = service.run_action(card_id, action, payload.values)
    if card is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=card)


@router.post("/claim", response_model=ActionResult)
def scheduled_claim(payload: EntryPayload) -> ActionResult:
    """定时领取：执行者按到期时间批量领取；与人工立即领取竞争时只有一方拿到本次尝试。"""
    executor = str(payload.values.get("executor") or "").strip()
    if not executor:
        return ActionResult(ok=False, message="定时领取必须带上执行者标识")
    mode = str(payload.values.get("mode") or "定时领取").strip()
    if mode not in CLAIM_MODES:
        return ActionResult(ok=False, message=f"领取方式「{mode}」不在允许范围：{'、'.join(CLAIM_MODES)}")
    limit = int(payload.values.get("limit") or 5)
    lease_seconds = payload.values.get("lease_seconds")
    result = service.scheduled_claim(
        executor,
        limit=limit,
        lease_seconds=int(lease_seconds) if lease_seconds else 120,
    )
    return ActionResult(
        ok=bool(result.get("ok")),
        message=str(result.get("message")),
        entry={"claimed": result.get("claimed", []), "skipped": result.get("skipped", [])},
    )


@router.get("/export")
def export_cards() -> dict[str, Any]:
    """导出完整重放队列：含已转存卡片与迟到回执。"""
    payload = service.list_cards(page=1, size=10000)
    return {
        "module": "replay",
        "total": payload["total"],
        "items": payload["items"],
        "late_receipts": service.late_receipts(),
        "actions": CARD_ACTIONS,
    }
