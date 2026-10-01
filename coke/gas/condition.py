"""炉况判定：决定该炉翻板"快调抢时间"还是"缓调求稳"。

炉况只影响调节快慢，不改找开度的层级——找开度永远在集气管整体层做。判定
依据该炉在结焦周期里所处阶段与压力偏差：装煤初期荒煤气量暴涨、偏差又大，
必须快调压住；进入成熟稳产期或偏差很小时缓调，避免来回扰动。判定结果是
纯函数，给定同样的阶段、偏差、参数必然得到同样的模式与速率倍率，审计可
逐条复算。
"""

from __future__ import annotations

from dataclasses import dataclass

# 结焦周期阶段：装煤期产气最猛，爬坡期次之，稳产期平缓，末期收尾。
OVEN_PHASES = ("charging", "ramping", "steady", "ending")

TUNE_MODES = ("fast", "slow")


@dataclass(frozen=True)
class TuneDecision:
    """一次炉况判定结论。"""

    phase: str
    pressure_err_pa: float
    mode: str
    rate_multiplier: float
    reason: str

    def as_dict(self) -> dict:
        return {
            "phase": self.phase,
            "pressure_err_pa": round(self.pressure_err_pa, 2),
            "mode": self.mode,
            "rate_multiplier": round(self.rate_multiplier, 3),
            "reason": self.reason,
        }


def decide_tune(
    phase: str,
    pressure_err_pa: float,
    *,
    fast_multiplier: float,
    fast_err_pa: float,
    active_phases: tuple[str, ...] = ("charging", "ramping"),
) -> TuneDecision:
    """按炉况与压力偏差判定快调/缓调。

    ``pressure_err_pa`` 为"目标减实测"：正值说明压力顶不住、需要抢开度。
    处在剧烈产气阶段且偏差超过阈值，或无论阶段偏差已经很大，都判快调；
    其余缓调求稳。
    """

    if phase not in OVEN_PHASES:
        raise ValueError(f"未知炉况阶段: {phase!r}")
    if fast_multiplier < 1.0:
        raise ValueError("快调倍率必须不小于 1")

    err = float(pressure_err_pa)
    urgent = abs(err) >= float(fast_err_pa)
    in_active_phase = phase in active_phases
    if urgent and in_active_phase:
        return TuneDecision(phase, err, "fast", float(fast_multiplier), "active_phase_large_error")
    if urgent:
        return TuneDecision(phase, err, "fast", float(fast_multiplier), "large_error")
    if in_active_phase:
        return TuneDecision(phase, err, "fast", float(fast_multiplier), "active_phase")
    return TuneDecision(phase, err, "slow", 1.0, "steady_phase_small_error")
