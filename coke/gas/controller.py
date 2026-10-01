"""集气管整体压力闭环 + 按炉分配（找开度定死在这一层）。

全段只有这一个压力闭环，这是"按集气管整体调、不按炉单独调"的落点：

* :class:`CollectorPressureController` 读取集气管实测压力与目标压力的偏差，
  输出一个"需要的总通流份额"（0..1）。压力顶不住（实测偏高）→ 加大总份额；
  荒煤气被抽太稀（实测偏低）→ 减小总份额。带死区与斜率，单步显式可复算。
* :func:`allocate_opening` 把总份额按各炉权重（炉况/快调倍率）和保底份额
  分到各炉，再反解成每炉翻板角度。分配采用和累计流量同一条 ``sin`` 通流曲线，
  因此"总开度→各炉开度→各炉流量"三者在审计里口径一致、可交叉核对。

任何单炉都不得持有集气管压力闭环；单炉只接受这里分配下来的开度目标。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import FlapControlLimits


@dataclass(frozen=True)
class PressureVerdict:
    """整体压力闭环一步结论。"""

    measured_pa: float
    target_pa: float
    error_pa: float
    total_conductance: float
    wanted_conductance: float
    in_deadband: bool
    settled: bool

    def as_dict(self) -> dict:
        return {
            "measured_pa": round(self.measured_pa, 2),
            "target_pa": round(self.target_pa, 2),
            "error_pa": round(self.error_pa, 2),
            "total_conductance": round(self.total_conductance, 4),
            "wanted_conductance": round(self.wanted_conductance, 4),
            "in_deadband": self.in_deadband,
            "settled": self.settled,
        }


class CollectorPressureController:
    """集气管整体压力→总通流份额的唯一闭环（平衡式比例律 + 死区 + 斜率）。

    不用纯积分律（饱和后压力回到目标也退不下来，会一直把荒煤气抽太稀），而
    是每周期直接给出"基准抽力 + 压力偏差修正"：压力顶不住（实测高）加大份额，
    抽太稀（实测低）减小份额，压力到目标后份额回落到基准。相邻两周期再做斜率
    限制，快调放宽斜率、缓调收紧，保证动作既能抢时间又不会阶跃。
    """

    def __init__(
        self,
        target_pa: float,
        band_pa: float,
        *,
        gain_per_pa: float,
        deadband_pa: float,
        base_conductance: float = 0.18,
        rate_per_s: float = 0.05,
        max_total_conductance: float = 1.0,
    ) -> None:
        self._target = float(target_pa)
        self._band = max(0.0, float(band_pa))
        self._gain = max(0.0, float(gain_per_pa))
        self._deadband = max(0.0, float(deadband_pa))
        self._base = max(0.0, min(1.0, float(base_conductance)))
        self._rate = max(0.0, float(rate_per_s))
        self._max_total = max(0.0, min(1.0, float(max_total_conductance)))
        self._total = self._base

    @property
    def target_pa(self) -> float:
        return self._target

    @property
    def total_conductance(self) -> float:
        return self._total

    def update(self, measured_pa: float, dt: float, rate_multiplier: float = 1.0) -> PressureVerdict:
        """按当前实测压力直接计算总份额，并做死区与相邻周期斜率限制。

        误差取"实测减目标"：压力高了（顶不住）要加大抽力→份额上升；压力低了
        （抽太稀）减小份额。``rate_multiplier`` 为快/缓调的斜率倍率（快调
        放宽、缓调收紧）；死区内维持上一周期份额（驻留，不抖动）。
        """

        if dt <= 0:
            raise ValueError("步长必须为正")
        error = float(measured_pa) - self._target
        in_deadband = abs(error) <= self._deadband
        previous = self._total
        if in_deadband:
            wanted = previous
        else:
            wanted = self._base + self._gain * error
            wanted = max(0.0, min(self._max_total, wanted))
        # 相邻周期斜率限制：快调倍率放宽、缓调收紧，杜绝阶跃与来回追。
        cap = self._rate * max(0.0, float(rate_multiplier)) * float(dt)
        applied = max(previous - cap, min(previous + cap, wanted))
        self._total = applied
        return PressureVerdict(
            measured_pa=float(measured_pa),
            target_pa=self._target,
            error_pa=error,
            total_conductance=applied,
            wanted_conductance=wanted,
            in_deadband=in_deadband,
            settled=abs(error) <= self._band,
        )


def allocate_opening(
    total_conductance: float,
    weights: dict[str, float],
    limits: FlapControlLimits,
) -> dict[str, float]:
    """把总通流份额按权重和保底份额分到各炉，返回每炉目标角度（度）。

    先给每个在役炉一个保底份额，剩余可调度份额按权重比例分配，再用
    ``asin`` 把每炉份额反解成翻板角度（``conductance`` 的逆函数）。权重全为
    零时均摊，保证总份额总能落地、且每一步可显式复算。
    """

    ovens = list(weights.keys())
    if not ovens:
        return {}
    total = max(0.0, min(1.0, float(total_conductance)))
    floor = max(0.0, min(1.0, limits.opening_floor_fraction))
    active = {name: max(0.0, float(weight)) for name, weight in weights.items()}
    weight_sum = sum(active.values())
    n = len(ovens)

    # 保底开度只在"确实需要抽力"（总份额超过保底区间）时才施加；压力太低、
    # 控制输出趋零时翻板应能基本关死，不能被保底份额强制抽走荒煤气。
    floor_total = min(floor, 1.0) * n
    if total <= 1e-9:
        return {name: 0.0 for name in ovens}
    floor_each = floor if total >= floor_total else total / n
    distributable = max(0.0, total - floor_each * n)

    targets: dict[str, float] = {}
    for name in ovens:
        share = floor_each
        if weight_sum > 0:
            share += distributable * active[name] / weight_sum
        else:
            share += distributable / n
        share = max(0.0, min(1.0, share))
        targets[name] = limits.opening_for_conductance(share)
    return targets
