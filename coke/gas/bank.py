"""桥管翻板组：按炉持有，总管一条开度令统一下发。

找开度按集气管整体调，因此 :class:`FlapBank` 只接收一条总管开度令，同时
下发给全部在役炉的翻板；按炉的差异只体现在各自的回读、故障冻结与闩锁上。
任一翻板回读跳动（部分失败）只冻结该炉，其余炉继续追随总管令，整体调节不
中断。
"""

from __future__ import annotations

from ..config import CollectorLimits
from .flap import FLAP_CLOSED_DEG, BridgeFlap
from .latch import FlapLatch


class FlapBank:
    """按炉号索引的桥管翻板集合。"""

    def __init__(self, ovens: tuple[str, ...], limits: CollectorLimits) -> None:
        if not ovens:
            raise ValueError("翻板组至少要包含一炉")
        if len(set(ovens)) != len(ovens):
            raise ValueError("炉号不能重复")
        self._limits = limits
        self._flaps: dict[str, BridgeFlap] = {
            oven: BridgeFlap(
                FlapLatch(),
                rate_deg_per_s=limits.flap_rate_fast_deg_per_s,
                readback_tol_deg=limits.flap_readback_tol_deg,
                readback_strikes=limits.flap_readback_strikes,
            )
            for oven in ovens
        }

    def ovens(self) -> list[str]:
        """返回组内全部炉号（登记顺序）。"""

        return list(self._flaps)

    def flap(self, oven: str) -> BridgeFlap:
        """按炉号取翻板，未知炉号抛 ``KeyError``。"""

        return self._flaps[oven]

    def drive(self, active_ovens: list[str], command_deg: float, dt: float) -> dict:
        """把总管开度令下发给在役炉，返回每炉指令／回读与故障情况。"""

        report: dict[str, dict] = {}
        for oven, flap in self._flaps.items():
            target = command_deg if oven in active_ovens else FLAP_CLOSED_DEG
            actual = flap.drive_to(target, dt)
            report[oven] = {
                "command_deg": round(flap.command_deg, 2),
                "readback_deg": round(flap.readback_deg(), 2),
                "angle_deg": round(actual, 2),
                "readback_fault": flap.readback_fault(),
            }
        return report

    def active_faults(self) -> dict[str, bool]:
        """返回每炉回读故障状态。"""

        return {oven: flap.readback_fault() for oven, flap in self._flaps.items()}

    def reset_fault(self, oven: str) -> bool:
        """复位某炉回读故障，返回该炉复位前是否处于故障。"""

        return self._flaps[oven].reset_readback_fault()

    def restore_positions(self, positions: dict[str, float]) -> None:
        """按炉恢复翻板物理开度（跨重启从审计最后一条流水重建）。"""

        for oven, deg in positions.items():
            flap = self._flaps.get(oven)
            if flap is not None:
                flap.force_position(float(deg))

    def inject_readback(self, oven: str, angle_deg: float) -> None:
        """注入某炉回读值（故障演练用）。"""

        self._flaps[oven].inject_readback(angle_deg)
