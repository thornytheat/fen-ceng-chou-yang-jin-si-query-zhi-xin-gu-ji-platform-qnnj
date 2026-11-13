"""数据快照（不可变版本）：一次成功刷新发布的整版分层口径。

快照是「线路数据重跑」后对外可见的唯一数据版本，整版一次可见、不可修改：

* ``strata_json`` 整版保存发布时刻每层的容量 N_h、层均值、层波动 S_h 与最小样本，
  旧方案与历史查询永远引用自己那一版，刷新不会原地改写它们读到的口径；
* 新快照只有在层口径、均值、波动、真值全部计算完成并成功提交后，才把
  ``datasets.current_snapshot_id`` 指针切到新行，因此中途失败不会留下半套状态；
* ``content_fingerprint`` 是整版层口径的确定性指纹，内容没变的重复刷新直接复用当前
  快照，重复执行刷新只生效一次。
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, DateTime, DECIMAL, ForeignKey, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models import Base


class DataSnapshot(Base):
    __tablename__ = "data_snapshots"
    __table_args__ = (
        UniqueConstraint("dataset_id", "version", name="uq_snapshot_dataset_version"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    dataset_id: Mapped[int] = mapped_column(
        ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False
    )
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="published", nullable=False)
    content_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    row_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    truth_total_minor: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    measured_full_scan_ms: Mapped[int | None] = mapped_column(Integer)
    # 发布时刻的精度标准一并冻结，保证该版本「数据 + 判定口径」自洽
    precision_target_pct: Mapped[Decimal] = mapped_column(DECIMAL(5, 2), nullable=False)
    confidence_level: Mapped[Decimal] = mapped_column(DECIMAL(4, 3), nullable=False)
    strata_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    note: Mapped[str | None] = mapped_column(String(400))
    created_by: Mapped[str] = mapped_column(String(160), default="system", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    published_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    dataset: Mapped["Dataset"] = relationship(
        back_populates="snapshots", foreign_keys=[dataset_id]
    )
