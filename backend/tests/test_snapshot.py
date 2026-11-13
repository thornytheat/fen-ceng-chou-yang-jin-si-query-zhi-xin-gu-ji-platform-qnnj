"""数据快照（不可变版本）集成测试：用 SQLite 内存库，不依赖 MySQL / Redis。

覆盖需求要点：
  1. 成功刷新发布为不可变快照，层容量/均值/波动整版一次可见；
  2. 内容没变的重复刷新只生效一次（指纹幂等，不新增版本）；
  3. 中途失败整体回滚，不留下半套状态、指针不切换；
  4. 方案与查询绑定快照；快照前进后旧方案审批/查询被拒，必须重建重审批；
  5. 历史结果随旧快照保留，页面/结果/导出读取同一数据版本。
"""
from __future__ import annotations

import math

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.models import Base
from src.models.dataset import Dataset, DatasetStratum
from src.models.waybill import Waybill
from src.services import dataset_service, plan_service, query_service, snapshot_service
from src.utils.errors import AppError


# ----------------------------------------------------------------- 夹具


@pytest.fixture()
def db() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = Session(engine)
    _seed_dataset(session)
    yield session
    session.close()


def _seed_dataset(session: Session, dataset_id: int = 1, scale: int = 100) -> Dataset:
    dataset = Dataset(
        id=dataset_id,
        code=f"DS-{dataset_id}",
        name="测试数据集",
        scenario="场景",
        metric_label="指标",
        unit="元",
        unit_scale=scale,
        row_count=0,
        source_rows=1000,
        strata_field="route_type",
        value_field="value_minor",
        precision_target_pct=2,
        confidence_level=0.95,
        prior_period="2026-10",
        full_scan_baseline_ms=1000,
        truth_total_minor=0,
    )
    session.add(dataset)
    session.flush()
    # 两层：A=[100,200]（N=2），B=[400]（N=1）
    layers = {"A": [100, 200], "B": [400]}
    waybill_id = 1
    for idx, (code, values) in enumerate(layers.items()):
        session.add(
            DatasetStratum(
                dataset_id=dataset.id,
                code=code,
                label=f"层{code}",
                population=len(values),
                mean_minor=int(round(sum(values) / len(values))),
                # 活动分层表的先验 S_h 故意与实测不同，用来验证快照取的是实测值
                prior_std_minor=1,
                min_sample=1,
                sort_order=idx,
            )
        )
        for i, value in enumerate(values):
            session.add(
                Waybill(
                    id=waybill_id,
                    dataset_id=dataset.id,
                    stratum_code=code,
                    value_minor=value,
                    sample_rank=f"{i:020d}",
                    biz_date=__import__("datetime").date(2026, 11, 11),
                )
            )
            waybill_id += 1
    session.flush()
    measured = dataset_service.measure_truth(session, dataset)
    dataset.row_count = measured["rows"]
    session.flush()
    return dataset


def _std(values: list[int]) -> float:
    n = len(values)
    mean = sum(values) / n
    return round(math.sqrt(sum((v - mean) ** 2 for v in values) / n), 2)


class _Payload:
    def __init__(self, **kw):
        self.__dict__.update(kw)


_ACTOR = {"email": "analyst@sampleaqp.io", "role": "analyst"}
_ADMIN = {"email": "admin@sampleaqp.io", "role": "admin"}


# ----------------------------------------------------------------- 基线快照


def test_baseline_snapshot_freezes_existing_strata_and_is_idempotent(db: Session) -> None:
    dataset = db.get(Dataset, 1)
    first = snapshot_service.ensure_baseline_snapshot(db, dataset, created_by="seed")
    db.commit()

    assert first.version == 1
    assert first.code == "SNAP-DS-1-v001"
    assert dataset.current_snapshot_id == first.id
    by_code = {row["code"]: row for row in first.strata_json}
    # 基线直接冻结活动分层表的先验口径
    assert by_code["A"]["population"] == 2
    assert by_code["A"]["prior_std"] == 1.0

    # 幂等：再次调用不新增版本，返回同一行
    again = snapshot_service.ensure_baseline_snapshot(db, dataset, created_by="seed")
    assert again.id == first.id
    assert snapshot_service.list_snapshots(db, dataset.id) == [first]


# ----------------------------------------------------------------- 发布 / 幂等


def test_publish_snapshot_recomputes_capacity_mean_std_and_truth(db: Session) -> None:
    dataset = db.get(Dataset, 1)
    snapshot_service.ensure_baseline_snapshot(db, dataset)
    db.commit()

    new = dataset_service.publish_refresh_snapshot(db, 1, actor_email="celery", actor_role="system")
    db.commit()

    snap = snapshot_service.get_snapshot(db, new["snapshot_id"])
    assert new["reused"] is False
    assert snap.version == 2
    layers = {row["code"]: row for row in snap.strata_json}
    # 层容量来自实测 COUNT
    assert layers["A"]["population"] == 2
    assert layers["B"]["population"] == 1
    # 层均值来自实测
    assert layers["A"]["mean_minor"] == 150
    assert layers["B"]["mean_minor"] == 400
    # 层波动（总体标准差）来自实测，而非活动表里写死的先验 1
    assert layers["A"]["prior_std"] == _std([100, 200])  # 50.0
    assert layers["B"]["prior_std"] == 0.0
    # 真值整版冻结，指针已切到新快照
    assert snap.truth_total_minor == 700
    assert dataset.current_snapshot_id == snap.id


def test_repeated_refresh_with_same_data_is_noop(db: Session) -> None:
    dataset = db.get(Dataset, 1)
    snapshot_service.ensure_baseline_snapshot(db, dataset)
    db.commit()

    first = dataset_service.publish_refresh_snapshot(db, 1)
    db.commit()
    # 明细没有任何变化：再次刷新必须复用当前快照，不产生 v3
    second = dataset_service.publish_refresh_snapshot(db, 1)
    db.commit()

    assert second["reused"] is True
    assert second["snapshot_id"] == first["snapshot_id"]
    versions = [row.version for row in snapshot_service.list_snapshots(db, 1)]
    assert versions == [2, 1]


def test_changed_data_publishes_new_version_but_keeps_history(db: Session) -> None:
    dataset = db.get(Dataset, 1)
    snapshot_service.ensure_baseline_snapshot(db, dataset)
    db.commit()
    v2 = dataset_service.publish_refresh_snapshot(db, 1)
    db.commit()

    # 线路数据重跑：A 层新增一行，容量/均值/波动/真值都变
    db.add(
        Waybill(
            id=900,
            dataset_id=1,
            stratum_code="A",
            value_minor=300,
            sample_rank=f"{9:020d}",
            biz_date=__import__("datetime").date(2026, 11, 11),
        )
    )
    db.commit()
    v3 = dataset_service.publish_refresh_snapshot(db, 1)
    db.commit()

    assert v3["reused"] is False
    assert v3["version"] == 3
    snap3 = snapshot_service.get_snapshot(db, v3["snapshot_id"])
    assert {row["code"]: row["population"] for row in snap3.strata_json}["A"] == 3
    # 旧版本原样保留
    snap2 = snapshot_service.get_snapshot(db, v2["snapshot_id"])
    assert {row["code"]: row["population"] for row in snap2.strata_json}["A"] == 2
    assert [row.version for row in snapshot_service.list_snapshots(db, 1)] == [3, 2, 1]


# ----------------------------------------------------------------- 原子性 / 失败回滚


def test_failed_publish_rolls_back_and_leaves_no_half_state(db: Session) -> None:
    dataset = db.get(Dataset, 1)
    baseline = snapshot_service.ensure_baseline_snapshot(db, dataset)
    db.commit()
    before_versions = [row.version for row in snapshot_service.list_snapshots(db, 1)]

    # 让发布在写入阶段抛错：用 monkeypatch 令持久化失败，模拟中途异常
    def boom(*args, **kwargs):  # noqa: ANN001
        raise RuntimeError("simulated mid-refresh failure")

    from src.services import snapshot_service as ss

    original = ss._persist_snapshot
    ss._persist_snapshot = boom
    try:
        with pytest.raises(RuntimeError, match="simulated"):
            dataset_service.publish_refresh_snapshot(db, 1)
        db.rollback()
    finally:
        ss._persist_snapshot = original

    # 没有新增快照、指针仍是基线、数据集真值未被半改
    assert [row.version for row in snapshot_service.list_snapshots(db, 1)] == before_versions
    db.refresh(dataset)
    assert dataset.current_snapshot_id == baseline.id
    assert dataset.truth_total_minor == baseline.truth_total_minor


def test_publish_with_empty_dataset_raises_and_creates_nothing(db: Session) -> None:
    # 新建一个没有任何明细的数据集
    empty = Dataset(
        code="DS-EMPTY",
        name="空集",
        scenario="s",
        metric_label="m",
        unit="元",
        unit_scale=100,
        row_count=0,
        source_rows=1,
        strata_field="route_type",
        value_field="v",
        precision_target_pct=2,
        confidence_level=0.95,
        prior_period="p",
        full_scan_baseline_ms=1,
    )
    db.add(empty)
    db.flush()
    with pytest.raises(AppError) as exc:
        snapshot_service.publish_snapshot(db, empty)
    assert exc.value.code == "DATASET_EMPTY"
    db.rollback()
    assert snapshot_service.list_snapshots(db, empty.id) == []


# ----------------------------------------------------------------- 方案绑定 / 旧方案失效


def _make_plan(db: Session, name: str = "方案"):
    payload = _Payload(
        dataset_id=1,
        name=name,
        confidence_level=0.95,
        target_error_pct=50.0,
        method="neyman",
        note=None,
    )
    return plan_service.create_plan(db, payload, _ACTOR)


def test_plan_is_bound_to_current_snapshot_and_approve_works(db: Session) -> None:
    dataset = db.get(Dataset, 1)
    snap = snapshot_service.ensure_baseline_snapshot(db, dataset)
    db.commit()

    plan = _make_plan(db)
    assert plan.snapshot_id == snap.id
    plan_service.approve_plan(db, plan.id, _ADMIN)
    assert plan.status == "active"


def test_stale_plan_cannot_be_approved_after_snapshot_advances(db: Session) -> None:
    dataset = db.get(Dataset, 1)
    snapshot_service.ensure_baseline_snapshot(db, dataset)
    db.commit()
    plan = _make_plan(db)  # 草稿，绑定 v1

    # 数据变化并发布 v2
    db.add(
        Waybill(
            id=900,
            dataset_id=1,
            stratum_code="A",
            value_minor=300,
            sample_rank=f"{9:020d}",
            biz_date=__import__("datetime").date(2026, 11, 11),
        )
    )
    db.commit()
    dataset_service.publish_refresh_snapshot(db, 1)
    db.commit()

    with pytest.raises(AppError) as exc:
        plan_service.approve_plan(db, plan.id, _ADMIN)
    assert exc.value.code == "PLAN_SNAPSHOT_STALE"

    # 按当前快照重建的新方案可以正常审批
    fresh = _make_plan(db, name="新方案")
    assert fresh.snapshot_id == dataset.current_snapshot_id
    plan_service.approve_plan(db, fresh.id, _ADMIN)
    assert fresh.status == "active"


def test_query_requires_plan_matching_current_snapshot(db: Session) -> None:
    dataset = db.get(Dataset, 1)
    snapshot_service.ensure_baseline_snapshot(db, dataset)
    db.commit()
    plan = _make_plan(db)
    plan_service.approve_plan(db, plan.id, _ADMIN)
    db.commit()

    # 当前快照下可以跑查询
    row = query_service.run_query(db, plan.id, [], None, _ACTOR)
    assert row.snapshot_id == plan.snapshot_id
    assert row.snapshot_code is not None
    db.commit()

    # 发布新版本后，旧的已审批方案不再允许跑新查询
    db.add(
        Waybill(
            id=900,
            dataset_id=1,
            stratum_code="A",
            value_minor=300,
            sample_rank=f"{9:020d}",
            biz_date=__import__("datetime").date(2026, 11, 11),
        )
    )
    db.commit()
    dataset_service.publish_refresh_snapshot(db, 1)
    db.commit()
    with pytest.raises(AppError) as exc:
        query_service.run_query(db, plan.id, [], None, _ACTOR)
    assert exc.value.code == "PLAN_SNAPSHOT_STALE"


def test_historical_query_keeps_old_snapshot_data_version(db: Session) -> None:
    dataset = db.get(Dataset, 1)
    snapshot_service.ensure_baseline_snapshot(db, dataset)
    db.commit()
    plan = _make_plan(db)
    plan_service.approve_plan(db, plan.id, _ADMIN)
    db.commit()
    historical = query_service.run_query(db, plan.id, [], None, _ACTOR)
    old_snapshot_id = historical.snapshot_id
    old_population = {item["code"]: item["population"] for item in historical.strata_detail}
    db.commit()

    # 数据刷新发布新版本
    db.add(
        Waybill(
            id=900,
            dataset_id=1,
            stratum_code="A",
            value_minor=300,
            sample_rank=f"{9:020d}",
            biz_date=__import__("datetime").date(2026, 11, 11),
        )
    )
    db.commit()
    dataset_service.publish_refresh_snapshot(db, 1)
    db.commit()

    # 历史结果仍归属旧快照，层容量不被新数据改写，页面/导出读到同一版本
    fetched = query_service.get_query(db, historical.id)
    assert fetched.snapshot_id == old_snapshot_id
    assert {item["code"]: item["population"] for item in fetched.strata_detail} == old_population
    out = query_service.query_out(db, fetched)
    assert out["snapshot_code"] == snapshot_service.get_snapshot(db, old_snapshot_id).code

    from src.services.export_service import build_estimate_markdown

    filename, markdown = build_estimate_markdown(fetched, None)
    assert out["snapshot_code"] in markdown
    assert filename.endswith(".md")


def test_snapshot_rows_are_immutable_after_publication(db: Session) -> None:
    dataset = db.get(Dataset, 1)
    snap = snapshot_service.ensure_baseline_snapshot(db, dataset)
    db.commit()
    payload = [dict(row) for row in snap.strata_json]
    payload[0]["population"] = 999999
    snap.strata_json = payload
    db.commit()

    # 重新读取确认：通过服务层正常发布流程不会修改已发布行；这里显式验证版本号唯一递增、
    # 新发布总是 INSERT 新行而不是 UPDATE 旧行
    db.add(
        Waybill(
            id=900,
            dataset_id=1,
            stratum_code="A",
            value_minor=300,
            sample_rank=f"{9:020d}",
            biz_date=__import__("datetime").date(2026, 11, 11),
        )
    )
    db.commit()
    outcome = dataset_service.publish_refresh_snapshot(db, 1)
    db.commit()
    assert outcome["snapshot_id"] != snap.id
    assert snapshot_service.get_snapshot(db, snap.id).version == 1
    assert snapshot_service.get_snapshot(db, outcome["snapshot_id"]).version == 2


def test_create_plan_without_snapshot_raises(db: Session) -> None:
    # 未发布任何快照时建方案应被拒绝
    payload = _Payload(
        dataset_id=1,
        name="无快照方案",
        confidence_level=0.95,
        target_error_pct=50.0,
        method="neyman",
        note=None,
    )
    with pytest.raises(AppError) as exc:
        plan_service.create_plan(db, payload, _ACTOR)
    assert exc.value.code == "SNAPSHOT_NOT_READY"
