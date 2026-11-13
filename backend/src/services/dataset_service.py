"""数据集：分层档案、精度标准维护、数据快照刷新。"""
from __future__ import annotations

import time
from decimal import Decimal

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from src.models.approx_query import ApproxQuery
from src.models.dataset import Dataset
from src.models.sample_plan import SamplePlan
from src.models.snapshot import DataSnapshot
from src.services import audit_service, snapshot_service
from src.utils.errors import AppError
from src.utils.timeutil import local_iso


def to_unit(minor: float, scale: int) -> float:
    return round(float(minor) / (scale or 1), 2)


def list_datasets(db: Session) -> list[Dataset]:
    return list(db.scalars(select(Dataset).order_by(Dataset.id)))


def get_dataset(db: Session, dataset_id: int) -> Dataset:
    dataset = db.get(Dataset, dataset_id)
    if dataset is None:
        raise AppError("NOT_FOUND", 404, detail=f"dataset {dataset_id}")
    return dataset


def _counts(db: Session, dataset_id: int) -> tuple[int, int]:
    plan_count = db.scalar(select(func.count()).select_from(SamplePlan).where(SamplePlan.dataset_id == dataset_id)) or 0
    query_count = (
        db.scalar(select(func.count()).select_from(ApproxQuery).where(ApproxQuery.dataset_id == dataset_id)) or 0
    )
    return int(plan_count), int(query_count)


def _snapshot_brief(snapshot: DataSnapshot | None) -> dict | None:
    if snapshot is None:
        return None
    return {
        "id": snapshot.id,
        "code": snapshot.code,
        "version": snapshot.version,
        "status": snapshot.status,
        "content_fingerprint": snapshot.content_fingerprint,
        "published_at": local_iso(snapshot.published_at),
    }


def _strata_from_snapshot(snapshot: DataSnapshot, scale: int) -> list[dict]:
    total = sum(int(s.get("population", 0)) for s in (snapshot.strata_json or [])) or 1
    return [
        {
            "code": s["code"],
            "label": s["label"],
            "population": int(s["population"]),
            "population_share_pct": round(int(s["population"]) / total * 100, 2),
            "mean_value": to_unit(float(s.get("mean_minor", 0)), scale),
            "prior_std": to_unit(float(s.get("prior_std", 0)), scale),
            "min_sample": int(s.get("min_sample", 30)),
            "sort_order": int(s.get("sort_order", 0)),
        }
        for s in snapshot_service.snapshot_layers(snapshot)
    ]


def dataset_out(db: Session, dataset: Dataset, with_strata: bool = False) -> dict:
    plan_count, query_count = _counts(db, dataset.id)
    scale = dataset.unit_scale
    snapshot = snapshot_service.current_snapshot(db, dataset)
    payload = {
        "id": dataset.id,
        "code": dataset.code,
        "name": dataset.name,
        "scenario": dataset.scenario,
        "metric_label": dataset.metric_label,
        "unit": dataset.unit,
        "unit_scale": scale,
        "row_count": dataset.row_count,
        "source_rows": dataset.source_rows,
        "strata_field": dataset.strata_field,
        "value_field": dataset.value_field,
        "precision_target_pct": float(dataset.precision_target_pct),
        "confidence_level": float(dataset.confidence_level),
        "prior_period": dataset.prior_period,
        "full_scan_baseline_ms": dataset.full_scan_baseline_ms,
        "truth_total": to_unit(dataset.truth_total_minor, scale),
        "measured_full_scan_ms": dataset.measured_full_scan_ms,
        "note": dataset.note,
        "strata_count": len(snapshot.strata_json) if snapshot else len(dataset.strata),
        "plan_count": plan_count,
        "query_count": query_count,
        "snapshot": _snapshot_brief(snapshot),
        "updated_at": local_iso(dataset.updated_at),
    }
    if with_strata:
        payload["strata"] = _strata_from_snapshot(snapshot, scale) if snapshot else []
    return payload


def strata_inputs(dataset: Dataset, keep_codes: list[str] | None = None) -> list[dict]:
    """兼容旧调用：从活动分层表读取（仅 seed / 基线快照使用）。

    业务链路请改用 ``snapshot_service.strata_inputs_from_snapshot``，确保读到不可变版本。
    """
    codes = set(keep_codes or [])
    rows = []
    for s in sorted(dataset.strata, key=lambda item: item.sort_order):
        if codes and s.code not in codes:
            continue
        rows.append(
            {
                "code": s.code,
                "label": s.label,
                "population": int(s.population),
                "mean_minor": float(s.mean_minor),
                "prior_std": float(Decimal(s.prior_std_minor)),
                "min_sample": int(s.min_sample),
            }
        )
    return rows


def patch_dataset(db: Session, dataset_id: int, payload, actor: dict, ip: str | None = None) -> Dataset:
    dataset = get_dataset(db, dataset_id)
    changes: dict[str, dict] = {}
    if payload.precision_target_pct is not None:
        new_value = round(float(payload.precision_target_pct), 2)
        if new_value != float(dataset.precision_target_pct):
            changes["precision_target_pct"] = {"from": float(dataset.precision_target_pct), "to": new_value}
            dataset.precision_target_pct = Decimal(str(new_value))
    if payload.confidence_level is not None:
        new_conf = round(float(payload.confidence_level), 3)
        if new_conf != float(dataset.confidence_level):
            changes["confidence_level"] = {"from": float(dataset.confidence_level), "to": new_conf}
            dataset.confidence_level = Decimal(str(new_conf))
    if payload.note is not None and payload.note != (dataset.note or ""):
        changes["note"] = {"from": dataset.note, "to": payload.note}
        dataset.note = payload.note
    if not changes:
        return dataset
    audit_service.record(
        db,
        actor_email=actor["email"],
        actor_role=actor["role"],
        action="dataset.calibrate",
        entity="dataset",
        entity_id=str(dataset.id),
        detail={"code": dataset.code, "changes": changes},
        ip=ip,
    )
    db.flush()
    return dataset


def measure_truth(db: Session, dataset: Dataset) -> dict:
    """真实执行一次全表聚合（这是「全量真值」的来源，不是硬编码）。

    仅 seed / 基线建立使用；业务刷新走 publish_refresh_snapshot，发布为不可变快照。
    """
    started = time.perf_counter()
    row = db.execute(
        text("SELECT COALESCE(SUM(value_minor), 0) AS total, COUNT(*) AS row_count FROM waybills WHERE dataset_id = :d"),
        {"d": dataset.id},
    ).one()
    elapsed_ms = (time.perf_counter() - started) * 1000
    dataset.truth_total_minor = int(row.total or 0)
    dataset.measured_full_scan_ms = int(round(elapsed_ms))
    db.flush()
    return {
        "truth_total_minor": int(row.total or 0),
        "rows": int(row.row_count or 0),
        "measured_full_scan_ms": int(round(elapsed_ms)),
    }


def publish_refresh_snapshot(
    db: Session,
    dataset_id: int,
    actor_email: str = "worker",
    actor_role: str = "system",
    ip: str | None = None,
) -> dict:
    """刷新发布为不可变快照：层容量/均值/波动/真值整版一次可见，原子提交、幂等。

    成功才切 ``current_snapshot_id``；任何异常由外层 session_scope 整体回滚。
    返回发布结果（含是否复用当前快照）。
    """
    dataset = get_dataset(db, dataset_id)
    if dataset.row_count <= 0 and not dataset.strata:
        raise AppError("DATASET_EMPTY", 422, detail="数据集还没有载入明细行")

    before = snapshot_service.current_snapshot(db, dataset)
    before_id = before.id if before else None

    started = time.perf_counter()
    truth_row = db.execute(
        text("SELECT COALESCE(SUM(value_minor), 0) AS total, COUNT(*) AS row_count FROM waybills WHERE dataset_id = :d"),
        {"d": dataset.id},
    ).one()
    measured_full_scan_ms = int(round((time.perf_counter() - started) * 1000))
    truth_total_minor = int(truth_row.total or 0)
    row_count = int(truth_row.row_count or 0)
    if truth_total_minor <= 0 or row_count <= 0:
        raise AppError("DATASET_EMPTY", 422, detail="数据集没有明细行，无法发布快照")

    snapshot = snapshot_service.publish_snapshot(
        db,
        dataset,
        created_by=actor_email,
        note=f"{actor_role} 触发的整版刷新",
        measured_full_scan_ms=measured_full_scan_ms,
    )
    reused = snapshot.id == before_id
    audit_service.record(
        db,
        actor_email=actor_email,
        actor_role=actor_role,
        action="dataset.publish_snapshot" if not reused else "dataset.refresh_noop",
        entity="data_snapshot",
        entity_id=str(snapshot.id),
        detail={
            "dataset_id": dataset.id,
            "dataset_code": dataset.code,
            "snapshot_code": snapshot.code,
            "version": snapshot.version,
            "reused": reused,
            "fingerprint": snapshot.content_fingerprint,
            "truth_total_minor": truth_total_minor,
            "rows": row_count,
            "measured_full_scan_ms": measured_full_scan_ms,
        },
        layer="worker" if actor_role == "system" else "domain",
        ip=ip,
    )
    return {
        "snapshot_id": snapshot.id,
        "snapshot_code": snapshot.code,
        "version": snapshot.version,
        "reused": reused,
        "truth_total_minor": truth_total_minor,
        "rows": row_count,
        "measured_full_scan_ms": measured_full_scan_ms,
    }


def refresh_truth(db: Session, dataset_id: int, actor_email: str = "worker", actor_role: str = "system") -> dict:
    """向后兼容的刷新入口：等价于发布一个不可变快照。"""
    return publish_refresh_snapshot(db, dataset_id, actor_email=actor_email, actor_role=actor_role)
