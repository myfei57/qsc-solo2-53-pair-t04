"""桥管开度时序仲裁。

控制器每个周期都能算出一条新开度令，但执行器不能跟着来回跳。
:class:`OpeningSequencer` 在指令与执行器之间加三道时序约束，全部可按输入
显式复算，便于审计核对每一拍为什么动／为什么不动：

1. **变化率限制**：单个周期内指令变化不超过 ``rate × dt``；
2. **死区**：变化量不超过死区视为无变化，沿用上一条指令；
3. **最小驻留**：一次改变后至少停留 ``dwell`` 秒才允许同向再变；改变方向
   （开 ↔ 关）前必须停留更长的 ``reverse_dwell`` 秒，杜绝来回跳。
"""

from __future__ import annotations

from .controller import Tuning

DIR_UP = 1
DIR_DOWN = -1
DIR_FLAT = 0


class OpeningSequencer:
    """带变化率、死区与驻留的开度指令时序仲裁。"""

    def __init__(
        self,
        deadband_deg: float,
        reverse_dwell_s: float,
        initial_deg: float = 0.0,
    ) -> None:
        self._deadband_deg = max(0.0, float(deadband_deg))
        self._reverse_dwell = max(0.0, float(reverse_dwell_s))
        self._committed = min(90.0, max(0.0, float(initial_deg)))
        self._started = False
        self._last_change_ts: float | None = None
        self._direction = DIR_FLAT

    @property
    def committed_deg(self) -> float:
        """最近一条真正放行给执行器的开度令。"""

        return self._committed

    def last_change_ts(self) -> float | None:
        """最近一次改变指令的时刻。"""

        return self._last_change_ts

    def restore(self, committed_deg: float, last_change_ts: float | None, direction: int = 0) -> None:
        """从审计流水恢复仲裁状态（跨重启用）。"""

        self._committed = min(90.0, max(0.0, float(committed_deg)))
        self._started = True
        self._last_change_ts = last_change_ts
        self._direction = direction

    def step(self, ts: float, wanted_deg: float, tuning: Tuning, dt: float) -> float:
        """仲裁一条开度令，返回本拍真正放行的开度。"""

        if dt <= 0:
            raise ValueError("步长必须为正")
        wanted = min(90.0, max(0.0, float(wanted_deg)))
        # 第一道：变化率限制（首个周期同样限速，从初始开度平滑起步）。
        travel = tuning.rate_deg_per_s * dt
        if wanted > self._committed + travel:
            wanted = self._committed + travel
        elif wanted < self._committed - travel:
            wanted = self._committed - travel
        delta = wanted - self._committed
        # 第二道：死区。
        if abs(delta) <= self._deadband_deg:
            self._started = True
            return self._committed

        direction = DIR_UP if delta > 0 else DIR_DOWN
        if not self._started or self._last_change_ts is None:
            elapsed = float("inf")
        else:
            elapsed = ts - self._last_change_ts
        dwell = tuning.dwell_s
        if self._direction != DIR_FLAT and direction != self._direction:
            dwell = max(dwell, self._reverse_dwell)
        # 第三道：最小驻留（反向时取反向驻留）；首个周期不受驻留约束。
        if self._started and elapsed < dwell:
            return self._committed

        self._committed = min(90.0, max(0.0, wanted))
        self._last_change_ts = ts
        self._direction = direction
        self._started = True
        return self._committed
