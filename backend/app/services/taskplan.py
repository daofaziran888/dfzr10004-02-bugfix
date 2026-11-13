"""采集任务编排业务规则。

生命周期、并发推进、配额台账与下游校验联动全部收口到这里，
计划动作、列表取数、校验入口共用同一条状态读取与推进路径。
"""
from __future__ import annotations

from typing import Any

from app.lifecycle import (
    IllegalTransition,
    LifecycleConflict,
    LifecycleSpec,
    TASKPLAN_SPEC,
    apply_transition,
    is_abnormal,
    is_pending,
    now_ts,
)
from app.store import store

MODULE = "taskplan"
REQUIRED_FIELDS = ["计划编号", "目标站点", "采集频率"]
SPEC: LifecycleSpec = TASKPLAN_SPEC

# 只有执行中的计划才允许继续下发采集任务，校验入口也共用这一判定。
DISPATCHABLE_STAGE = "执行中"
# 撤销暂停（已暂停 -> 执行中）仅对运营管理员开放。
RESUME_ACTION = "恢复执行"
OPERATOR_ROLE = "运营管理员"

# 每次合法流转在配额台账上留下的记录类型；暂停只冻结、不删除，修复配额丢失。
QUOTA_HOOKS: dict[str, tuple[str, str]] = {
    "启动计划": ("配额预留", "计划进入待执行，按资源配额预占额度"),
    "开始执行": ("配额生效", "计划开始执行，配额进入实际消耗"),
    "暂停计划": ("配额冻结", "计划挂起，已耗配额保留、停止继续消耗"),
    "恢复执行": ("配额解冻", "撤销暂停，配额恢复消耗"),
    "归档计划": ("配额结清", "计划归档，剩余配额释放并封存台账"),
}


class TaskplanService:
    # ---------- 统一读取路径 ----------
    def _serialize(self, entry: dict[str, Any]) -> dict[str, Any]:
        """列表/详情/导出共用的出口：状态只认状态机，按钮只给允许的动作。"""
        view = dict(entry)
        view["计划状态"] = entry["status"]
        view["allowed_actions"] = SPEC.allowed_actions(str(entry.get("status")))
        view["pending"] = is_pending(entry, SPEC)
        view["abnormal"] = is_abnormal(entry, SPEC)
        return view

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.snapshot(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("计划编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [self._serialize(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find_snapshot(MODULE, entry_id)
        return self._serialize(entry) if entry is not None else None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        with store.locked(MODULE):
            entry: dict[str, Any] = {"id": store.next_id(MODULE)}
            entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
            # 其余展示字段原样保留，没有就留空，不再写入样本文案。
            for extra in ("数据范围", "优先级", "资源配额", "预计用时"):
                entry[extra] = values.get(extra)
            entry["status"] = SPEC.stages[0]
            entry["version"] = 1
            entry["updated_at"] = now_ts()
            entry["history"] = []
            entry["quota_records"] = [
                {
                    "seq": 1,
                    "type": "配额登记",
                    "label": f"登记资源配额：{entry.get('资源配额') or '未配置'}",
                    "at": entry["updated_at"],
                    "version": 1,
                }
            ]
            entry["history"] = [
                {
                    "seq": 1,
                    "action": "登记计划",
                    "from_stage": None,
                    "to_stage": SPEC.stages[0],
                    "at": entry["updated_at"],
                    "operator": str(values.get("operator") or "值班管理员"),
                    "note": "计划登记，进入草稿",
                    "version": 1,
                }
            ]
            entry["pending"] = True
            entry["abnormal"] = False
            store.rows(MODULE).append(entry)
            return self._serialize(entry), []

    # ---------- 统一推进路径 ----------
    def _ensure_quota_ledger(self, entry: dict[str, Any]) -> list[dict[str, Any]]:
        """历史样本没有配额台账时补一条期初记录，后续流转只追加、不覆盖。"""
        ledger = entry.setdefault("quota_records", [])
        if not ledger:
            ledger.append({
                "seq": 1,
                "type": "配额登记",
                "label": f"历史计划配额期初：{entry.get('资源配额') or '未配置'}",
                "at": int(entry.get("updated_at") or now_ts()),
                "version": int(entry.get("version", 1)),
            })
        return ledger

    def run_action(
        self,
        entry_id: int,
        action: str,
        *,
        expected_version: int | None = None,
        operator: str = "值班管理员",
        operator_role: str | None = None,
        note: str | None = None,
        at: int | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        # 延迟导入避免与校验服务形成模块级循环依赖。
        from app.services.verify import verify_service

        with store.locked(MODULE):
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"采集计划 {entry_id} 不存在或已归档"
            if action == RESUME_ACTION and operator_role != OPERATOR_ROLE:
                return None, "仅运营管理员可撤销暂停（恢复执行）"

            ledger = self._ensure_quota_ledger(entry)

            def quota_hook(current: dict[str, Any], event: dict[str, Any]) -> None:
                kind, label = QUOTA_HOOKS[event["action"]]
                ledger.append({
                    "seq": len(ledger) + 1,
                    "type": kind,
                    "label": label,
                    "at": event["at"],
                    "version": event["version"],
                })

            try:
                event = apply_transition(
                    entry,
                    SPEC,
                    action,
                    expected_version=expected_version,
                    operator=operator,
                    note=note,
                    at=at,
                    hook=quota_hook,
                )
            except LifecycleConflict as exc:
                raise exc
            except IllegalTransition as exc:
                return None, str(exc)

            # 下游入口联动：校验清单随计划阶段同步待处理项与状态回写。
            verify_service.sync_plan_event(entry, event)
            return self._serialize(entry), f"采集计划已{action}，当前阶段：{entry['status']}"

    # ---------- 生命周期视图 ----------
    def lifecycle_view(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find_snapshot(MODULE, entry_id)
        if entry is None:
            return None
        current = str(entry.get("status"))
        return {
            "entry": self._serialize(entry),
            "stages": list(SPEC.stages),
            "current_stage": current,
            "current_index": SPEC.stage_index(current),
            "allowed_actions": SPEC.allowed_actions(current),
            "is_terminal": SPEC.is_terminal(current),
            "history": list(entry.get("history", [])),
            "quota_records": list(entry.get("quota_records", [])),
        }

    # ---------- 校验入口共用的下发判定 ----------
    def dispatchable_plans(self) -> list[dict[str, Any]]:
        """仅执行中的计划可继续下发任务；已暂停/已归档在此被统一挡下。"""
        rows = store.snapshot(MODULE)
        return [self._serialize(row) for row in rows if row.get("status") == DISPATCHABLE_STAGE]


# 模块级单例：verify 等下游模块延迟导入，共用同一份计划状态读取口径。
taskplan_service = TaskplanService()
