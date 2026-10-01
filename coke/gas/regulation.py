"""桥管翻板自动找开度协调器（集气管整体调，按炉分配与记账）。

:class:`GasRegulation` 把整体压力闭环、按炉分配、调节时序、回读诊断和每炉
累计器接成一条每周期推进的链，严格满足定死的层级：

* 找开度只在集气管整体层做（:class:`CollectorPressureController` 唯一闭环）；
* 单炉不持有压力闭环，只接收按炉况权重分配下来的目标开度；
* 快调/缓调由炉况判定，只改变速率倍率，不改层级；
* 每炉动作经过驻留时序，避免来回跳；
* 任一炉回读抖动/超差进入部分失败，立即冻结该炉、停安全开度，整链停止继续
  按目标开度分配，等待显式复位；
* 每炉每周期按同一通流公式累计荒煤气，并可做一致性核对。

协调器不直接写审计，它返回结构化结论；由平台层发布事件，保持组件与审计解耦。
"""

from __future__ import annotations

from ..config import FlapControlLimits
from .condition import decide_tune
from .controller import CollectorPressureController, allocate_opening
from .flap import BridgeFlap
from .latch import FlapLatch
from .reconcile import reconcile_cycle
from .timing import FlapTiming
from .totalizer import OvenGasTotalizer


class OvenChannel:
    """单炉在协调链里的全部状态：翻板、时序、累计、炉况。"""

    def __init__(self, oven: str, limits: FlapControlLimits) -> None:
        self.oven = oven
        self.limits = limits
        self.flap = BridgeFlap(
            FlapLatch(),
            max_deg=limits.flap_max_deg,
            rate_deg_per_s=limits.flap_rate_deg_per_s,
            readback_tol_deg=limits.readback_tol_deg,
            jitter_band_deg=limits.jitter_band_deg,
            jitter_window_s=limits.jitter_window_s,
            jitter_count=int(limits.jitter_count),
            fail_hold_deg=limits.fail_hold_deg,
        )
        self.timing = FlapTiming(limits.dwell_min_s, limits.deadband_deg, limits.min_step_deg)
        self.totalizer = OvenGasTotalizer(
            oven, limits.flow_k_nm3_h, limits.dp_pa, limits.flap_max_deg
        )
        self.phase = "steady"
        self.last_tune = "slow"

    def healthy(self) -> bool:
        return self.flap.healthy()


class GasRegulation:
    """集气管整体压力闭环 + 多炉翻板协调 + 每炉荒煤气累计。"""

    def __init__(
        self,
        oven_ids: list[str] | tuple[str, ...],
        collector_target_pa: float,
        collector_band_pa: float,
        limits: FlapControlLimits,
    ) -> None:
        if not oven_ids:
            raise ValueError("至少要有一炉")
        self._limits = limits
        self._ovens: dict[str, OvenChannel] = {
            oven: OvenChannel(oven, limits) for oven in oven_ids
        }
        self._controller = CollectorPressureController(
            collector_target_pa,
            collector_band_pa,
            gain_per_pa=limits.conductance_gain_per_pa,
            deadband_pa=limits.deadband_pa,
            base_conductance=limits.base_conductance,
            rate_per_s=limits.conductance_rate_per_s,
        )
        self._frozen = False
        self._cycles = 0

    @property
    def limits(self) -> FlapControlLimits:
        return self._limits

    @property
    def ovens(self) -> dict[str, OvenChannel]:
        return self._ovens

    @property
    def total_conductance(self) -> float:
        return self._controller.total_conductance

    def channel(self, oven: str) -> OvenChannel:
        if oven not in self._ovens:
            from ..errors import CokeError

            raise CokeError(f"未登记的炉号: {oven}", component="gas")
        return self._ovens[oven]

    def frozen(self) -> bool:
        """整链是否因回读部分失败而冻结。"""

        return self._frozen

    def set_phase(self, oven: str, phase: str) -> None:
        """设置某炉所处结焦阶段（决定快/缓调）。"""

        self._ovens[oven].phase = phase

    def begin_charge(self, oven: str, ts: float) -> None:
        """某炉开炉次：复位闩锁、开新累计账。"""

        channel = self._ovens[oven]
        channel.flap.reset_latch()
        channel.totalizer.begin_charge(ts)
        channel.timing.reset()

    def clear_fault(self, oven: str) -> bool:
        """显式确认并复位某炉回读故障，全部健康后解除整链冻结。"""

        channel = self._ovens[oven]
        healthy = channel.flap.clear_fault()
        channel.timing.reset()
        if healthy and all(item.healthy() for item in self._ovens.values()):
            self._frozen = False
        return healthy

    def cycle(
        self,
        ts: float,
        measured_pressure_pa: float,
        dt: float,
        *,
        readbacks: dict[str, float] | None = None,
        dp_pa: dict[str, float] | None = None,
    ) -> dict:
        """推进一个控制周期，返回压力判定、各炉开度/时序/累计与一致性核对。"""

        if dt <= 0:
            raise ValueError("步长必须为正")
        self._cycles += 1
        readbacks = readbacks or {}
        dp_pa = dp_pa or {oven: self._limits.dp_pa for oven in self._ovens}

        # 整链冻结时不再按目标开度走，只按各炉当前开度记账，等待人工复位。
        if self._frozen:
            return self._advance_frozen(ts, dt, dp_pa)

        # 1) 唯一的整体压力闭环：误差 → 快/缓调速率 → 总通流份额。
        active_error = self._controller.target_pa - measured_pressure_pa
        weights: dict[str, float] = {}
        for oven, channel in self._ovens.items():
            decision = decide_tune(
                channel.phase,
                active_error,
                fast_multiplier=self._limits.fast_rate_multiplier,
                fast_err_pa=self._limits.deadband_pa * 2.0,
            )
            channel.last_tune = decision.mode
            weights[oven] = decision.rate_multiplier
        fastest = max(weights.values()) if weights else 1.0
        verdict = self._controller.update(measured_pressure_pa, dt, rate_multiplier=fastest)

        # 2) 总份额按炉况权重分到各炉目标角度。
        targets = allocate_opening(verdict.total_conductance, weights, self._limits)

        # 3) 各炉经时序仲裁后驱动翻板并核对回读。
        oven_report: dict[str, dict] = {}
        any_fault = False
        for oven, channel in self._ovens.items():
            timing = channel.timing.evaluate(channel.flap.angle_deg, targets[oven], dt)
            if timing.may_move:
                channel.flap.command(timing.commanded_deg)
            status = channel.flap.step(dt, readback_deg=readbacks.get(oven), ts=ts)
            if channel.flap.fault:
                any_fault = True
            # 4) 按现场真实开度累计本炉荒煤气（故障期挂可疑账）。
            flow = channel.totalizer.accumulate(
                ts,
                channel.flap.readback_deg,
                dp_pa.get(oven, self._limits.dp_pa),
                dt,
                healthy=channel.flap.healthy(),
            )
            current = channel.totalizer.current()
            oven_report[oven] = {
                "phase": channel.phase,
                "tune_mode": channel.last_tune,
                "target_deg": round(targets[oven], 3),
                "commanded_deg": status["commanded_deg"],
                "readback_deg": status["readback_deg"],
                "timing": timing.as_dict(),
                "fault": status["fault"],
                "fault_reason": status["fault_reason"],
                "dp_pa": round(float(dp_pa.get(oven, self._limits.dp_pa)), 2),
                "flow_nm3_h": flow["flow_nm3_h"],
                "volume_nm3": flow["volume_nm3"],
                "total_nm3": current.total_nm3 if current else 0.0,
                "valid_nm3": current.valid_nm3 if current else 0.0,
            }

        if any_fault:
            self._frozen = True

        # 5) 一致性核对（同一周期样本，三量交叉）。
        pressure_sample = {
            "error_pa": verdict.error_pa,
            "wanted_conductance": verdict.wanted_conductance,
            "base_conductance": self._limits.base_conductance,
            "deadband_pa": self._limits.deadband_pa,
        }
        consistency = reconcile_cycle(
            pressure_sample,
            oven_report,
            self._limits,
            any_frozen=self._frozen,
        )

        return {
            "cycle": self._cycles,
            "ts": round(ts, 3),
            "pressure": verdict.as_dict(),
            "total_conductance": round(verdict.total_conductance, 4),
            "frozen": self._frozen,
            "ovens": oven_report,
            "consistency": consistency,
        }

    def _advance_frozen(self, ts: float, dt: float, dp_pa: dict[str, float]) -> dict:
        oven_report: dict[str, dict] = {}
        for oven, channel in self._ovens.items():
            status = channel.flap.step(dt)
            flow = channel.totalizer.accumulate(
                ts,
                channel.flap.readback_deg,
                dp_pa.get(oven, self._limits.dp_pa),
                dt,
                healthy=channel.flap.healthy(),
            )
            current = channel.totalizer.current()
            oven_report[oven] = {
                "phase": channel.phase,
                "tune_mode": channel.last_tune,
                "target_deg": round(channel.flap.commanded_deg, 3),
                "commanded_deg": status["commanded_deg"],
                "readback_deg": status["readback_deg"],
                "timing": channel.timing.evaluate(channel.flap.angle_deg, channel.flap.angle_deg, dt).as_dict(),
                "fault": status["fault"],
                "fault_reason": status["fault_reason"],
                "dp_pa": round(float(dp_pa.get(oven, self._limits.dp_pa)), 2),
                "flow_nm3_h": flow["flow_nm3_h"],
                "volume_nm3": flow["volume_nm3"],
                "total_nm3": current.total_nm3 if current else 0.0,
                "valid_nm3": current.valid_nm3 if current else 0.0,
            }
        return {
            "cycle": self._cycles,
            "ts": round(ts, 3),
            "pressure": None,
            "total_conductance": round(self._controller.total_conductance, 4),
            "frozen": True,
            "ovens": oven_report,
            "consistency": {"ok": False, "findings": [{"check": "chain", "ok": False, "reason": "frozen"}]},
        }

    def seal_charge(self, oven: str, ts: float) -> dict:
        """某炉结束炉次：关翻板并封账，返回封账总量（按炉对账依据）。"""

        channel = self._ovens[oven]
        channel.flap.close()
        record = channel.totalizer.seal_charge(ts)
        return record.as_dict()

    def oven_status(self, oven: str) -> dict:
        """返回单炉对外状态（控制台展示口径）。"""

        channel = self.channel(oven)
        current = channel.totalizer.current()
        return {
            "oven": oven,
            "phase": channel.phase,
            "tune_mode": channel.last_tune,
            "commanded_deg": round(channel.flap.commanded_deg, 3),
            "readback_deg": round(channel.flap.readback_deg, 3),
            "healthy": channel.flap.healthy(),
            "fault_reason": channel.flap.fault_reason,
            "latch_engaged": channel.flap.latch.is_engaged(),
            "moves": channel.timing.moves(),
            "holds": channel.timing.holds(),
            "current_total_nm3": round(current.total_nm3, 3) if current else None,
            "current_valid_nm3": round(current.valid_nm3, 3) if current else None,
            "sealed_charges": len(channel.totalizer.history()),
            "grand_total_nm3": round(channel.totalizer.grand_total_nm3(), 3),
        }

    def status(self) -> dict:
        """返回整链对外状态。"""

        return {
            "frozen": self._frozen,
            "cycles": self._cycles,
            "target_pa": self._controller.target_pa,
            "total_conductance": round(self._controller.total_conductance, 4),
            "ovens": {oven: self.oven_status(oven) for oven in self._ovens},
        }
