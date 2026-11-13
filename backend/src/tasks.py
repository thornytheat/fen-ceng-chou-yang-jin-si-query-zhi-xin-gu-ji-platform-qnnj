"""Celery 任务：心跳 + 数据快照刷新（整版重算层容量/均值/波动并原子发布）。"""
from __future__ import annotations

import time

from src.config.celery_app import TASK_HEARTBEAT, TASK_REFRESH_SNAPSHOT, TASK_REFRESH_TRUTH, celery_app
from src.config.settings import settings
from src.utils.logger import get_logger

logger = get_logger("tasks")


def _redis():
    import redis

    return redis.Redis.from_url(settings.redis_url, decode_responses=True)


@celery_app.task(name=TASK_HEARTBEAT)
def heartbeat_task() -> str:
    try:
        _redis().set("sampleaqp:worker:heartbeat", f"{time.time():.3f}", ex=300)
    except Exception as exc:  # noqa: BLE001
        logger.warning("heartbeat failed: %s", exc)
    return "ok"


def _refresh_snapshots(dataset_id: int | None) -> dict:
    from sqlalchemy import select

    from src.config.database import session_scope
    from src.models.dataset import Dataset
    from src.services import dataset_service

    results: list[dict] = []
    # 每个数据集一个独立事务：单个失败不影响其它数据集，且失败整体回滚不留半套状态
    with session_scope() as db:
        if dataset_id is None:
            ids = [row.id for row in db.scalars(select(Dataset))]
        else:
            ids = [int(dataset_id)]
        for current in ids:
            outcome = dataset_service.publish_refresh_snapshot(
                db, current, actor_email="celery", actor_role="system"
            )
            outcome["dataset_id"] = current
            results.append(outcome)
            logger.info(
                "snapshot refreshed dataset=%s version=%s reused=%s",
                current,
                outcome.get("version"),
                outcome.get("reused"),
            )
    return {"snapshots": results}


@celery_app.task(name=TASK_REFRESH_SNAPSHOT, bind=True, max_retries=1)
def refresh_snapshot_task(self, dataset_id: int | None = None) -> dict:
    """重算数据集的整版层口径与全量真值，发布为不可变快照（worker 主要业务职责）。"""
    return _refresh_snapshots(dataset_id)


# 旧任务名保留为别名，避免已有部署 / 队列里残留的 refresh_truth 消息找不到消费者
@celery_app.task(name=TASK_REFRESH_TRUTH, bind=True, max_retries=1)
def refresh_truth_task(self, dataset_id: int | None = None) -> dict:
    return _refresh_snapshots(dataset_id)
