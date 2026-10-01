"""带回读校验与抖动诊断的连续开度翻板。

取代只有开/关两态的旧翻板：本类维护"指令开度"和"回读开度"两个量。每个
周期驱动机构按速率向指令开度运动，再用现场回读核对——回读与指令长期对不
上属于卡涩/掉电这类硬故障；回读在小范围内高频来回跳则属于"部分失败"，
此时既不能盲目继续追目标开度，也不能当成正常，而是冻结本炉指令、停在故障
安全开度并置故障标记，等待显式复位。
"""

from __future__ import annotations

from collections import deque

from .latch import FlapLatch

FLAP_OPEN_DEG = 85.0
FLAP_CLOSED_DEG = 0.0


class ReadbackMonitor:
    """回读偏差与短时抖动诊断。"""

    def __init__(
        self,
        tol_deg: float,
        jitter_band_deg: float,
        jitter_window_s: float,
        jitter_count: int,
    ) -> None:
        self._tol = max(0.0, float(tol_deg))
        self._band = max(0.0, float(jitter_band_deg))
        self._window = max(0.1, float(jitter_window_s))
        self._count = max(2, int(jitter_count))
        self._samples: deque[tuple[float, float]] = deque()

    def reset(self) -> None:
        self._samples.clear()

    def record(self, ts: float, readback_deg: float) -> None:
        """压入一个回读样本并丢弃窗口外旧样本。"""

        self._samples.append((float(ts), float(readback_deg)))
        while self._samples and float(ts) - self._samples[0][0] > self._window:
            self._samples.popleft()

    def deviation_ok(self, commanded_deg: float) -> bool:
        """最新回读是否落在指令容差内。"""

        if not self._samples:
            return True
        return abs(self._samples[-1][1] - float(commanded_deg)) <= self._tol

    def is_jittering(self) -> bool:
        """窗口内回读是否在小带内高频来回跳（换向次数达到阈值）。"""

        if len(self._samples) < self._count:
            return False
        values = [item[1] for item in self._samples]
        span = max(values) - min(values)
        if span > self._band or span <= 0.0:
            return False
        reversals = 0
        direction = 0
        for previous, current in zip(values, values[1:]):
            step = 1 if current > previous else (-1 if current < previous else 0)
            if step != 0 and direction != 0 and step != direction:
                reversals += 1
            if step != 0:
                direction = step
        return reversals + 1 >= self._count

    def latest(self) -> float | None:
        """返回最新回读值。"""

        return self._samples[-1][1] if self._samples else None


class BridgeFlap:
    """桥管连续翻板：指令/回读双量 + 闩锁 + 回读故障冻结。"""

    def __init__(
        self,
        latch: FlapLatch | None = None,
        *,
        max_deg: float = FLAP_OPEN_DEG,
        rate_deg_per_s: float = 4.0,
        readback_tol_deg: float = 3.0,
        jitter_band_deg: float = 2.0,
        jitter_window_s: float = 6.0,
        jitter_count: int = 4,
        fail_hold_deg: float = 12.0,
    ) -> None:
        self._latch = latch or FlapLatch()
        self._max = float(max_deg)
        self._rate = max(0.1, float(rate_deg_per_s))
        self._fail_hold = max(0.0, min(self._max, float(fail_hold_deg)))
        self._commanded = FLAP_CLOSED_DEG
        self._readback = FLAP_CLOSED_DEG
        self._monitor = ReadbackMonitor(
            readback_tol_deg, jitter_band_deg, jitter_window_s, jitter_count
        )
        self._fault = False
        self._fault_reason = ""

    @property
    def latch(self) -> FlapLatch:
        return self._latch

    @property
    def angle_deg(self) -> float:
        """对外口径的"当前开度"取回读值（现场真实位置）。"""

        return self._readback

    @property
    def commanded_deg(self) -> float:
        return self._commanded

    @property
    def readback_deg(self) -> float:
        return self._readback

    @property
    def fault(self) -> bool:
        return self._fault

    @property
    def fault_reason(self) -> str:
        return self._fault_reason

    def healthy(self) -> bool:
        """翻板回读是否正常（无故障）。"""

        return not self._fault

    def is_open(self) -> bool:
        """翻板是否处于全开状态（沿用旧两态口径）。"""

        return self._readback >= self._max - 1e-9

    def reset_latch(self) -> bool:
        return self._latch.reset()

    def clear_fault(self) -> bool:
        """显式复位回读故障，返回复位后的健康状态。"""

        self._fault = False
        self._fault_reason = ""
        self._monitor.reset()
        return not self._fault

    def open(self) -> float:
        """两态全开（装煤序列兼容入口），闩锁未复位即拒绝。"""

        if self._latch.is_engaged():
            from ..errors import InterlockError

            raise InterlockError("翻板闩锁未复位，禁止重复打开", component="gas")
        self._commanded = self._max
        self._readback = self._max
        self._latch.engage()
        return self._readback

    def close(self) -> float:
        """两态全关，返回停靠开度。"""

        self._commanded = FLAP_CLOSED_DEG
        self._readback = FLAP_CLOSED_DEG
        return self._readback

    def command(self, target_deg: float) -> float:
        """登记目标指令开度（夹紧到合法行程），返回夹紧后的目标。"""

        if self._fault:
            # 部分失败时拒绝继续按目标开度走，维持故障安全位。
            return self._commanded
        self._commanded = max(0.0, min(self._max, float(target_deg)))
        return self._commanded

    def step(self, dt: float, readback_deg: float | None = None, ts: float = 0.0) -> dict:
        """推进一个周期：机构按速率运动，再核对回读并诊断抖动。

        健康时回读跟随指令（受速率约束）；传入现场 ``readback_deg`` 时以现场
        为准。回读持续超差或窗口内抖动即冻结在故障安全开度，此后
        :meth:`command` 不再接受新目标，直到 :meth:`clear_fault`。
        """

        if dt <= 0:
            raise ValueError("步长必须为正")

        if self._fault:
            return self._status(reason="fault_held")

        travel = self._rate * dt
        gap = self._commanded - self._readback
        if abs(gap) <= travel:
            self._readback = self._commanded
        else:
            self._readback += travel if gap > 0 else -travel

        if readback_deg is not None:
            self._monitor.record(ts, float(readback_deg))
            # 抖动（部分失败）优先判定：回读在小带内高频换向时即便均值接近
            # 指令也属于部分失败，不能当成普通超差，更不能继续追目标开度。
            if self._monitor.is_jittering():
                return self._freeze("readback_jitter")
            if not self._monitor.deviation_ok(self._commanded):
                return self._freeze("readback_deviation")
            self._readback = float(readback_deg)

        return self._status(reason="tracking")

    def _freeze(self, reason: str) -> dict:
        self._fault = True
        self._fault_reason = reason
        self._commanded = self._fail_hold
        self._readback = self._fail_hold
        return self._status(reason=f"frozen_{reason}")

    def _status(self, *, reason: str) -> dict:
        return {
            "commanded_deg": round(self._commanded, 3),
            "readback_deg": round(self._readback, 3),
            "healthy": not self._fault,
            "fault": self._fault,
            "fault_reason": self._fault_reason,
            "reason": reason,
        }
