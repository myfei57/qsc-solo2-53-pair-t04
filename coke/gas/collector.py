"""集气管压力状态。

压力需要被调节到目标带内才算"调好"，装煤序列据此放行桥管翻板。外部扰动
（开盖、换向）会临时把压力拉走，由控制循环周期性重新调节。

压力演化除了鼓风机/初冷器把压力往目标带推之外，还叠加桥管翻板开度的抽吸
作用：在役翻板平均开度越大，荒煤气被抽走越多，集气管压力被拉得越低。控制
环再依据压力调整翻板开度，构成"压力 ↔ 开度"的闭环，避免开大了抽稀、开
小了顶压。
"""

from __future__ import annotations

from ..config import CollectorLimits
from .pressure import regulate_pressure


class CollectorMain:
    """集气管压力状态机。"""

    def __init__(self, limits: CollectorLimits, initial_pa: float = 30.0) -> None:
        self._limits = limits
        self._pressure_pa = float(initial_pa)

    @property
    def limits(self) -> CollectorLimits:
        return self._limits

    @property
    def pressure_pa(self) -> float:
        return self._pressure_pa

    def regulate(self, dt: float, mean_open_deg: float | None = None) -> float:
        """向目标压力调节一个步长，返回新压力。

        ``mean_open_deg`` 为在役翻板的平均开度（度），相对偏置开度的部分按
        抽吸系数拉低压力；缺省取偏置开度（净抽吸为零），保持无翻板链路时的
        老行为。
        """

        if mean_open_deg is None:
            mean_open_deg = self._limits.flap_bias_deg
        base = regulate_pressure(
            self._pressure_pa,
            self._limits.pressure_target_pa,
            self._limits.regulate_rate_pa_per_s,
            dt,
        )
        suction = self._limits.flap_suction_pa_per_deg * (mean_open_deg - self._limits.flap_bias_deg)
        self._pressure_pa = base - suction * min(1.0, dt)
        return self._pressure_pa

    def disturb(self, delta_pa: float) -> float:
        """施加一次外部扰动，返回扰动后的压力。"""

        self._pressure_pa += float(delta_pa)
        return self._pressure_pa

    def settled(self) -> bool:
        """压力是否已进入目标带。"""

        return self._limits.settled(self._pressure_pa)
