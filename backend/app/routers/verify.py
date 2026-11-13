"""采集结果校验接口：校验规则生命周期 + 计划联动的校验任务清单。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.lifecycle import LifecycleConflict, TASK_STATUSES, VERIFY_SPEC
from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.verify import VerifyService

router = APIRouter(prefix="/api/verify", tags=["采集结果校验"])

service = VerifyService()

LIST_FIELDS = ["规则编号", "校验对象", "校验类型", "校验表达式", "阈值配置", "采样比例", "阻断级别", "最近校验"]
STATUSES = list(VERIFY_SPEC.stages)


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按规则编号检索"),
    status: str | None = Query(default=None, description="草稿、运行中、已暂停、已归档"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按规则编号与阶段过滤采集结果校验列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    if status and status not in STATUSES:
        raise HTTPException(status_code=400, detail=f"阶段「{status}」不在允许的生命周期里")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/tasks")
def list_tasks(
    keyword: str | None = Query(default=None, description="按计划编号检索"),
    status: str | None = Query(default=None, description="校验清单状态"),
    pending_only: bool = Query(default=False, description="仅看待处理/未结案项"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """采集结果校验清单：计划启动产生的待处理项与状态回写都在这里。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    if status and status not in TASK_STATUSES:
        raise HTTPException(status_code=400, detail=f"清单状态「{status}」不被支持")
    items, total = service.list_tasks(
        keyword=keyword, status=status, pending_only=pending_only, page=page, size=size
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/dispatchable")
def dispatchable_plans() -> dict[str, Any]:
    """下发任务前的统一判定：已暂停、已归档的计划不会出现在结果里。"""
    items = service.dispatchable_plans()
    return {"module": "taskplan", "total": len(items), "items": items}


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出采集结果校验清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "verify", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条校验规则明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"校验规则 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条校验规则，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="校验规则已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条校验规则执行启动、暂停、恢复、归档；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    try:
        entry, message = service.run_action(entry_id, action, **_rule_kwargs(payload))
    except LifecycleConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IllegalTransition as exc:
        return ActionResult(ok=False, message=str(exc))
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/tasks/{task_id}/result", response_model=ActionResult)
def record_task_result(task_id: int, payload: EntryPayload) -> ActionResult:
    """校验结果回写清单；冲突时以时间戳更晚且校验通过的版本为准。"""
    values = payload.values
    if "passed" not in values:
        return ActionResult(ok=False, message="缺少必填字段：passed（true/false）")
    try:
        task, message = service.record_result(
            task_id,
            passed=bool(values["passed"]),
            summary=str(values.get("summary") or "未填写校验结论"),
            operator=str(values.get("operator") or "值班管理员"),
            expected_version=int(values["expected_version"]) if values.get("expected_version") is not None else None,
            at=int(values["at"]) if values.get("at") is not None else None,
        )
    except LifecycleConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if task is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=task)


def _rule_kwargs(payload: EntryPayload) -> dict[str, Any]:
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
