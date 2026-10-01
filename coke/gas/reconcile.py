"""压力、翻板开度、每炉累计量三者的一致性核对。

审计要能独立回答"这条自动调节链自相矛盾吗"。:func:`reconcile_cycle` 在同一
时间网格上取一条控制周期样本，做四类核对：

1. **指令↔回读一致**：每炉翻板回读是否落在指令容差内（否则属于回读故障）；
2. **开度↔压力链一致**：整体总份额方向是否与压力偏差方向一致（压力高却在
   关小，或压力低却在开大，判反向异常；死区/冻结期除外）；
3. **开度↔累计量一致**：用记录的开度、压差按同一条流量公式重算瞬时流量，
   与记账瞬时流量比对，超过容差说明记账口径被污染；
4. **每炉总量闭合**：各炉封账总量之和与全段荒煤气入口总量的偏差是否在容差
   内（按炉对账的总量平衡）。

核对函数是纯函数，只读取样本、不改动状态，结论可落审计也可供控制台展示。
"""

from __future__ import annotations

import math

from ..config import FlapControlLimits


def _check_command_readback(ovens: dict[str, dict], tol_deg: float) -> list[dict]:
    findings: list[dict] = []
    for oven, sample in ovens.items():
        if sample.get("fault"):
            findings.append(
                {"check": "command_readback", "oven": oven, "ok": False, "reason": sample.get("fault_reason", "fault")}
            )
            continue
        gap = abs(float(sample["commanded_deg"]) - float(sample["readback_deg"]))
        findings.append(
            {
                "check": "command_readback",
                "oven": oven,
                "ok": gap <= tol_deg,
                "deviation_deg": round(gap, 3),
            }
        )
    return findings


def _check_pressure_opening(pressure: dict, frozen: bool) -> dict:
    error = float(pressure["error_pa"])
    # 用"未经斜率限制的期望方向"判定，避免斜率追赶期的瞬时反向被误报；
    # 真正的逻辑反向是：压力高却想关小、或压力低却想开大。
    wanted = float(pressure["wanted_conductance"])
    baseline = float(pressure["base_conductance"])
    wanted_delta = wanted - baseline
    deadband = abs(error) <= float(pressure["deadband_pa"])
    if frozen or deadband or abs(wanted_delta) < 1e-9:
        return {"check": "pressure_opening", "ok": True, "reason": "deadband_or_hold"}
    aligned = (error > 0) == (wanted_delta > 0)
    return {
        "check": "pressure_opening",
        "ok": aligned,
        "error_pa": round(error, 2),
        "wanted_delta": round(wanted_delta, 5),
        "reason": "aligned" if aligned else "reverse_action",
    }


def _check_opening_flow(ovens: dict[str, dict], limits: FlapControlLimits, rel_tol: float) -> list[dict]:
    findings: list[dict] = []
    for oven, sample in ovens.items():
        angle = float(sample["readback_deg"])
        pressure_factor = math.sqrt(max(0.0, float(sample["dp_pa"])) / limits.dp_pa)
        # 直接用配置里唯一的通流曲线重算，确保与分配、记账同一口径。
        recomputed = limits.flow_k_nm3_h * limits.conductance(angle) * pressure_factor
        recorded = float(sample["flow_nm3_h"])
        scale = max(1.0, abs(recorded))
        ok = abs(recomputed - recorded) <= rel_tol * scale
        findings.append(
            {
                "check": "opening_flow",
                "oven": oven,
                "ok": ok,
                "recorded_nm3_h": round(recorded, 3),
                "recomputed_nm3_h": round(recomputed, 3),
            }
        )
    return findings


def _check_mass_balance(oven_totals_nm3: dict[str, float], measured_inlet_nm3: float, rel_tol: float) -> dict:
    allocated = sum(float(value) for value in oven_totals_nm3.values())
    scale = max(1.0, abs(float(measured_inlet_nm3)))
    gap = float(measured_inlet_nm3) - allocated
    ok = abs(gap) <= rel_tol * scale
    return {
        "check": "mass_balance",
        "ok": ok,
        "allocated_nm3": round(allocated, 3),
        "inlet_nm3": round(float(measured_inlet_nm3), 3),
        "gap_nm3": round(gap, 3),
    }


def reconcile_cycle(
    pressure: dict,
    ovens: dict[str, dict],
    limits: FlapControlLimits,
    *,
    any_frozen: bool = False,
    flow_rel_tol: float = 0.02,
) -> dict:
    """对一条控制周期样本做前三类（指令/压力链/开度流量）一致性核对。"""

    findings: list[dict] = []
    findings.extend(_check_command_readback(ovens, limits.readback_tol_deg))
    findings.append(_check_pressure_opening(pressure, any_frozen))
    findings.extend(_check_opening_flow(ovens, limits, flow_rel_tol))
    ok = all(item["ok"] for item in findings)
    return {"ok": ok, "findings": findings}


def reconcile_totals(
    oven_totals_nm3: dict[str, float],
    measured_inlet_nm3: float,
    *,
    rel_tol: float = 0.05,
) -> dict:
    """对账各炉封账总量与全段入口总量是否闭合。"""

    return _check_mass_balance(oven_totals_nm3, measured_inlet_nm3, rel_tol)
