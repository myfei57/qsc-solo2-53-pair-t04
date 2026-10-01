"""每炉荒煤气抽走量累计（按炉对账口径）。

一炉翻板在某开度下被抽走的荒煤气流量，按和总开度分配同一条通流曲线复算：

    q = K * sin(theta) * sqrt(dp / dp_ref)

``K`` 为每炉满开额定流量（Nm³/h），``dp`` 为桥管与集气管之间的实际压差，
``dp_ref`` 为额定压差。累计器每个控制周期把瞬时流量乘步长（小时换算）加进
本炉本炉次的累计量，并保留一个"有效累计"和一个带质量标记的累计，供按炉
对账时区分"调节正常"与"回读故障冻结期"。累计只追加并可在新炉次开始时
封账，封账后该炉次总量不再变化。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class ChargeTotal:
    """一个炉次的荒煤气累计账。"""

    oven: str
    charge_seq: int
    started_ts: float
    closed_ts: float | None = None
    total_nm3: float = 0.0
    valid_nm3: float = 0.0
    suspect_nm3: float = 0.0
    frozen_s: float = 0.0
    samples: int = 0
    sealed: bool = False
    final_total_nm3: float | None = None

    def seal(self, ts: float) -> float:
        """封账：记录结束时刻并冻结总量，返回封账总量。"""

        if not self.sealed:
            self.closed_ts = float(ts)
            self.sealed = True
            self.final_total_nm3 = self.total_nm3
        return self.total_nm3

    def as_dict(self) -> dict:
        return {
            "oven": self.oven,
            "charge_seq": self.charge_seq,
            "started_ts": round(self.started_ts, 3),
            "closed_ts": round(self.closed_ts, 3) if self.closed_ts is not None else None,
            "total_nm3": round(self.total_nm3, 3),
            "valid_nm3": round(self.valid_nm3, 3),
            "suspect_nm3": round(self.suspect_nm3, 3),
            "frozen_s": round(self.frozen_s, 3),
            "samples": self.samples,
            "sealed": self.sealed,
            "final_total_nm3": round(self.final_total_nm3, 3)
            if self.final_total_nm3 is not None
            else None,
        }


def instantaneous_flow_nm3h(
    angle_deg: float,
    k_nm3_h: float,
    dp_pa: float,
    dp_ref_pa: float,
    *,
    max_deg: float = 85.0,
) -> float:
    """按通流曲线与压差计算瞬时流量（Nm³/h），非负。

    通流份额与开度分配共用同一条 ``sin`` 曲线（见
    :meth:`coke.config.FlapControlLimits.conductance`），此处以等价闭式公式
    复算，保证"开度"和"抽走量"在审计里口径一致。
    """

    if k_nm3_h < 0 or dp_ref_pa <= 0:
        raise ValueError("流量系数必须非负、额定压差必须为正")
    clipped = max(0.0, min(max_deg, float(angle_deg)))
    if clipped <= 0.0:
        return 0.0
    pressure_factor = math.sqrt(max(0.0, float(dp_pa)) / float(dp_ref_pa))
    conductance = math.sin(math.radians(clipped))
    return float(k_nm3_h) * conductance * pressure_factor


class OvenGasTotalizer:
    """单炉跨炉次的荒煤气累计器。"""

    def __init__(self, oven: str, k_nm3_h: float, dp_ref_pa: float, max_deg: float) -> None:
        self._oven = oven
        self._k = float(k_nm3_h)
        self._dp_ref = float(dp_ref_pa)
        self._max_deg = float(max_deg)
        self._seq = 0
        self._current: ChargeTotal | None = None
        self._history: list[ChargeTotal] = []
        self._implicit = False

    @property
    def oven(self) -> str:
        return self._oven

    @property
    def charge_seq(self) -> int:
        return self._seq

    def begin_charge(self, ts: float) -> ChargeTotal:
        """开始一个新炉次；若上一炉次未封账则先封账。

        显式开 open 账是新炉次的唯一起点；自动循环为不丢账而隐式补建的账，
        在首次显式开账时作废重开，不占炉次序号。
        """

        if self._current is not None and not self._current.sealed and not self._implicit:
            self.seal_charge(ts)
        if self._implicit and self._current is not None:
            # 丢弃隐式补建账（未落历史），从序号 1 正式起算。
            self._seq -= 1
            self._current = None
        self._seq += 1
        self._implicit = False
        self._current = ChargeTotal(self._oven, self._seq, float(ts))
        return self._current

    def accumulate(
        self,
        ts: float,
        angle_deg: float,
        dp_pa: float,
        dt: float,
        *,
        healthy: bool = True,
    ) -> dict:
        """累计一个周期，返回本周期流量明细。"""

        if dt <= 0:
            raise ValueError("步长必须为正")
        if self._current is None:
            # 控制循环在未显式开账时也不能丢量，隐式补建一笔占位账。
            self._seq += 1
            self._implicit = True
            self._current = ChargeTotal(self._oven, self._seq, float(ts))
        assert self._current is not None
        flow = instantaneous_flow_nm3h(angle_deg, self._k, dp_pa, self._dp_ref, max_deg=self._max_deg)
        volume = flow * float(dt) / 3600.0
        self._current.total_nm3 += volume
        self._current.samples += 1
        if healthy:
            self._current.valid_nm3 += volume
        else:
            # 回读故障冻结期也按最后指令开度估算，但单独挂账，对账时不计入有效量。
            self._current.suspect_nm3 += volume
            self._current.frozen_s += float(dt)
        return {
            "oven": self._oven,
            "ts": round(float(ts), 3),
            "flow_nm3_h": round(flow, 3),
            "volume_nm3": round(volume, 6),
            "healthy": healthy,
            "total_nm3": round(self._current.total_nm3, 3),
        }

    def seal_charge(self, ts: float) -> ChargeTotal:
        """封账当前炉次并转入历史，返回封账记录。"""

        if self._current is None:
            raise ValueError(f"{self._oven} 没有进行中的炉次")
        record = self._current
        record.seal(ts)
        self._history.append(record)
        self._current = None
        return record

    def current(self) -> ChargeTotal | None:
        """返回进行中的炉次账（无则 ``None``）。"""

        return self._current

    def history(self) -> list[ChargeTotal]:
        """返回已封账的炉次账（按炉次升序）。"""

        return list(self._history)

    def grand_total_nm3(self) -> float:
        """返回该炉全部已封账炉次的累计总量。"""

        return sum(item.total_nm3 for item in self._history)
