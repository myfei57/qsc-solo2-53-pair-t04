"""集气管压力 → 桥管翻板总管开度。

找开度**按集气管整体调**，不按炉单独调：一个总管压力控制器把集气管压力偏差
折成一条总管开度令，再由翻板组同时下发给在役各炉。每炉只负责执行、回读和
各自的荒煤气累计量。

快调／缓调两套整定按在役炉况选择，不允许中途抖动：

* 任一在役炉处于装煤／推焦（或压力偏差超过快调门限兜底）→ 快调，高增益、
  大变化率、短驻留，抢时间；
* 全部在役炉处于结焦中期且偏差不大 → 缓调，低增益、小变化率、长驻留，求稳。

比例律围绕偏置开度对称：压力偏低（有被抽稀的趋势）关小，压力偏高（顶压）
开大，目标压力对应偏置开度。每一步都能用同样的输入显式复算，供审计核对。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import CollectorLimits

MODE_FAST = "fast"
MODE_SLOW = "slow"


@dataclass(frozen=True)
class Tuning:
    """一套调节整定。"""

    mode: str
    gain_deg_per_pa: float
    rate_deg_per_s: float
    dwell_s: float

    def as_dict(self) -> dict:
        return {
            "mode": self.mode,
            "gain_deg_per_pa": round(self.gain_deg_per_pa, 4),
            "rate_deg_per_s": round(self.rate_deg_per_s, 3),
            "dwell_s": round(self.dwell_s, 3),
        }


def choose_mode(
    limits: CollectorLimits,
    pressure_pa: float,
    *,
    urgent_ovens: int = 0,
    override: str | None = None,
) -> str:
    """按炉况与压力偏差选择快调或缓调。

    ``override`` 只接受 ``"fast"`` / ``"slow"``，由操作编排显式锁死模式；
    缺省时任一在役炉紧急（装煤／推焦）或压力偏差越限即快调，否则缓调。
    """

    if override == MODE_FAST:
        return MODE_FAST
    if override == MODE_SLOW:
        return MODE_SLOW
    if urgent_ovens > 0:
        return MODE_FAST
    if abs(pressure_pa - limits.pressure_target_pa) > limits.fast_error_pa:
        return MODE_FAST
    return MODE_SLOW


class CollectorPressureController:
    """集气管整体压力 → 总管开度令（无记忆比例律）。"""

    def __init__(self, limits: CollectorLimits) -> None:
        self._limits = limits

    def tuning(self, mode: str) -> Tuning:
        """返回某个模式下的增益／变化率／驻留整定。"""

        if mode == MODE_FAST:
            return Tuning(
                MODE_FAST,
                self._limits.flap_gain_fast_deg_per_pa,
                self._limits.flap_rate_fast_deg_per_s,
                self._limits.flap_dwell_fast_s,
            )
        return Tuning(
            MODE_SLOW,
            self._limits.flap_gain_slow_deg_per_pa,
            self._limits.flap_rate_slow_deg_per_s,
            self._limits.flap_dwell_slow_s,
        )

    def command(self, pressure_pa: float, mode: str) -> float:
        """按当前压力与模式重算总管开度令（0～90 度）。"""

        tuning = self.tuning(mode)
        target = self._limits.flap_bias_deg + tuning.gain_deg_per_pa * (
            pressure_pa - self._limits.pressure_target_pa
        )
        return min(90.0, max(0.0, target))
