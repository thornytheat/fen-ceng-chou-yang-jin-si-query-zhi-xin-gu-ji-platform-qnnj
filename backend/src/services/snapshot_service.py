"""数据快照服务：不可变版本的构建、发布、读取与一致性校验。

刷新语义（对应「成功刷新发布为不可变快照」的需求）：

* 层容量 N_h、层均值、层波动 S_h 从运单明细真实聚合，连同全量真值一起整版写入
  一条不可变 ``data_snapshots`` 行（``strata_json`` 全量冻结）；
* 全部计算与指针切换在同一个 DB 事务内完成，提交失败整体回滚，不会留下半套状态；
* ``content_fingerprint`` 对整版层口径取确定性指纹，内容没变的重复刷新复用当前快照，
  即「重复执行刷新只生效一次」；
* 方案、查询都通过本模块读取自己绑定版本的层口径，不再回查会被原地改写的活动分层表。
"""
from __future__ import annotations

import hashlib
import json
import math
from decimal import Decimal

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from src.models.dataset import Dataset, DatasetStratum
from src.models.snapshot import DataSnapshot
from src.utils.errors import AppError

# 逐层聚合：COUNT / SUM(value) / 平移二阶矩。层数量很少（演示 5 层），逐层查询开销可忽略。
# MySQL 下先 CAST 成 DECIMAL 再 POWER，避免 BIGINT 相乘溢出；SQLite 无 POWER/DECIMAL，
# 直接平方即可（测试库数值很小）。shift 取该层最小值，平移后平方量级显著缩小，
# 标准差由 E[(x-shift)²] 与平移均值反推，数值更稳。
_STRATUM_AGG_MYSQL = text(
    """
    SELECT COUNT(*) AS row_count,
           COALESCE(SUM(value_minor), 0) AS value_sum,
           COALESCE(SUM(POWER(CAST(value_minor AS DECIMAL(28, 4)) - :shift, 2)), 0) AS value_sq_shift
    FROM waybills
    WHERE dataset_id = :dataset_id AND stratum_code = :code
    """
)

_STRATUM_AGG_GENERIC = text(
    """
    SELECT COUNT(*) AS row_count,
           COALESCE(SUM(value_minor), 0) AS value_sum,
           COALESCE(SUM((value_minor - :shift) * (value_minor - :shift)), 0) AS value_sq_shift
    FROM waybills
    WHERE dataset_id = :dataset_id AND stratum_code = :code
    """
)


def _agg_sql(db: Session):
    return _STRATUM_AGG_MYSQL if db.get_bind().dialect.name == "mysql" else _STRATUM_AGG_GENERIC


# ----------------------------------------------------------------- 基础读取


def get_snapshot(db: Session, snapshot_id: int) -> DataSnapshot:
    row = db.get(DataSnapshot, snapshot_id)
    if row is None:
        raise AppError("NOT_FOUND", 404, detail=f"snapshot {snapshot_id}")
    return row


def current_snapshot(db: Session, dataset: Dataset) -> DataSnapshot | None:
    if dataset.current_snapshot_id is None:
        return None
    return db.get(DataSnapshot, dataset.current_snapshot_id)


def require_current_snapshot(db: Session, dataset: Dataset) -> DataSnapshot:
    snapshot = current_snapshot(db, dataset)
    if snapshot is None:
        raise AppError("SNAPSHOT_NOT_READY", 409, detail=f"dataset {dataset.code} 还没有已发布的数据快照")
    return snapshot


def list_snapshots(db: Session, dataset_id: int) -> list[DataSnapshot]:
    return list(
        db.scalars(
            select(DataSnapshot)
            .where(DataSnapshot.dataset_id == dataset_id)
            .order_by(DataSnapshot.version.desc())
        )
    )


# ----------------------------------------------------------------- 层口径计算


def _layer_label(db: Session, dataset_id: int) -> dict[str, dict]:
    rows = db.execute(
        select(DatasetStratum.code, DatasetStratum.label, DatasetStratum.min_sample, DatasetStratum.sort_order)
        .where(DatasetStratum.dataset_id == dataset_id)
        .order_by(DatasetStratum.sort_order)
    ).mappings().all()
    return {row["code"]: dict(row) for row in rows}


def compute_layer_stats(db: Session, dataset: Dataset) -> list[dict]:
    """从明细真实聚合每层 N_h / 均值 / 总体标准差（波动），按既有分层顺序输出。"""
    labels = _layer_label(db, dataset.id)
    layers: list[dict] = []
    for code, meta in labels.items():
        shift = db.execute(
            text("SELECT COALESCE(MIN(value_minor), 0) FROM waybills WHERE dataset_id = :d AND stratum_code = :c"),
            {"d": dataset.id, "c": code},
        ).scalar()
        row = db.execute(
            _agg_sql(db),
            {"dataset_id": dataset.id, "code": code, "shift": float(shift or 0)},
        ).mappings().one()
        count = int(row["row_count"])
        if count <= 0:
            raise AppError("DATASET_EMPTY", 422, detail=f"层 {code} 没有明细行，无法发布快照")
        value_sum = int(row["value_sum"] or 0)
        sq_shift = float(row["value_sq_shift"] or 0.0)
        mean = value_sum / count
        # 平移法总体方差：Var(x) = E[(x-shift)²] - (mean-shift)²，浮点残差兜底为 0
        variance = max(0.0, sq_shift / count - (mean - float(shift or 0)) ** 2)
        std = math.sqrt(variance)
        layers.append(
            {
                "code": code,
                "label": meta["label"],
                "population": count,
                "value_sum": value_sum,  # 内部字段：层真实 SUM，用于精确累加全表真值（持久化前剥离）
                "mean_minor": int(round(mean)),
                "mean_value": round(mean, 4),
                "prior_std": round(std, 2),
                "min_sample": int(meta["min_sample"]),
                "sort_order": int(meta["sort_order"]),
            }
        )
    if not layers:
        raise AppError("DATASET_EMPTY", 422, detail="数据集没有分层定义")
    return layers


def _fingerprint(layers: list[dict], truth_total_minor: int, row_count: int) -> str:
    """整版层容量/均值/波动 + 真值的确定性指纹（量化到与存储一致的精度）。"""
    payload = {
        "row_count": int(row_count),
        "truth_total_minor": int(truth_total_minor),
        "strata": [
            {
                "code": item["code"],
                "population": int(item["population"]),
                "mean_minor": int(item["mean_minor"]),
                "prior_std": round(float(item["prior_std"]), 2),
                "min_sample": int(item["min_sample"]),
            }
            for item in sorted(layers, key=lambda item: item["code"])
        ],
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _next_version(db: Session, dataset_id: int) -> int:
    last = db.scalar(
        select(func.coalesce(func.max(DataSnapshot.version), 0)).where(DataSnapshot.dataset_id == dataset_id)
    )
    return int(last or 0) + 1


def _snapshot_code(dataset_code: str, version: int) -> str:
    return f"SNAP-{dataset_code}-v{version:03d}"


# ----------------------------------------------------------------- 发布 / 刷新


def _public_layers(layers: list[dict]) -> list[dict]:
    """剥离内部聚合字段（如层真实 SUM），只保留要冻结进快照的对外口径。"""
    return [{k: v for k, v in row.items() if k != "value_sum"} for row in layers]


def _persist_snapshot(
    db: Session,
    dataset: Dataset,
    layers: list[dict],
    *,
    truth_total_minor: int,
    row_count: int,
    measured_full_scan_ms: int | None,
    fingerprint: str,
    version: int,
    created_by: str,
    note: str | None,
) -> DataSnapshot:
    snapshot = DataSnapshot(
        dataset_id=dataset.id,
        code=_snapshot_code(dataset.code, version),
        version=version,
        status="published",
        content_fingerprint=fingerprint,
        row_count=row_count,
        truth_total_minor=truth_total_minor,
        measured_full_scan_ms=measured_full_scan_ms,
        precision_target_pct=Decimal(str(dataset.precision_target_pct)),
        confidence_level=Decimal(str(dataset.confidence_level)),
        strata_json=_public_layers(layers),
        note=note,
        created_by=created_by,
    )
    db.add(snapshot)
    db.flush()  # 拿到 snapshot.id，随后在同一事务内切指针
    dataset.current_snapshot_id = snapshot.id
    dataset.row_count = row_count
    dataset.truth_total_minor = truth_total_minor
    if measured_full_scan_ms is not None:
        dataset.measured_full_scan_ms = measured_full_scan_ms
    db.flush()
    return snapshot


def publish_snapshot(
    db: Session,
    dataset: Dataset,
    *,
    created_by: str = "system",
    note: str | None = None,
    measured_full_scan_ms: int | None = None,
) -> DataSnapshot:
    """重算整版口径并发布新快照（单事务、原子切指针）。

    内容指纹与当前快照相同则复用、不新增版本，保证重复刷新只生效一次。
    调用方需用 session_scope 包裹，异常时整体回滚。
    """
    # 行锁：并发刷新时串行化，避免两个事务同时切指针（SQLite 不支持 FOR UPDATE 会被忽略）
    db.execute(select(Dataset.id).where(Dataset.id == dataset.id).with_for_update())

    layers = compute_layer_stats(db, dataset)
    # 真值 / 行数由各层精确累加，避免再做一次全表扫描（8000 万行下省一轮 SUM）
    truth_total_minor = sum(int(item["value_sum"]) for item in layers)
    row_count = sum(int(item["population"]) for item in layers)
    if truth_total_minor <= 0 or row_count <= 0:
        raise AppError("DATASET_EMPTY", 422, detail="数据集没有明细行，无法发布快照")
    fingerprint = _fingerprint(layers, truth_total_minor, row_count)

    current = current_snapshot(db, dataset)
    if current is not None and current.content_fingerprint == fingerprint:
        # 数据版本未变：刷新幂等，直接复用当前快照，不产生新版本
        return current

    snapshot = _persist_snapshot(
        db,
        dataset,
        layers,
        truth_total_minor=truth_total_minor,
        row_count=row_count,
        measured_full_scan_ms=measured_full_scan_ms,
        fingerprint=fingerprint,
        version=_next_version(db, dataset.id),
        created_by=created_by,
        note=note,
    )
    return snapshot


def ensure_baseline_snapshot(db: Session, dataset: Dataset, *, created_by: str = "seed") -> DataSnapshot:
    """为尚未有快照的数据集补一条 v1 基线快照（幂等）。

    层口径直接冻结既有活动分层表（N_h / 先验均值 / 先验 S_h / 最小样本），
    真值取数据集已测量值，保证升级前已审批的方案与历史结果仍归属同一版本。
    """
    existing = current_snapshot(db, dataset)
    if existing is not None:
        return existing
    if not dataset.strata:
        raise AppError("DATASET_EMPTY", 422, detail="数据集没有分层定义，无法建立基线快照")
    layers = [
        {
            "code": s.code,
            "label": s.label,
            "population": int(s.population),
            "mean_minor": int(s.mean_minor),
            "mean_value": round(float(s.mean_minor), 4),
            "prior_std": round(float(Decimal(s.prior_std_minor)), 2),
            "min_sample": int(s.min_sample),
            "sort_order": int(s.sort_order),
        }
        for s in sorted(dataset.strata, key=lambda item: item.sort_order)
    ]
    truth_total_minor = int(dataset.truth_total_minor or 0)
    row_count = int(dataset.row_count or sum(item["population"] for item in layers))
    fingerprint = _fingerprint(layers, truth_total_minor, row_count)
    return _persist_snapshot(
        db,
        dataset,
        layers,
        truth_total_minor=truth_total_minor,
        row_count=row_count,
        measured_full_scan_ms=dataset.measured_full_scan_ms,
        fingerprint=fingerprint,
        version=_next_version(db, dataset.id),
        created_by=created_by,
        note="v1 基线快照：冻结升级前的分层口径与全量真值",
    )


# ----------------------------------------------------------------- 版本口径读取


def snapshot_layers(snapshot: DataSnapshot, keep_codes: list[str] | None = None) -> list[dict]:
    codes = set(keep_codes or [])
    layers = sorted(snapshot.strata_json or [], key=lambda item: item.get("sort_order", 0))
    if codes:
        layers = [item for item in layers if item["code"] in codes]
    return layers


def strata_inputs_from_snapshot(snapshot: DataSnapshot, keep_codes: list[str] | None = None) -> list[dict]:
    """转成抽样 / 估计算法所需的层入参（字段与原 dataset_service.strata_inputs 对齐）。"""
    rows = []
    for item in snapshot_layers(snapshot, keep_codes):
        rows.append(
            {
                "code": item["code"],
                "label": item["label"],
                "population": int(item["population"]),
                "mean_minor": float(item["mean_minor"]),
                "prior_std": float(item["prior_std"]),
                "min_sample": int(item.get("min_sample", 30)),
            }
        )
    return rows


def assert_matches_current(db: Session, plan) -> None:
    """方案必须绑定数据集当前快照才能审批 / 跑查询，否则要求按新快照重建并重审批。"""
    dataset = db.get(Dataset, plan.dataset_id)
    if dataset is None:
        raise AppError("NOT_FOUND", 404, detail=f"dataset {plan.dataset_id}")
    current = require_current_snapshot(db, dataset)
    if plan.snapshot_id != current.id:
        raise AppError(
            "PLAN_SNAPSHOT_STALE",
            409,
            detail=f"plan {plan.code} 绑定的快照已不是当前版本，请按当前快照重建方案并重新审批",
        )


def snapshot_out(snapshot: DataSnapshot) -> dict:
    return {
        "id": snapshot.id,
        "code": snapshot.code,
        "dataset_id": snapshot.dataset_id,
        "version": snapshot.version,
        "status": snapshot.status,
        "row_count": int(snapshot.row_count),
        "truth_total_minor": int(snapshot.truth_total_minor),
        "precision_target_pct": float(snapshot.precision_target_pct),
        "confidence_level": float(snapshot.confidence_level),
        "content_fingerprint": snapshot.content_fingerprint,
        "created_by": snapshot.created_by,
        "note": snapshot.note,
        "published_at": snapshot.published_at.isoformat() if snapshot.published_at else None,
        "created_at": snapshot.created_at.isoformat() if snapshot.created_at else None,
        "strata": snapshot_layers(snapshot),
    }
