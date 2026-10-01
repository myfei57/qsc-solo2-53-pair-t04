"""单炉荒煤气估算流量与按炉累计量。

桥管上没有可用的真实流量计，流量按"开度折算"估算：全开时取
``flow_full_open_m3_s``，实际流量与回读开度成比例。用回读开度而不是指令开
度，回读跳动冻结时计入的是冻结在现场的真实开度，账仍然可对。

模型刻意做成纯线性、无隐藏状态，审计可以拿同一组开度与系数逐步复算累计量。
"""

from __future__ import annotations

from ..config import CollectorLimits
from .flap import FLAP_MAX_DEG


def estimate_flow_m3_s(readback_deg: float, limits: CollectorLimits) -> float:
    """按翻板回读开度估算单炉荒煤气瞬时流量（立方米/秒）。"""

    fraction = min(1.0, max(0.0, float(readback_deg) / FLAP_MAX_DEG))
    return limits.flow_full_open_m3_s * fraction


class OvenGasMeter:
    """单炉累计量积分器（梯形外的左矩形积分，口径固定便于复算）。"""

    def __init__(self, limits: CollectorLimits) -> None:
        self._limits = limits
        self._total_m3 = 0.0

    @property
    def total_m3(self) -> float:
        """本账期累计荒煤气量（立方米）。"""

        return self._total_m3

    def accumulate(self, readback_deg: float, dt: float) -> float:
        """按本拍回读开度累加一个周期，返回本拍计入量。"""

        if dt <= 0:
            raise ValueError("步长必须为正")
        volume = estimate_flow_m3_s(readback_deg, self._limits) * dt
        self._total_m3 += volume
        return volume

    def seed(self, total_m3: float) -> None:
        """把累计量置到给定值（跨重启从审计流水恢复用）。"""

        if total_m3 < 0.0:
            raise ValueError("累计量不能为负")
        self._total_m3 = float(total_m3)
