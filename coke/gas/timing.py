"""翻板调节时序：防来回跳的驻留与死区仲裁。

自动找开度不能"差一点就动、刚动完又反向动"。时序器在每周期只回答两件事：

1. 目标开度的变化量是否小到落在死区内（小偏差不动）；
2. 上一次实际动作后是否已经驻留够最短时间（没到点不许再动，更不许反向）。

只有越过死区 **且** 驻留计时到点，才允许输出一个不小于最小步长、方向与当前
需求一致的指令；驻留期内目标只更新、不动作。时序器不持有翻板本体，纯做
"该不该动、动多少、朝哪动"的显式判定，便于审计逐步复算。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TimingVerdict:
    """本周期时序仲裁结论。"""

    may_move: bool
    direction: int
    commanded_deg: float
    requested_deg: float
    reason: str
    dwell_remaining_s: float

    def as_dict(self) -> dict:
        return {
            "may_move": self.may_move,
            "direction": self.direction,
            "commanded_deg": round(self.commanded_deg, 3),
            "requested_deg": round(self.requested_deg, 3),
            "reason": self.reason,
            "dwell_remaining_s": round(self.dwell_remaining_s, 3),
        }


class FlapTiming:
    """驻留计时 + 死区 + 最小步长 + 方向锁定的时序仲裁。"""

    def __init__(
        self,
        dwell_min_s: float,
        deadband_deg: float,
        min_step_deg: float,
    ) -> None:
        self._dwell_min_s = max(0.0, float(dwell_min_s))
        self._deadband_deg = max(0.0, float(deadband_deg))
        self._min_step_deg = max(0.0, float(min_step_deg))
        self._since_move_s = self._dwell_min_s
        self._last_direction = 0
        self._moves = 0
        self._holds = 0

    @property
    def dwell_min_s(self) -> float:
        return self._dwell_min_s

    def moves(self) -> int:
        """返回累计放行动作次数。"""

        return self._moves

    def holds(self) -> int:
        """返回累计驻留保持次数。"""

        return self._holds

    def dwell_remaining(self) -> float:
        """返回距下次允许动作还需驻留的秒数。"""

        return max(0.0, self._dwell_min_s - self._since_move_s)

    def reset(self) -> None:
        """复位驻留计时（如失败恢复后重新起算）。"""

        self._since_move_s = self._dwell_min_s
        self._last_direction = 0

    def evaluate(self, current_deg: float, requested_deg: float, dt: float) -> TimingVerdict:
        """根据当前/目标开度与步长，仲裁本周期是否允许动作。"""

        if dt <= 0:
            raise ValueError("步长必须为正")
        self._since_move_s += float(dt)
        delta = float(requested_deg) - float(current_deg)
        direction = 1 if delta > 0 else (-1 if delta < 0 else 0)
        remaining = self.dwell_remaining()

        if direction == 0 or abs(delta) <= self._deadband_deg:
            self._holds += 1
            return TimingVerdict(False, 0, float(current_deg), float(requested_deg), "in_deadband", remaining)

        if self._since_move_s < self._dwell_min_s:
            self._holds += 1
            return TimingVerdict(False, direction, float(current_deg), float(requested_deg), "dwell_not_elapsed", remaining)

        # 驻留期内目标反向也算来回跳：方向锁与上一次动作相反时仍按驻留处理。
        if self._last_direction != 0 and direction == -self._last_direction and remaining > 0.0:
            self._holds += 1
            return TimingVerdict(False, direction, float(current_deg), float(requested_deg), "direction_locked", remaining)

        magnitude = max(abs(delta), self._min_step_deg)
        commanded = float(current_deg) + direction * magnitude
        self._since_move_s = 0.0
        self._last_direction = direction
        self._moves += 1
        return TimingVerdict(True, direction, commanded, float(requested_deg), "move_commanded", 0.0)
