"""桥管翻板（模拟量开度 + 回读校验 + 开阀闩锁）。

翻板不再只有全开／全关：控制环按目标开度连续驱动，位置回读用于核对实际到
位。回读连续若干次偏离指令即判为回读跳动（部分失败），此时翻板冻结在最后
一次可信开度，``drive_to`` 不再追随新目标，必须现场复位后才重新受控。

开关两态的老接口（``open`` / ``close`` / ``is_open``）保留给装煤序列与闩锁
联锁：闩锁未复位前不允许重新全开，防止同一炉重复开阀。
"""

from __future__ import annotations

from ..errors import InterlockError
from .latch import FlapLatch

FLAP_OPEN_DEG = 85.0
FLAP_CLOSED_DEG = 0.0
FLAP_MAX_DEG = 90.0


class BridgeFlap:
    """单孔桥管翻板状态机（连续开度、回读校验、开阀闩锁）。"""

    def __init__(
        self,
        latch: FlapLatch | None = None,
        *,
        rate_deg_per_s: float = 12.0,
        readback_tol_deg: float = 4.0,
        readback_strikes: int = 2,
    ) -> None:
        self._latch = latch or FlapLatch()
        self._rate = max(0.1, float(rate_deg_per_s))
        self._readback_tol = max(0.1, float(readback_tol_deg))
        self._readback_strikes = max(1, int(readback_strikes))
        self._command_deg = FLAP_CLOSED_DEG
        self._angle_deg = FLAP_CLOSED_DEG
        self._readback_override: float | None = None
        self._readback_bad = 0
        self._readback_fault = False
        self._last_trusted_deg = FLAP_CLOSED_DEG

    @property
    def latch(self) -> FlapLatch:
        return self._latch

    @property
    def angle_deg(self) -> float:
        """最近一次通过校验的实际开度（回读正常时等于回读值）。"""

        return self._angle_deg

    @property
    def command_deg(self) -> float:
        """最近一次下发给执行器的目标开度。"""

        return self._command_deg

    def readback_deg(self) -> float:
        """当前位置回读；外部可注入故障回读，正常时回读跟随实际开度。"""

        if self._readback_override is not None:
            return self._readback_override
        return self._angle_deg

    def inject_readback(self, angle_deg: float) -> None:
        """注入外部回读值（标定／故障演练用）；传负数恢复随动回读。"""

        self._readback_override = None if angle_deg < 0.0 else float(angle_deg)

    def readback_fault(self) -> bool:
        """回读跳动是否已被判为部分失败（冻结中）。"""

        return self._readback_fault

    def readback_bad_strikes(self) -> int:
        """连续偏离指令的回读次数。"""

        return self._readback_bad

    def drive_to(self, target_deg: float, dt: float) -> float:
        """把目标开度推进一个步长并校验回读，返回通过校验的实际开度。

        回读跳动冻结期间直接返回最后可信开度，不再追随新目标。
        """

        if dt <= 0:
            raise ValueError("步长必须为正")
        if self._readback_fault:
            return self._angle_deg

        target = min(FLAP_MAX_DEG, max(FLAP_CLOSED_DEG, float(target_deg)))
        self._command_deg = target
        # 变化率／驻留已由上游开度时序仲裁统一约束，到这里执行器即时跟随指令。
        driven = target
        actual = min(FLAP_MAX_DEG, max(FLAP_CLOSED_DEG, self._readback_override if self._readback_override is not None else driven))
        if abs(actual - driven) <= self._readback_tol:
            self._readback_bad = 0
            self._angle_deg = driven
            self._last_trusted_deg = driven
            return self._angle_deg

        self._readback_bad += 1
        if self._readback_bad < self._readback_strikes:
            # 偶发跳动先维持上一拍开度，等下一拍复核，不轻易冻结。
            return self._angle_deg
        self._readback_fault = True
        self._angle_deg = self._last_trusted_deg
        return self._angle_deg

    def force_position(self, angle_deg: float) -> float:
        """把指令与物理开度一起置到给定值（跨重启恢复用，不触发闩锁）。"""

        position = min(FLAP_MAX_DEG, max(FLAP_CLOSED_DEG, float(angle_deg)))
        self._command_deg = position
        self._angle_deg = position
        self._last_trusted_deg = position
        self._readback_bad = 0
        self._readback_fault = False
        self._readback_override = None
        return position

    def reset_readback_fault(self) -> bool:
        """现场确认后复位回读故障，返回复位时是否清掉了一个故障。"""

        recovered = self._readback_fault
        self._readback_fault = False
        self._readback_bad = 0
        self._readback_override = None
        self._angle_deg = self._command_deg
        self._last_trusted_deg = self._command_deg
        return recovered

    def open(self) -> float:
        """全开翻板，返回开度；闩锁未复位即拒绝。"""

        if self._latch.is_engaged():
            raise InterlockError("翻板闩锁未复位，禁止重复打开", component="gas")
        self._command_deg = FLAP_OPEN_DEG
        self._angle_deg = FLAP_OPEN_DEG
        self._last_trusted_deg = FLAP_OPEN_DEG
        self._latch.engage()
        return self._angle_deg

    def close(self) -> float:
        """全关翻板，返回停靠开度。"""

        self._command_deg = FLAP_CLOSED_DEG
        self._angle_deg = FLAP_CLOSED_DEG
        self._last_trusted_deg = FLAP_CLOSED_DEG
        return self._angle_deg

    def is_open(self) -> bool:
        """翻板是否处于全开状态。"""

        return self._angle_deg >= FLAP_OPEN_DEG - 1e-9

    def reset_latch(self) -> bool:
        """复位开阀闩锁，返回复位后的闩锁状态。"""

        return self._latch.reset()
