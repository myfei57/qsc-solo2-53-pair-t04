"""按炉荒煤气台账：开账、积分、关账。

一炉一个账期：装煤相关动作触发 ``open_account``，推焦出炉触发
``close_account``。账期内每个控制周期按翻板回读开度积分；关账时把本炉总量
定格成一张账单，供按炉对账。账期状态、账单与逐期快照都能直接打印进审计。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import CollectorLimits
from .meter import OvenGasMeter

CONDITIONS = ("charging", "pushing", "coking", "idle")


@dataclass(frozen=True)
class OvenGasBill:
    """一张按炉荒煤气账单。"""

    oven: str
    opened_ts: float
    closed_ts: float
    total_m3: float
    final_deg: float
    readback_fault_at_close: bool

    def as_dict(self) -> dict:
        return {
            "oven": self.oven,
            "opened_ts": round(self.opened_ts, 3),
            "closed_ts": round(self.closed_ts, 3),
            "duration_s": round(self.closed_ts - self.opened_ts, 3),
            "total_m3": round(self.total_m3, 3),
            "final_deg": round(self.final_deg, 2),
            "readback_fault_at_close": self.readback_fault_at_close,
        }


@dataclass
class _Account:
    oven: str
    opened_ts: float
    condition: str
    meter: OvenGasMeter
    samples: int = 0
    last_deg: float = 0.0
    readback_fault: bool = False
    last_volume_m3: float = 0.0


class GasLedger:
    """按炉管理在役账期与已关账单。"""

    def __init__(self, limits: CollectorLimits) -> None:
        self._limits = limits
        self._accounts: dict[str, _Account] = {}
        self._bills: list[OvenGasBill] = []

    def open_account(self, oven: str, ts: float, condition: str = "charging") -> bool:
        """给一炉开账；已在账期内返回 ``False``，不重复开账。"""

        if condition not in CONDITIONS:
            raise ValueError(f"未知炉况: {condition}")
        if oven in self._accounts:
            return False
        self._accounts[oven] = _Account(oven, float(ts), condition, OvenGasMeter(self._limits))
        return True

    def set_condition(self, oven: str, condition: str) -> bool:
        """更新在役炉炉况（决定快调／缓调）；炉不在账期返回 ``False``。"""

        if condition not in CONDITIONS:
            raise ValueError(f"未知炉况: {condition}")
        account = self._accounts.get(oven)
        if account is None:
            return False
        account.condition = condition
        return True

    def condition(self, oven: str) -> str | None:
        """返回在役炉炉况，未开账返回 ``None``。"""

        account = self._accounts.get(oven)
        return None if account is None else account.condition

    def active_ovens(self) -> list[str]:
        """返回当前在账期内的炉号（开账顺序）。"""

        return list(self._accounts)

    def urgent_ovens(self) -> list[str]:
        """返回处于装煤／推焦等紧急炉况的在役炉号。"""

        return [oven for oven, account in self._accounts.items() if account.condition in ("charging", "pushing")]

    def accumulate(self, oven: str, readback_deg: float, dt: float) -> float:
        """给在役炉累加一个周期的量；炉未开账不计并返回 0。"""

        account = self._accounts.get(oven)
        if account is None:
            return 0.0
        volume = account.meter.accumulate(readback_deg, dt)
        account.samples += 1
        account.last_deg = float(readback_deg)
        account.last_volume_m3 = volume
        return volume

    def mark_fault(self, oven: str, readback_fault: bool) -> None:
        """记录本拍翻板回读故障状态，随账期定格进账单。"""

        account = self._accounts.get(oven)
        if account is not None:
            account.readback_fault = bool(readback_fault)

    def total(self, oven: str) -> float:
        """返回在役炉当前累计量；未开账返回 0。"""

        account = self._accounts.get(oven)
        return 0.0 if account is None else account.meter.total_m3

    def close_account(self, oven: str, ts: float) -> OvenGasBill | None:
        """关账并定格账单；炉未开账返回 ``None``。"""

        account = self._accounts.pop(oven, None)
        if account is None:
            return None
        bill = OvenGasBill(
            oven=account.oven,
            opened_ts=account.opened_ts,
            closed_ts=float(ts),
            total_m3=account.meter.total_m3,
            final_deg=account.last_deg,
            readback_fault_at_close=account.readback_fault,
        )
        self._bills.append(bill)
        return bill

    def bills(self, oven: str | None = None) -> list[OvenGasBill]:
        """返回已关账单，可按炉过滤。"""

        if oven is None:
            return list(self._bills)
        return [bill for bill in self._bills if bill.oven == oven]

    def restore(self, gas_events: list[dict]) -> dict:
        """从审计流水（``gas_account_opened`` / ``gas_cycle`` / ``gas_billed``）
        重建在役账期与已关账单。重复调用先清空，保证幂等。
        """

        self._accounts = {}
        self._bills = []
        for item in sorted(gas_events, key=lambda e: float(e.get("ts", 0.0))):
            action = item.get("action", "")
            oven = str(item.get("oven", ""))
            payload = item.get("payload", {})
            ts = float(item.get("ts", 0.0))
            if action == "gas_account_opened" and oven and oven not in self._accounts:
                account = _Account(oven, ts, "coking", OvenGasMeter(self._limits))
                self._accounts[oven] = account
            elif action == "gas_cycle":
                for oven_id, ov in dict(payload.get("ovens", {})).items():
                    if not ov.get("active", False):
                        continue
                    account = self._accounts.get(oven_id)
                    if account is None:
                        account = _Account(oven_id, ts, str(ov.get("condition") or "coking"), OvenGasMeter(self._limits))
                        self._accounts[oven_id] = account
                    account.condition = str(ov.get("condition") or account.condition)
                    account.meter.seed(float(ov.get("total_m3", 0.0)))
                    account.samples += 1
                    account.last_deg = float(ov.get("angle_deg", 0.0))
                    account.readback_fault = bool(ov.get("readback_fault", False))
            elif action == "gas_billed" and oven:
                account = self._accounts.pop(oven, None)
                bill = OvenGasBill(
                    oven=oven,
                    opened_ts=float(payload.get("opened_ts", ts)),
                    closed_ts=float(payload.get("closed_ts", ts)),
                    total_m3=float(payload.get("total_m3", 0.0)),
                    final_deg=float(payload.get("final_deg", 0.0)),
                    readback_fault_at_close=bool(payload.get("readback_fault_at_close", False)),
                )
                self._bills.append(bill)
        return {"active": list(self._accounts), "bills": len(self._bills)}

    def live(self) -> dict:
        """返回在役炉实时累计量与炉况，供控制台展示。"""

        return {
            oven: {
                "condition": account.condition,
                "total_m3": round(account.meter.total_m3, 3),
                "samples": account.samples,
                "last_deg": round(account.last_deg, 2),
                "last_volume_m3": round(account.last_volume_m3, 5),
                "readback_fault": account.readback_fault,
            }
            for oven, account in self._accounts.items()
        }
