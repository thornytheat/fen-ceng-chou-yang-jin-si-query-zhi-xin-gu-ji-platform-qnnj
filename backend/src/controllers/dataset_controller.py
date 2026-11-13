"""数据集 controller：列表 / 详情 / 分层 / 精度标准 / 数据快照刷新与历史。"""
from __future__ import annotations

from sqlalchemy.orm import Session

from src.services import dataset_service, snapshot_service
from src.utils.errors import AppError


def list_datasets(db: Session) -> dict:
    items = [dataset_service.dataset_out(db, row) for row in dataset_service.list_datasets(db)]
    return {"items": items, "total": len(items)}


def get_dataset(db: Session, dataset_id: int) -> dict:
    dataset = dataset_service.get_dataset(db, dataset_id)
    return dataset_service.dataset_out(db, dataset, with_strata=True)


def get_strata(db: Session, dataset_id: int) -> dict:
    dataset = dataset_service.get_dataset(db, dataset_id)
    detail = dataset_service.dataset_out(db, dataset, with_strata=True)
    return {"items": detail["strata"], "total": len(detail["strata"])}


def list_snapshots(db: Session, dataset_id: int) -> dict:
    dataset = dataset_service.get_dataset(db, dataset_id)
    rows = snapshot_service.list_snapshots(db, dataset.id)
    items = [snapshot_service.snapshot_out(row) for row in rows]
    return {"items": items, "total": len(items)}


def patch_dataset(db: Session, dataset_id: int, payload, actor: dict, ip: str | None = None) -> dict:
    dataset_service.patch_dataset(db, dataset_id, payload, actor, ip)
    db.commit()
    return dataset_service.dataset_out(db, dataset_service.get_dataset(db, dataset_id), with_strata=True)


def refresh_snapshot(db: Session, dataset_id: int, actor: dict, ip: str | None = None) -> dict:
    dataset = dataset_service.get_dataset(db, dataset_id)
    if dataset.row_count <= 0:
        raise AppError("DATASET_EMPTY", 422)
    task_id = None
    queued = False
    try:
        from src.tasks import refresh_snapshot_task

        async_result = refresh_snapshot_task.delay(dataset_id)
        task_id = getattr(async_result, "id", None)
        queued = True
    except Exception:  # noqa: BLE001 - broker 不可用时同步兜底
        queued = False
    if not queued:
        outcome = dataset_service.publish_refresh_snapshot(
            db,
            dataset_id,
            actor_email=actor["email"],
            actor_role=actor["role"],
            ip=ip,
        )
        db.commit()
        return {
            "task_id": None,
            "queued": False,
            "message": "消息队列不可用，已同步发布数据快照",
            "snapshot_id": outcome["snapshot_id"],
            "snapshot_code": outcome["snapshot_code"],
            "version": outcome["version"],
            "reused": outcome["reused"],
        }
    return {
        "task_id": task_id,
        "queued": queued,
        "message": "已投递后台快照刷新任务",
        "snapshot_id": None,
        "snapshot_code": None,
        "version": None,
        "reused": None,
    }


def refresh_truth(db: Session, dataset_id: int, actor: dict, ip: str | None = None) -> dict:
    """向后兼容：旧的刷新真值入口等价于刷新发布快照。"""
    return refresh_snapshot(db, dataset_id, actor, ip)
