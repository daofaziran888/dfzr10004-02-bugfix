"""采集任务编排接口：维护采集计划，覆盖启动、执行、暂停、恢复、归档等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.lifecycle import LifecycleConflict
from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.taskplan import OPERATOR_ROLE, SPEC, TaskplanService

router = APIRouter(prefix="/api/taskplan", tags=["采集任务编排"])

service = TaskplanService()

LIST_FIELDS = ["计划编号", "目标站点", "采集频率", "数据范围", "优先级", "资源配额", "预计用时", "计划状态"]
STATUSES = list(SPEC.stages)


def _payload_kwargs(payload: EntryPayload) -> dict[str, Any]:
    values = payload.values
    kwargs: dict[str, Any] = {
        "operator": str(values.get("operator") or "值班管理员"),
        "note": values.get("note") or (payload.remark or None),
    }
    if values.get("expected_version") is not None:
        kwargs["expected_version"] = int(values["expected_version"])
    if values.get("at") is not None:
        kwargs["at"] = int(values["at"])
    return kwargs


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按计划编号检索"),
    status: str | None = Query(default=None, description="草稿、待执行、执行中、已暂停、已归档"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按计划编号与阶段过滤采集计划；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    if status and status not in STATUSES:
        raise HTTPException(status_code=400, detail=f"阶段「{status}」不在允许的生命周期里")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/lifecycle/options")
def lifecycle_options() -> dict[str, Any]:
    """生命周期视图元数据：阶段序列、流转规则与各阶段允许的动作。"""
    return {
        "stages": list(SPEC.stages),
        "transitions": [
            {"action": action, "from": from_stage, "to": to_stage}
            for action, (from_stage, to_stage) in SPEC.transitions.items()
        ],
        "allowed_actions": {stage: SPEC.allowed_actions(stage) for stage in SPEC.stages},
        "resume_role": OPERATOR_ROLE,
    }


@router.get("/dispatchable")
def dispatchable_plans() -> dict[str, Any]:
    """校验/下发入口共用：仅返回执行中、允许继续下发任务的计划。"""
    items = service.dispatchable_plans()
    return {"module": "taskplan", "total": len(items), "items": items}


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出采集任务编排清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "taskplan", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条采集计划明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"采集计划 {entry_id} 不存在或已归档")
    return entry


@router.get("/{entry_id}/lifecycle", response_model=dict)
def lifecycle_view(entry_id: int) -> dict:
    """计划生命周期视图：阶段轨迹、每次变更注明的当前阶段与配额台账。"""
    view = service.lifecycle_view(entry_id)
    if view is None:
        raise HTTPException(status_code=404, detail=f"采集计划 {entry_id} 不存在或已归档")
    return view


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条采集计划，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="采集计划已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条采集计划执行动作；跳级、退回、终态再操作一律拒绝并说明原因。

    携带 expected_version 时做乐观并发控制：两个入口并发提交同一请求，
    只有一个能推进成功，另一个收到 409；重复请求因当前阶段已不满足前置条件而幂等拒绝。
    """
    action = str(payload.values.get("action") or "").strip()
    try:
        entry, message = service.run_action(
            entry_id,
            action,
            operator_role=payload.values.get("operator_role"),
            **_payload_kwargs(payload),
        )
    except LifecycleConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IllegalTransition as exc:
        return ActionResult(ok=False, message=str(exc))
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
