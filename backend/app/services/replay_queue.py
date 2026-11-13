"""失败重放队列：领取租约、冻结、迟到回执、失败转存与重启续接。

业务约束（与页面文案一一对应）：

- 卡片按 ``next_claim_at`` 到期时间排序；每次领取发放一张带有效期的租约，只有持租约的执行者能上报。
- 租约有效期内，人工立即领取和定时领取都走同一把锁，同一张卡片只有一方拿到本次尝试。
- 租约到期时只回收「执行中（未确认）」的失败阶段，已确认成功的阶段保持封存；
  由于执行者从未确认，这次领取不计入重试次数。
- 过期之后才到达的回执不改变卡片状态，只写入 ``late_receipts`` 作为迟到证据。
- 建卡时快照当时的退避配置；之后修改重试策略只影响新卡片，已经排期的卡片保留原来的
  ``next_claim_at`` 等待承诺，重新排期时也沿用卡片自己的快照。
- 重试次数耗尽，或失败异常不在「可重试异常」许可集合里时，卡片转入失败转存；
  同时把停止位置写回关联采集作业，该作业不再出现在可领取队列里。
- 不提供「无条件从头重跑」：没有重置阶段或次数的动作，已转存卡片也不能再领取。
- 队列状态落盘到 ``.runtime/replay_queue.json``，重启时加载并先回收过期租约。
"""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.store import Store, store

CARD_PENDING = "待领取"
CARD_FROZEN = "已冻结"
CARD_CLAIMED = "领取中"
CARD_COMPLETED = "已完成"
CARD_ARCHIVED = "已转存"

CARD_STATUSES = [CARD_PENDING, CARD_FROZEN, CARD_CLAIMED, CARD_COMPLETED, CARD_ARCHIVED]
QUEUE_STATUSES = [CARD_PENDING, CARD_FROZEN, CARD_CLAIMED, CARD_COMPLETED]

STAGE_PENDING = "待确认"
STAGE_RUNNING = "执行中"
STAGE_SEALED = "已确认成功"
STAGE_FAILED = "未确认失败"

ACTION_FREEZE = "冻结卡片"
ACTION_UNFREEZE = "解除冻结"
ACTION_CLAIM = "领取尝试"
ACTION_REPORT = "上报结果"
ACTION_RECLAIM = "回收过期租约"
CARD_ACTIONS = [ACTION_FREEZE, ACTION_UNFREEZE, ACTION_CLAIM, ACTION_REPORT, ACTION_RECLAIM]

MODE_MANUAL = "人工立即"
MODE_SCHEDULED = "定时领取"
CLAIM_MODES = [MODE_MANUAL, MODE_SCHEDULED]

OUTCOME_SUCCESS = "成功"
OUTCOME_FAILURE = "失败"
OUTCOMES = [OUTCOME_SUCCESS, OUTCOME_FAILURE]

ALGORITHM_FIXED = "固定间隔"
ALGORITHM_LINEAR = "线性退避"
ALGORITHM_EXPONENTIAL = "指数退避"
ALGORITHMS = [ALGORITHM_FIXED, ALGORITHM_LINEAR, ALGORITHM_EXPONENTIAL]

LEASE_DEFAULT_SECONDS = 120
LEASE_MIN_SECONDS = 10
LEASE_MAX_SECONDS = 3600

DEFAULT_STATE_FILE = Path(__file__).resolve().parents[2] / ".runtime" / "replay_queue.json"

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def split_retryable(raw: Any) -> list[str]:
    """把「可重试异常」拆成条目：逗号、分号、顿号、竖线、换行都算分隔符。"""
    text = str(raw or "").strip()
    if not text:
        return []
    return [item.strip() for item in re.split(r"[,，;；、|\n]+", text) if item.strip()]


def stage_definitions(spider_entry: dict[str, Any] | None) -> list[str]:
    """从采集作业的输入推导阶段序列；没有作业信息时退回一条通用阶段。"""
    if not spider_entry:
        return ["列表采集"]
    template = str(spider_entry.get("抽取模板") or "").strip()
    return ["列表采集", f"详情抽取（{template or '模板未指定'}）", "结果校验"]


class ReplayQueueService:
    """队列服务：所有状态流转都在这里，路由层只负责转发与错误信息。"""

    def __init__(
        self,
        data_store: Store | None = None,
        state_file: Path | str | None = None,
        clock: Any | None = None,
    ) -> None:
        self._store = data_store or store
        env_path = os.environ.get("REPLAY_QUEUE_STATE_FILE")
        self._state_file = Path(state_file or env_path or DEFAULT_STATE_FILE)
        self._clock = clock or utcnow
        self._lock = threading.RLock()
        self._cards: list[dict[str, Any]] = []
        self._late_receipts: list[dict[str, Any]] = []
        self._loaded = False

    # ------------------------------------------------------------------ 生命周期

    def now(self) -> datetime:
        return self._clock()

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            payload = json.loads(self._state_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            self._seed_locked(self.now())
            self._persist_locked()
            return
        if not isinstance(payload, dict):
            return
        cards = payload.get("cards")
        late = payload.get("late_receipts")
        self._cards = [card for card in cards or [] if isinstance(card, dict)]
        self._late_receipts = [item for item in late or [] if isinstance(item, dict)]
        self._reconcile_jobs_locked()
        self._reclaim_expired_locked(self.now(), reason="服务重启后回收未确认的领取")

    def _reconcile_jobs_locked(self) -> None:
        """重启后把已转存 / 已完成卡片的结论回写到采集作业，页面上仍能看到停止位置。"""
        for card in self._cards:
            if card.get("status") not in (CARD_ARCHIVED, CARD_COMPLETED):
                continue
            job = self._spider(str(card.get("job_id") or ""))
            if job is None:
                continue
            if card.get("status") == CARD_ARCHIVED:
                position = str(card.get("stop_position") or self._stop_position(card))
                job["停止位置"] = position
                job["status"] = "已失败"
                job["pending"] = False
                job["abnormal"] = True
            else:
                job["status"] = "已完成"
                job["pending"] = False
                job["abnormal"] = False
                job["停止位置"] = ""

    def _persist_locked(self) -> None:
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {"cards": self._cards, "late_receipts": self._late_receipts}
            descriptor, temp_path = tempfile.mkstemp(
                dir=str(self._state_file.parent), suffix=".tmp"
            )
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False, indent=1)
                os.replace(temp_path, self._state_file)
            finally:
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
        except OSError:
            # 落盘失败不阻断内存里的状态流转；页面上仍能看到当前队列。
            pass

    def _seed_locked(self, moment: datetime) -> None:
        """首次启动时准备几条可操作的卡片，覆盖待领取、已冻结、领取中三种形态。"""
        self._cards = [
            {
                "id": 1,
                "job_id": "SPID-0001",
                "status": CARD_PENDING,
                "attempts": 2,
                "max_attempts": 5,
                "stages": [
                    {"name": "列表采集", "state": STAGE_SEALED},
                    {"name": "详情抽取（TEMP-0001）", "state": STAGE_FAILED},
                    {"name": "结果校验", "state": STAGE_PENDING},
                ],
                "backoff": {
                    "algorithm": ALGORITHM_EXPONENTIAL,
                    "initial_seconds": 30,
                    "max_seconds": 900,
                },
                "backoff_source": "RETR-0001",
                "retryable": ["TimeoutError", "ConnectionError", "HTTP 429", "HTTP 503"],
                "failure_store": "failure_archive/SPID-0001",
                "next_claim_at": iso(moment - timedelta(seconds=45)),
                "reason": "详情页连续超时，等待操作员确认代理池后重新领取",
                "created_at": iso(moment - timedelta(minutes=30)),
                "created_by": "值班管理员",
                "updated_at": iso(moment - timedelta(minutes=6)),
            },
            {
                "id": 2,
                "job_id": "SPID-0002",
                "status": CARD_FROZEN,
                "attempts": 1,
                "max_attempts": 3,
                "stages": [
                    {"name": "列表采集", "state": STAGE_SEALED},
                    {"name": "详情抽取（TEMP-0002）", "state": STAGE_FAILED},
                    {"name": "结果校验", "state": STAGE_PENDING},
                ],
                "backoff": {
                    "algorithm": ALGORITHM_FIXED,
                    "initial_seconds": 60,
                    "max_seconds": 60,
                },
                "backoff_source": "RETR-0002",
                "retryable": ["TimeoutError"],
                "failure_store": "failure_archive/SPID-0002",
                "next_claim_at": iso(moment - timedelta(seconds=120)),
                "frozen_by": "值班管理员",
                "frozen_at": iso(moment - timedelta(minutes=4)),
                "freeze_reason": "站点返回验证码，先确认反爬策略是否已切换",
                "created_at": iso(moment - timedelta(minutes=40)),
                "created_by": "值班管理员",
                "updated_at": iso(moment - timedelta(minutes=4)),
            },
            {
                "id": 3,
                "job_id": "SPID-0003",
                "status": CARD_PENDING,
                "attempts": 0,
                "max_attempts": 4,
                "stages": [
                    {"name": "列表采集", "state": STAGE_SEALED},
                    {"name": "详情抽取（TEMP-0003）", "state": STAGE_FAILED},
                    {"name": "结果校验", "state": STAGE_PENDING},
                ],
                "backoff": {
                    "algorithm": ALGORITHM_LINEAR,
                    "initial_seconds": 45,
                    "max_seconds": 300,
                },
                "backoff_source": "RETR-0003",
                "retryable": ["HTTP 503"],
                "failure_store": "failure_archive/SPID-0003",
                "next_claim_at": iso(moment + timedelta(seconds=180)),
                "reason": "首次失败后按线性退避排期，等待承诺保留到下次领取时刻",
                "created_at": iso(moment - timedelta(minutes=12)),
                "created_by": "值班管理员",
                "updated_at": iso(moment - timedelta(minutes=1)),
            },
        ]

    # ------------------------------------------------------------------ 查询辅助

    def _spider(self, job_id: str) -> dict[str, Any] | None:
        job_id = str(job_id or "").strip()
        if not job_id:
            return None
        for row in self._store.rows("spider"):
            if str(row.get("作业编号") or "") == job_id:
                return row
        return None

    def _policy_for(self, job_id: str) -> dict[str, Any] | None:
        for row in self._store.rows("retry"):
            if str(row.get("关联任务") or "") == job_id and row.get("status") != "已停用":
                return row
        return None

    def _card(self, card_id: int) -> dict[str, Any] | None:
        for card in self._cards:
            if _int(card.get("id")) == card_id:
                return card
        return None

    def _next_claim(self, card: dict[str, Any]) -> datetime | None:
        return parse_iso(card.get("next_claim_at"))

    def _remaining(self, card: dict[str, Any]) -> int:
        return max(_int(card.get("max_attempts")) - _int(card.get("attempts")), 0)

    def _job_finished(self, job_id: str) -> bool:
        job = self._spider(job_id)
        return bool(job and str(job.get("status")) in ("已完成", "已失败"))

    # ------------------------------------------------------------------ 退避快照

    @staticmethod
    def _snapshot_backoff(policy: dict[str, Any] | None) -> dict[str, Any]:
        """建卡时冻结退避配置；之后策略改了也沿用它。"""
        algorithm = str((policy or {}).get("退避算法") or ALGORITHM_FIXED).strip()
        if algorithm not in ALGORITHMS:
            algorithm = ALGORITHM_FIXED
        initial = max(_int((policy or {}).get("初始等待秒"), 30), 1)
        maximum = max(_int((policy or {}).get("最大等待秒"), initial), initial)
        return {
            "algorithm": algorithm,
            "initial_seconds": initial,
            "max_seconds": maximum,
        }

    @staticmethod
    def _wait_seconds(snapshot: dict[str, Any], attempt_number: int) -> int:
        algorithm = str(snapshot.get("algorithm") or ALGORITHM_FIXED)
        initial = max(_int(snapshot.get("initial_seconds"), 1), 1)
        maximum = max(_int(snapshot.get("max_seconds"), initial), initial)
        number = max(attempt_number, 1)
        if algorithm == ALGORITHM_LINEAR:
            seconds = initial * number
        elif algorithm == ALGORITHM_EXPONENTIAL:
            seconds = initial * (2 ** (number - 1))
        else:
            seconds = initial
        return min(seconds, maximum)

    def _schedule(self, card: dict[str, Any], moment: datetime, attempt_number: int) -> int:
        snapshot = card.get("backoff") or self._snapshot_backoff(None)
        wait = self._wait_seconds(snapshot, attempt_number)
        card["next_claim_at"] = iso(moment + timedelta(seconds=wait))
        card["wait_seconds"] = wait
        card["status"] = CARD_PENDING
        return wait

    # ------------------------------------------------------------------ 租约

    def _release_lease(self, card: dict[str, Any], reason: str, *, revert: bool) -> list[str]:
        lease = card.get("lease")
        if not lease:
            return []
        released: list[str] = []
        if revert:
            for stage in card.get("stages", []):
                if stage.get("state") == STAGE_RUNNING:
                    stage["state"] = STAGE_FAILED
                    released.append(stage["name"])
        sealed = [stage["name"] for stage in card.get("stages", []) if stage.get("state") == STAGE_SEALED]
        card["last_leased_until"] = lease.get("expires_at")
        card["last_lease_id"] = lease.get("id")
        card["last_executor"] = lease.get("executor")
        card.setdefault("lease_history", []).append(
            {
                "lease_id": lease.get("id"),
                "executor": lease.get("executor"),
                "mode": lease.get("mode"),
                "issued_at": lease.get("issued_at"),
                "expires_at": lease.get("expires_at"),
                "closed_at": iso(self.now()),
                "reason": reason,
                "reverted_stages": released,
                "sealed_stages": sealed,
            }
        )
        card["lease"] = None
        card["lease_expires_at"] = None
        card["updated_at"] = iso(self.now())
        return released

    def _reclaim_expired_locked(self, moment: datetime, reason: str) -> list[dict[str, Any]]:
        reclaimed: list[dict[str, Any]] = []
        for card in self._cards:
            if card.get("status") != CARD_CLAIMED:
                continue
            expires = parse_iso(card.get("lease_expires_at"))
            if expires is None or expires > moment:
                continue
            released = self._release_lease(card, reason, revert=True)
            # 租约一经发放即算消耗一次尝试；回收只回退阶段，不返还次数。
            if self._remaining(card) <= 0:
                self._archive_locked(card, "租约到期未回执且重试次数已耗尽", moment)
                card["reason"] = "租约到期已回收未确认阶段，重试次数耗尽，卡片转入失败转存"
            else:
                card["status"] = CARD_PENDING
                card["next_claim_at"] = iso(moment)
                card["wait_seconds"] = 0
                card["reason"] = "租约到期，已回收未确认的失败阶段；已确认成功的阶段保持封存"
            card["updated_at"] = iso(moment)
            reclaimed.append({"card_id": card.get("id"), "released_stages": released})
        return reclaimed

    # ------------------------------------------------------------------ 输出

    def _decorate(self, card: dict[str, Any]) -> dict[str, Any]:
        moment = self.now()
        expires = parse_iso(card.get("lease_expires_at"))
        next_at = self._next_claim(card)
        stages = card.get("stages", [])
        remaining = self._remaining(card)
        lease_active = (
            card.get("status") == CARD_CLAIMED and expires is not None and expires > moment
        )
        claimable = (
            card.get("status") == CARD_PENDING
            and remaining > 0
            and next_at is not None
            and next_at <= moment
            and not self._job_finished(str(card.get("job_id") or ""))
        )
        seconds_left = max(int((expires - moment).total_seconds()), 0) if lease_active and expires else 0
        wait_seconds = max(int((next_at - moment).total_seconds()), 0) if next_at and next_at > moment else 0
        job = self._spider(str(card.get("job_id") or ""))
        return {
            "id": card.get("id"),
            "job_id": card.get("job_id"),
            "job_status": str(job.get("status") or "") if job else "作业已移除",
            "job_stop_position": str(job.get("停止位置") or "") if job else "",
            "status": card.get("status"),
            "claimable": claimable,
            "frozen": card.get("status") == CARD_FROZEN,
            "frozen_by": card.get("frozen_by"),
            "freeze_reason": card.get("freeze_reason"),
            "frozen_at": card.get("frozen_at"),
            "stages": [
                {"name": stage.get("name"), "state": stage.get("state")}
                for stage in stages
            ],
            "successful_stages": [stage.get("name") for stage in stages if stage.get("state") == STAGE_SEALED],
            "failed_stages": [
                stage.get("name")
                for stage in stages
                if stage.get("state") in (STAGE_FAILED, STAGE_RUNNING)
            ],
            "pending_stages": [stage.get("name") for stage in stages if stage.get("state") == STAGE_PENDING],
            "attempts": _int(card.get("attempts")),
            "max_attempts": _int(card.get("max_attempts")),
            "remaining_attempts": remaining,
            "next_claim_at": card.get("next_claim_at"),
            "wait_seconds": wait_seconds,
            "claim_mode": card.get("claim_mode"),
            "lease": (
                {
                    "id": (card.get("lease") or {}).get("id"),
                    "executor": (card.get("lease") or {}).get("executor"),
                    "mode": (card.get("lease") or {}).get("mode"),
                    "issued_at": (card.get("lease") or {}).get("issued_at"),
                    "expires_at": card.get("lease_expires_at"),
                    "seconds_left": seconds_left,
                    "pending_stages": (card.get("lease") or {}).get("pending_stages", []),
                }
                if lease_active
                else None
            ),
            "backoff": card.get("backoff"),
            "backoff_source": card.get("backoff_source"),
            "retryable": card.get("retryable", []),
            "failure_store": card.get("failure_store"),
            "stop_position": card.get("stop_position") or "",
            "last_leased_until": card.get("last_leased_until"),
            "archive_reason": card.get("archive_reason"),
            "reason": card.get("reason") or "",
            "created_at": card.get("created_at"),
            "created_by": card.get("created_by"),
            "updated_at": card.get("updated_at"),
            "lease_history": card.get("lease_history", []),
            "late_receipts": [
                receipt
                for receipt in self._late_receipts
                if _int(receipt.get("card_id")) == _int(card.get("id"))
            ],
        }

    def overview(self) -> dict[str, Any]:
        with self._lock:
            self._load()
            self._reclaim_expired_locked(self.now(), reason="读取前回收过期租约")
            counts = {status: 0 for status in CARD_STATUSES}
            claimable = 0
            for card in self._cards:
                status = str(card.get("status") or CARD_PENDING)
                counts[status] = counts.get(status, 0) + 1
                if self._decorate(card)["claimable"]:
                    claimable += 1
            return {
                "counts": counts,
                "total": len(self._cards),
                "claimable_now": claimable,
                "late_receipts": len(self._late_receipts),
                "policy": {
                    "algorithms": ALGORITHMS,
                    "claim_modes": CLAIM_MODES,
                    "lease_seconds_default": LEASE_DEFAULT_SECONDS,
                    "lease_seconds_min": LEASE_MIN_SECONDS,
                    "lease_seconds_max": LEASE_MAX_SECONDS,
                },
            }

    def list_cards(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 50,
    ) -> dict[str, Any]:
        with self._lock:
            self._load()
            self._reclaim_expired_locked(self.now(), reason="读取前回收过期租约")
            cards = list(self._cards)
            if status:
                cards = [card for card in cards if card.get("status") == status]
            if keyword:
                cards = [
                    card
                    for card in cards
                    if keyword in str(card.get("job_id") or "") or keyword in str(card.get("id"))
                ]
            # 在途卡片按到期时间排；已完成/已转存沉到最后，避免抢占操作员视线。
            cards.sort(
                key=lambda card: (
                    card.get("status") in (CARD_COMPLETED, CARD_ARCHIVED),
                    parse_iso(card.get("next_claim_at")) or EPOCH,
                    _int(card.get("id")),
                )
            )
            items = [self._decorate(card) for card in cards]
            start = max(page - 1, 0) * size
            return {
                "items": items[start:start + size],
                "total": len(items),
                "page": page,
                "size": size,
            }

    def get_card(self, card_id: int) -> dict[str, Any] | None:
        with self._lock:
            self._load()
            self._reclaim_expired_locked(self.now(), reason="读取前回收过期租约")
            card = self._card(card_id)
            return self._decorate(card) if card else None

    def late_receipts(self) -> list[dict[str, Any]]:
        with self._lock:
            self._load()
            return list(self._late_receipts)

    # ------------------------------------------------------------------ 建卡

    def create_card(
        self, values: dict[str, Any], operator: str = "值班管理员"
    ) -> tuple[dict[str, Any] | None, str]:
        with self._lock:
            self._load()
            job_id = str(values.get("job_id") or values.get("关联作业") or "").strip()
            if not job_id:
                return None, "缺少关联采集作业"
            job = self._spider(job_id)
            if job is None:
                return None, f"采集作业 {job_id} 不存在或已归档，不能建重放卡片"
            policy = self._policy_for(job_id)
            if policy is None:
                return None, f"采集作业 {job_id} 没有可用的重试策略（缺失或已停用），不能建重放卡片"
            in_flight = [
                card
                for card in self._cards
                if card.get("job_id") == job_id
                and card.get("status") in (CARD_PENDING, CARD_FROZEN, CARD_CLAIMED)
            ]
            if in_flight:
                return None, f"采集作业 {job_id} 已有在途重放卡片 #{in_flight[0].get('id')}，不重复建卡"
            stages = stage_definitions(job)
            failed_stage = str(values.get("failed_stage") or "").strip()
            sealed = {str(item).strip() for item in values.get("successful_stages") or []}
            pieces = []
            for name in stages:
                if name in sealed:
                    state = STAGE_SEALED
                elif failed_stage and name == failed_stage:
                    state = STAGE_FAILED
                else:
                    state = STAGE_PENDING
                pieces.append({"name": name, "state": state})
            max_attempts = max(
                _int(values.get("max_attempts"), _int(policy.get("最大重试次数"), 1)), 1
            )
            moment = self.now()
            card: dict[str, Any] = {
                "id": max((_int(row.get("id")) for row in self._cards), default=0) + 1,
                "job_id": job_id,
                "status": CARD_PENDING,
                "attempts": 0,
                "max_attempts": max_attempts,
                "stages": pieces,
                "backoff": self._snapshot_backoff(policy),
                "backoff_source": str(policy.get("策略编号") or ""),
                "retryable": split_retryable(policy.get("可重试异常")),
                "failure_store": str(policy.get("失败转存") or ""),
                "created_at": iso(moment),
                "created_by": operator,
                "updated_at": iso(moment),
                "reason": "新卡片按当前退避配置排期",
            }
            wait = self._schedule(card, moment, 1)
            self._cards.append(card)
            self._persist_locked()
            return self._decorate(card), (
                f"重放卡片 #{card['id']} 已进入队列，按{card['backoff']['algorithm']}等待 "
                f"{wait} 秒，最早可领取于 {card['next_claim_at']}"
            )

    # ------------------------------------------------------------------ 动作

    def run_action(
        self,
        card_id: int,
        action: str,
        payload: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        payload = payload or {}
        with self._lock:
            self._load()
            moment = self.now()
            self._reclaim_expired_locked(moment, reason="动作前回收过期租约")
            card = self._card(card_id)
            if card is None:
                return None, f"重放卡片 {card_id} 不存在或已归档"
            operator = str(payload.get("operator") or "值班管理员").strip() or "值班管理员"
            if action == ACTION_FREEZE:
                return self._freeze(card, operator, payload, moment)
            if action == ACTION_UNFREEZE:
                return self._unfreeze(card, operator, moment)
            if action == ACTION_CLAIM:
                return self._claim(card, payload, moment)
            if action == ACTION_REPORT:
                return self._report(card, payload, moment)
            if action == ACTION_RECLAIM:
                reclaimed = self._reclaim_expired_locked(moment, reason="人工触发回收过期租约")
                if not reclaimed:
                    return self._decorate(card), "当前没有过期租约需要回收"
                self._persist_locked()
                return self._decorate(card), f"已回收 {len(reclaimed)} 张过期租约，只回退未确认的失败阶段"
            return None, f"动作「{action}」不属于失败重放队列可执行范围"

    def _freeze(
        self,
        card: dict[str, Any],
        operator: str,
        payload: dict[str, Any],
        moment: datetime,
    ) -> tuple[dict[str, Any], str]:
        if card.get("status") == CARD_FROZEN:
            return self._decorate(card), "卡片已经是冻结状态"
        if card.get("status") in (CARD_COMPLETED, CARD_ARCHIVED):
            return self._decorate(card), f"卡片{card.get('status')}，不能再冻结"
        if card.get("status") == CARD_CLAIMED:
            return self._decorate(card), "卡片正被执行者领取，租约结束前不能冻结"
        card["status"] = CARD_FROZEN
        card["frozen_by"] = operator
        card["frozen_at"] = iso(moment)
        card["freeze_reason"] = str(payload.get("reason") or "操作员先冻结检查原因").strip()
        card["updated_at"] = iso(moment)
        self._persist_locked()
        return self._decorate(card), "卡片已冻结，检查原因并解除冻结后执行者才能领取"

    def _unfreeze(
        self, card: dict[str, Any], operator: str, moment: datetime
    ) -> tuple[dict[str, Any], str]:
        if card.get("status") != CARD_FROZEN:
            return self._decorate(card), "只有已冻结的卡片可以解除冻结"
        card["status"] = CARD_PENDING
        card["frozen_by"] = None
        card["frozen_at"] = None
        card["freeze_reason"] = None
        card["unfrozen_by"] = operator
        next_at = self._next_claim(card)
        if next_at is None or next_at < moment:
            # 冻结期间到期的卡片，解除冻结后立即可以领取；未到期的仍保留原等待承诺。
            card["next_claim_at"] = iso(moment)
            card["wait_seconds"] = 0
        card["updated_at"] = iso(moment)
        self._persist_locked()
        return self._decorate(card), "卡片已解除冻结，执行者可以按到期时间领取"

    def _claim(
        self, card: dict[str, Any], payload: dict[str, Any], moment: datetime
    ) -> tuple[dict[str, Any], str]:
        executor = str(payload.get("executor") or "").strip()
        if not executor:
            return self._decorate(card), "领取必须带上执行者标识，否则无法回收租约"
        mode = str(payload.get("mode") or MODE_MANUAL).strip()
        if mode not in CLAIM_MODES:
            return self._decorate(card), f"领取方式「{mode}」不在允许范围：{'、'.join(CLAIM_MODES)}"
        if card.get("status") == CARD_FROZEN:
            return self._decorate(card), "卡片已冻结，请操作员先解除冻结"
        if card.get("status") == CARD_CLAIMED:
            lease = card.get("lease") or {}
            return self._decorate(card), (
                f"卡片已被执行者 {lease.get('executor')} 以{lease.get('mode')}方式领取，"
                "本次尝试不重复发放"
            )
        if card.get("status") in (CARD_COMPLETED, CARD_ARCHIVED):
            return self._decorate(card), f"卡片{card.get('status')}，不再发放领取"
        if self._remaining(card) <= 0:
            return self._decorate(card), "重试次数已耗尽，卡片应转入失败转存"
        next_at = self._next_claim(card)
        if next_at is None or next_at > moment:
            return self._decorate(card), f"尚未到下次允许领取时刻 {card.get('next_claim_at') or '—'}"
        if self._job_finished(str(card.get("job_id") or "")):
            return self._decorate(card), "关联采集作业已完成或已失败，不能领取"
        lease_seconds = _int(payload.get("lease_seconds"), LEASE_DEFAULT_SECONDS)
        lease_seconds = min(max(lease_seconds, LEASE_MIN_SECONDS), LEASE_MAX_SECONDS)
        expires = moment + timedelta(seconds=lease_seconds)
        pending: list[str] = []
        for stage in card.get("stages", []):
            if stage.get("state") == STAGE_FAILED:
                stage["state"] = STAGE_RUNNING
                pending.append(stage["name"])
        if not pending:
            for stage in card.get("stages", []):
                if stage.get("state") == STAGE_PENDING:
                    stage["state"] = STAGE_RUNNING
                    pending.append(stage["name"])
                    break
        if not pending:
            return self._decorate(card), "所有阶段都已确认成功，没有需要重放的阶段"
        card["status"] = CARD_CLAIMED
        card["attempts"] = _int(card.get("attempts")) + 1
        card["claim_mode"] = mode
        card["lease_expires_at"] = iso(expires)
        card["lease"] = {
            "id": secrets.token_hex(6),
            "executor": executor,
            "mode": mode,
            "issued_at": iso(moment),
            "expires_at": iso(expires),
            "pending_stages": pending,
            "sealed_stages": [
                stage["name"]
                for stage in card.get("stages", [])
                if stage.get("state") == STAGE_SEALED
            ],
        }
        card["updated_at"] = iso(moment)
        self._persist_locked()
        return self._decorate(card), (
            f"执行者 {executor} 以{mode}方式领取第 {card['attempts']} 次尝试，"
            f"租约有效期 {lease_seconds} 秒；已确认成功的阶段保持封存"
        )

    def _report(
        self, card: dict[str, Any], payload: dict[str, Any], moment: datetime
    ) -> tuple[dict[str, Any], str]:
        lease = card.get("lease") or {}
        executor = str(payload.get("executor") or "").strip()
        lease_id = str(payload.get("lease_id") or "").strip()
        outcome = str(payload.get("outcome") or "").strip()
        stage_name = str(payload.get("stage") or "").strip()
        error = str(payload.get("error") or "").strip()
        expires = parse_iso(card.get("lease_expires_at"))
        expired = (
            card.get("status") != CARD_CLAIMED
            or expires is None
            or expires <= moment
            or (lease_id and lease_id != lease.get("id"))
        )
        # 租约仍然有效但不是当前持有者的回执：不能冒充持有者，也不构成迟到证据。
        foreign = (
            not expired
            and executor
            and lease
            and executor != lease.get("executor")
        )
        if foreign:
            return self._decorate(card), (
                f"卡片当前由执行者 {lease.get('executor')} 持有租约，{executor} 不能代为上报"
            )
        stale = expired
        if stale:
            receipt = {
                "id": max((_int(item.get("id")) for item in self._late_receipts), default=0) + 1,
                "card_id": card.get("id"),
                "job_id": card.get("job_id"),
                "lease_id": lease_id or lease.get("id") or card.get("last_lease_id"),
                "executor": executor or lease.get("executor") or card.get("last_executor") or "未知执行者",
                "outcome": outcome or "未知",
                "stage": stage_name or None,
                "error": error or None,
                "received_at": iso(moment),
                "expected_expires_at": card.get("last_leased_until"),
                "note": "回执到达时租约已过期或被回收，只作为迟到证据留存",
            }
            self._late_receipts.append(receipt)
            self._persist_locked()
            return self._decorate(card), (
                f"回执已作为迟到证据留存（卡片 #{card.get('id')}）；卡片状态不变，"
                "未确认的失败阶段仍等待重新领取"
            )
        if outcome not in OUTCOMES:
            return self._decorate(card), f"回执结果「{outcome or '空'}」必须为{'或'.join(OUTCOMES)}"
        stages = card.get("stages", [])
        if not stage_name:
            running = [stage["name"] for stage in stages if stage.get("state") == STAGE_RUNNING]
            stage_name = running[0] if running else ""
        target = next((stage for stage in stages if stage.get("name") == stage_name), None)
        if target is None:
            return self._decorate(card), f"阶段「{stage_name or '空'}」不属于这张卡片"
        if target.get("state") == STAGE_SEALED:
            return self._decorate(card), f"阶段「{stage_name}」已确认成功并封存，不能再改"
        if outcome == OUTCOME_SUCCESS:
            target["state"] = STAGE_SEALED
            lease = card.get("lease") or {}
            lease["pending_stages"] = [
                stage["name"]
                for stage in stages
                if stage.get("state") in (STAGE_RUNNING, STAGE_FAILED)
            ]
            unfinished = [stage for stage in stages if stage.get("state") != STAGE_SEALED]
            if not unfinished:
                if card.get("lease"):
                    self._release_lease(card, "all_stages_success", revert=False)
                card["status"] = CARD_COMPLETED
                card["next_claim_at"] = None
                card["wait_seconds"] = 0
                card["reason"] = "全部阶段确认成功，已封存归档"
                card["updated_at"] = iso(moment)
                job = self._spider(str(card.get("job_id") or ""))
                if job is not None:
                    job["status"] = "已完成"
                    job["pending"] = False
                    job["abnormal"] = False
                    job["停止位置"] = ""
                self._persist_locked()
                return self._decorate(card), "全部阶段已确认成功并封存，卡片归档为已完成"
            if self._remaining(card) <= 0:
                self._archive_locked(card, "重试次数已耗尽", moment)
                self._persist_locked()
                return self._decorate(card), (
                    f"阶段「{stage_name}」已确认成功并封存，但重试次数已耗尽，卡片转入失败转存"
                )
            self._persist_locked()
            return self._decorate(card), (
                f"阶段「{stage_name}」已确认成功并封存；租约仍然有效，请继续上报剩余阶段"
            )
        target["state"] = STAGE_FAILED
        released = self._release_lease(card, "failure", revert=True)
        remaining = self._remaining(card)
        if remaining <= 0:
            self._archive_locked(card, "重试次数已耗尽", moment)
            self._persist_locked()
            return self._decorate(card), (
                "重试次数已耗尽，卡片转入失败转存；采集作业已记录停止位置"
            )
        retryable = card.get("retryable") or []
        if retryable and not any(item and item.lower() in error.lower() for item in retryable):
            self._archive_locked(
                card,
                f"异常不在可重试集合：{error or '未提供异常信息'}",
                moment,
            )
            self._persist_locked()
            return self._decorate(card), (
                "异常不在许可集合中，卡片转入失败转存；采集作业已记录停止位置，不从头重跑"
            )
        wait = self._schedule(card, moment, _int(card.get("attempts")) + 1)
        card["reason"] = f"阶段「{stage_name}」失败，按{card['backoff']['algorithm']}退避"
        card["updated_at"] = iso(moment)
        self._persist_locked()
        return self._decorate(card), (
            f"失败阶段「{stage_name}」已回收，按{card['backoff']['algorithm']}等待 {wait} 秒后可再次领取；"
            "已确认成功的阶段保持封存"
        )

    def _archive_locked(
        self, card: dict[str, Any], reason: str, moment: datetime
    ) -> None:
        if card.get("lease"):
            self._release_lease(card, "archived", revert=True)
        card["status"] = CARD_ARCHIVED
        card["archive_reason"] = reason
        card["stop_position"] = self._stop_position(card)
        card["next_claim_at"] = None
        card["wait_seconds"] = 0
        card["updated_at"] = iso(moment)
        job = self._spider(str(card.get("job_id") or ""))
        if job is not None:
            job["停止位置"] = card["stop_position"]
            job["status"] = "已失败"
            job["pending"] = False
            job["abnormal"] = True

    def _stop_position(self, card: dict[str, Any]) -> str:
        failed = [
            stage["name"]
            for stage in card.get("stages", [])
            if stage.get("state") in (STAGE_FAILED, STAGE_RUNNING, STAGE_PENDING)
        ]
        sealed = [
            stage["name"]
            for stage in card.get("stages", [])
            if stage.get("state") == STAGE_SEALED
        ]
        position = "停止于：" + "、".join(failed) if failed else "无未完成阶段"
        if sealed:
            position += "；已封存成功阶段：" + "、".join(sealed)
        return position

    # ------------------------------------------------------------------ 定时领取

    def scheduled_claim(
        self, executor: str, limit: int = 5, lease_seconds: int = LEASE_DEFAULT_SECONDS
    ) -> dict[str, Any]:
        """定时领取：与人工立即领取共用同一把锁，抢先领取的只有一方。"""
        executor = executor.strip()
        with self._lock:
            self._load()
            moment = self.now()
            self._reclaim_expired_locked(moment, reason="定时领取前回收过期租约")
            if not executor:
                return {"ok": False, "message": "定时领取必须带上执行者标识", "claimed": [], "skipped": []}
            claimed: list[dict[str, Any]] = []
            skipped: list[dict[str, Any]] = []
            for card in sorted(
                self._cards,
                key=lambda item: (
                    parse_iso(item.get("next_claim_at")) or EPOCH,
                    _int(item.get("id")),
                ),
            ):
                if len(claimed) >= max(limit, 1):
                    break
                if not self._decorate(card)["claimable"]:
                    continue
                updated, message = self._claim(
                    card,
                    {"executor": executor, "mode": MODE_SCHEDULED, "lease_seconds": lease_seconds},
                    moment,
                )
                if updated.get("status") == CARD_CLAIMED:
                    claimed.append({"card_id": updated.get("id"), "lease": updated.get("lease")})
                else:
                    skipped.append({"card_id": updated.get("id"), "message": message})
            self._persist_locked()
            if not claimed:
                return {
                    "ok": False,
                    "message": "没有到期的可领取卡片，或已被其他执行者抢先领取",
                    "claimed": [],
                    "skipped": skipped,
                }
            return {
                "ok": True,
                "message": f"定时领取已为 {executor} 发放 {len(claimed)} 张租约",
                "claimed": claimed,
                "skipped": skipped,
            }


replay_queue = ReplayQueueService()
