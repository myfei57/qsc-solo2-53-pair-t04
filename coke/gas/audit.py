"""集气管压力、翻板开度与每炉累计量的审计一致性核对。

巡检不信任任何一份单点状态，只信落盘流水。:func:`reconcile_gas` 从审计事件
重放，核对五件事：

1. **控制律可复算**：每条 ``gas_cycle`` 里的控制器原始开度令 ``wanted_deg``
   必须等于用当时压力、紧急炉数与配置重选模式后重算的值（允许舍入容差）；
2. **时序合法**：相邻周期时间戳严格单调；仲裁后 ``command_deg`` 相对上拍
   变化不超过该模式变化率 × dt，方向反转满足反向驻留；
3. **令回一致**：无回读故障的炉，指令开度与回读开度偏差不超过回读容差；
4. **累计量不回跳**：同一炉相邻周期累计量只增不减，单拍计入量不为负；
5. **账单对得上**：每条 ``gas_billed`` 的关账总量等于该账期各周期计入量
   （``delta_m3``）之和。

核对只依赖事件负载与配置，不读任何运行态对象，可以对历史流水反复跑。
"""

from __future__ import annotations

from ..config import CollectorLimits
from .controller import CollectorPressureController, choose_mode
from .eventkeys import GAS_BILLED, GAS_CYCLE

RECOMPUTE_TOL_DEG = 0.05
RATE_TOL_DEG = 0.02
BILL_TOL_M3 = 0.05


def _records(events: list[dict]) -> list[dict]:
    normalized = []
    for item in events:
        payload = item.get("payload", item)
        action = item.get("action", payload.get("action", ""))
        if action in (GAS_CYCLE, GAS_BILLED):
            entry = {
                "ts": float(item.get("ts", payload.get("ts", 0.0))),
                "action": action,
                "payload": payload,
            }
            # 落盘审计记录把 oven 放顶层，归一化时保留，供按炉对账。
            if "oven" in item:
                entry["oven"] = item["oven"]
            normalized.append(entry)
    normalized.sort(key=lambda item: item["ts"])
    return normalized


def reconcile_gas(events: list[dict], limits: CollectorLimits) -> dict:
    """重放气体相关审计事件，返回一致性核对报告。"""

    records = _records(events)
    controller = CollectorPressureController(limits)
    violations: list[dict] = []
    cycles = 0
    prev_ts: float | None = None
    prev_command: float | None = None
    prev_mode: str | None = None
    prev_change_ts: float | None = None
    prev_direction = 0
    prev_total: dict[str, float] = {}
    sums: dict[str, float] = {}
    closed: set[str] = set()
    bills: list[dict] = []

    for record in records:
        payload = record["payload"]
        if record["action"] == GAS_BILLED:
            bills.append(_check_bill(record, sums, violations))
            oven = str(record.get("oven", ""))
            if oven:
                closed.add(oven)
            continue

        cycles += 1
        if prev_ts is not None and record["ts"] < prev_ts:
            violations.append({"kind": "timestamp_non_monotonic", "ts": record["ts"]})
        elif prev_ts is not None and record["ts"] == prev_ts:
            violations.append({"kind": "duplicate_cycle_timestamp", "ts": record["ts"]})
        dt = float(payload.get("dt_s", max(0.0, record["ts"] - (prev_ts if prev_ts is not None else record["ts"]))))
        prev_ts = record["ts"]
        mode = _check_command(record["ts"], payload, controller, limits, violations)
        prev_command, prev_change_ts, prev_direction = _check_sequence(
            record["ts"],
            payload,
            mode,
            dt,
            limits,
            prev_command,
            prev_change_ts,
            prev_direction,
            violations,
        )
        prev_mode = mode
        _check_ovens(record["ts"], payload, limits, prev_total, sums, closed, violations)

    return {
        "ok": not violations,
        "cycles_checked": cycles,
        "bills_checked": len(bills),
        "bills": bills,
        "violations": violations,
    }


def _check_command(
    ts: float,
    payload: dict,
    controller: CollectorPressureController,
    limits: CollectorLimits,
    violations: list,
) -> str:
    pressure = payload.get("pressure_pa")
    wanted = payload.get("wanted_deg")
    urgent = int(payload.get("urgent_ovens", 0))
    if pressure is None or wanted is None:
        violations.append({"kind": "cycle_missing_fields", "ts": ts})
        return str(payload.get("mode", ""))
    mode = choose_mode(limits, float(pressure), urgent_ovens=urgent)
    expected = controller.command(float(pressure), mode)
    if abs(expected - float(wanted)) > RECOMPUTE_TOL_DEG:
        violations.append(
            {
                "kind": "command_recompute_mismatch",
                "ts": ts,
                "expected_deg": round(expected, 3),
                "recorded_deg": round(float(wanted), 3),
                "mode": mode,
            }
        )
    return mode


def _check_sequence(
    ts: float,
    payload: dict,
    mode: str,
    dt: float,
    limits: CollectorLimits,
    prev_command: float | None,
    prev_change_ts: float | None,
    prev_direction: int,
    violations: list,
) -> tuple[float, float, int]:
    command = float(payload.get("command_deg", 0.0))
    wanted = float(payload.get("wanted_deg", command))
    if prev_command is not None and dt > 0.0:
        rate = limits.flap_rate_fast_deg_per_s if mode == "fast" else limits.flap_rate_slow_deg_per_s
        if abs(command - prev_command) > rate * dt + RATE_TOL_DEG:
            violations.append(
                {
                    "kind": "command_rate_exceeded",
                    "ts": ts,
                    "delta_deg": round(command - prev_command, 3),
                    "max_delta_deg": round(rate * dt, 3),
                }
            )
        # 仲裁令不得越过控制器原始令所在的一侧（限速后允许但不允许超调）。
        if wanted > prev_command and command > wanted + RATE_TOL_DEG:
            violations.append({"kind": "command_overshoot", "ts": ts})
        if wanted < prev_command and command < wanted - RATE_TOL_DEG:
            violations.append({"kind": "command_overshoot", "ts": ts})

    delta = command - (prev_command if prev_command is not None else command)
    direction = 1 if delta > RATE_TOL_DEG else -1 if delta < -RATE_TOL_DEG else 0
    dwell = limits.flap_dwell_fast_s if mode == "fast" else limits.flap_dwell_slow_s
    if (
        prev_command is not None
        and direction != 0
        and prev_direction != 0
        and direction != prev_direction
        and prev_change_ts is not None
        and ts - prev_change_ts < max(dwell, limits.flap_reverse_dwell_s) - 1e-6
    ):
        violations.append({"kind": "reverse_dwell_violated", "ts": ts})
    if direction != 0:
        return command, ts, direction
    return command, prev_change_ts if prev_change_ts is not None else ts, prev_direction


def _check_ovens(
    ts: float,
    payload: dict,
    limits: CollectorLimits,
    prev_total: dict[str, float],
    sums: dict[str, float],
    closed: set[str],
    violations: list,
) -> None:
    for oven, item in dict(payload.get("ovens", {})).items():
        active = bool(item.get("active", False))
        # 已关账炉：后续周期里它应处于非在役；重新开账则从 0 起算新账期。
        if oven in closed:
            if not active:
                continue
            closed.discard(oven)
            sums.pop(oven, None)
            prev_total.pop(oven, None)
            prev_total.pop(f"{oven}__angle", None)
        command_deg = float(item.get("command_deg", 0.0))
        readback_deg = float(item.get("readback_deg", 0.0))
        # angle_deg 是通过回读校验后采信的物理开度；冻结时停在最后可信位置。
        angle_deg = float(item.get("angle_deg", readback_deg))
        fault = bool(item.get("readback_fault", False))
        if not fault and abs(angle_deg - command_deg) > limits.flap_readback_tol_deg + 0.01:
            violations.append(
                {
                    "kind": "readback_exceeds_tolerance",
                    "ts": ts,
                    "oven": oven,
                    "command_deg": round(command_deg, 2),
                    "readback_deg": round(readback_deg, 2),
                    "angle_deg": round(angle_deg, 2),
                }
            )
        # 冻结期间 angle_deg 必须保持上一拍可信开度，不允许被故障回读拖走。
        if fault:
            prev_angle = prev_total.get(f"{oven}__angle")
            if prev_angle is not None and abs(angle_deg - float(prev_angle)) > 0.01:
                violations.append(
                    {
                        "kind": "frozen_position_moved",
                        "ts": ts,
                        "oven": oven,
                        "before_deg": round(float(prev_angle), 2),
                        "after_deg": round(angle_deg, 2),
                    }
                )
            prev_total[f"{oven}__angle"] = angle_deg
        total = float(item.get("total_m3", 0.0))
        before = prev_total.get(oven)
        if before is not None and total + 1e-6 < before:
            violations.append(
                {"kind": "oven_total_decreased", "ts": ts, "oven": oven, "before": before, "after": total}
            )
        delta = float(item.get("delta_m3", 0.0))
        if delta < -1e-9:
            violations.append({"kind": "negative_cycle_volume", "ts": ts, "oven": oven})
        prev_total[oven] = total
        if item.get("active", False):
            sums[oven] = sums.get(oven, 0.0) + max(0.0, delta)


def _check_bill(record: dict, sums: dict[str, float], violations: list) -> dict:
    # 落盘审计记录：oven 在顶层，账单字段在 payload 内；兼容直接传账单字典。
    oven = str(record.get("oven", record.get("payload", {}).get("oven", "")))
    payload = record.get("payload", record)
    total_m3 = float(payload.get("total_m3", 0.0))
    accumulated = sums.get(oven, 0.0)
    if abs(accumulated - total_m3) > BILL_TOL_M3:
        violations.append(
            {
                "kind": "bill_total_mismatch",
                "oven": oven,
                "summed_m3": round(accumulated, 3),
                "billed_m3": round(total_m3, 3),
            }
        )
    return {
        "oven": oven,
        "total_m3": round(total_m3, 3),
        "summed_m3": round(accumulated, 3),
        "matched": abs(accumulated - total_m3) <= BILL_TOL_M3,
    }
