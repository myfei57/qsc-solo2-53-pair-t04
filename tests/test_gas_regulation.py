"""桥管翻板自动找开度、每炉荒煤气累计与回读故障冻结。"""

from __future__ import annotations

import pytest

from coke.config import default_config
from coke.console.api import Api
from coke.errors import ConfigError, CokeError
from coke.gas.condition import decide_tune
from coke.gas.controller import CollectorPressureController, allocate_opening
from coke.gas.timing import FlapTiming


def settle(platform, clock, cycles=40):
    for _ in range(cycles):
        clock.advance(1.0)
        platform.cycle(dt=1.0)


def test_finding_opening_is_main_level_single_loop(platform, clock):
    platform.start()
    settle(platform, clock)
    baseline = platform.gas_regulation.total_conductance

    # 压力顶不住（实测偏高）→ 总通流份额加大；唯一压力闭环在整体层。
    platform.disturb_collector(120.0)
    for _ in range(12):
        clock.advance(1.0)
        result = platform.cycle(dt=1.0)
    assert result["gas"]["total_conductance"] > baseline + 0.1
    # 所有炉都从同一个总份额分开度，单炉不持有压力闭环（无单炉压力入口）。
    assert set(result["gas"]["ovens"]) == {"oven01", "oven02", "oven03", "oven04"}


def test_controller_closes_when_over_diluted():
    limits = default_config().flap_control
    controller = CollectorPressureController(
        90.0,
        18.0,
        gain_per_pa=limits.conductance_gain_per_pa,
        deadband_pa=limits.deadband_pa,
        base_conductance=limits.base_conductance,
        rate_per_s=limits.conductance_rate_per_s,
    )
    for _ in range(300):
        high = controller.update(230.0, 1.0)
    assert high.total_conductance == pytest.approx(1.0, abs=1e-6)
    for _ in range(300):
        low = controller.update(30.0, 1.0)
    assert low.total_conductance == pytest.approx(0.0, abs=1e-6)


def test_fast_or_slow_tune_by_oven_condition():
    assert decide_tune("steady", 2.0, fast_multiplier=2.5, fast_err_pa=8.0).mode == "slow"
    charging = decide_tune("charging", 2.0, fast_multiplier=2.5, fast_err_pa=8.0)
    urgent = decide_tune("steady", 40.0, fast_multiplier=2.5, fast_err_pa=8.0)
    assert charging.mode == "fast" and charging.rate_multiplier == 2.5
    assert urgent.mode == "fast"


def test_timing_dwell_and_deadband_prevent_hunting():
    timing = FlapTiming(dwell_min_s=8.0, deadband_deg=1.5, min_step_deg=1.0)
    first = timing.evaluate(10.0, 30.0, 1.0)
    assert first.may_move and first.direction == 1
    # 驻留未到点，反向请求被保持而不是来回跳。
    reverse = timing.evaluate(first.commanded_deg, 5.0, 1.0)
    assert not reverse.may_move
    # 小变化落在死区内不动。
    assert not timing.evaluate(10.0, 10.5, 1.0).may_move


def test_allocation_weights_and_bounds():
    limits = default_config().flap_control
    targets = allocate_opening(
        1.0, {"oven01": 2.5, "oven02": 1.0, "oven03": 1.0, "oven04": 1.0}, limits
    )
    assert set(targets) == {"oven01", "oven02", "oven03", "oven04"}
    assert targets["oven01"] > targets["oven02"]
    assert all(0.0 <= angle <= 90.0 for angle in targets.values())
    # 零总份额时各炉可关死，不被保底份额强制抽气。
    assert allocate_opening(0.0, {"oven01": 1.0}, limits) == {"oven01": 0.0}


def test_per_oven_gas_is_totalized_and_sealed_per_charge(platform, clock):
    platform.start()
    platform.disturb_collector(120.0)
    settle(platform, clock, 30)
    platform.begin_oven_charge("oven03")
    for _ in range(6):
        clock.advance(1.0)
        result = platform.cycle(dt=1.0)
    assert result["gas"]["ovens"]["oven03"]["total_nm3"] >= 0.0
    sealed = platform.seal_oven_charge("oven03")
    assert sealed["sealed"] and sealed["charge_seq"] == 1
    assert sealed["final_total_nm3"] == sealed["total_nm3"]
    totals = platform.gas_totals(measured_inlet_nm3=sealed["total_nm3"])
    assert totals["balance"]["ok"]


def test_readback_jitter_freezes_chain_and_blocks_target(platform, clock):
    platform.start()
    platform.disturb_collector(120.0)
    platform.set_oven_phase("oven01", "charging")
    settle(platform, clock, 40)
    base = platform.gas_status()["ovens"]["oven01"]["readback_deg"]

    result = None
    for value in (base + 1.0, base - 1.0, base + 1.0, base - 1.0, base + 1.0, base - 1.0):
        platform.report_flap_readback("oven01", value)
        clock.advance(1.0)
        result = platform.cycle(dt=1.0)
        if result["gas"]["frozen"]:
            break

    assert result["gas"]["frozen"]
    oven = result["gas"]["ovens"]["oven01"]
    assert oven["fault_reason"] == "readback_jitter"
    assert oven["readback_deg"] == default_config().flap_control.fail_hold_deg
    # 冻结期继续推进也不会再按目标开度走。
    clock.advance(1.0)
    held = platform.cycle(dt=1.0)
    assert held["gas"]["frozen"]
    assert held["gas"]["ovens"]["oven01"]["readback_deg"] == oven["readback_deg"]
    assert any(item["action"] == "flap_chain_frozen" for item in platform.audit.alarms())

    # 显式复位后整链恢复（允许一个回位瞬态周期）。
    platform.clear_flap_fault("oven01")
    recovered = False
    for _ in range(6):
        clock.advance(1.0)
        after = platform.cycle(dt=1.0)
        if not platform.gas_status()["frozen"] and after["gas"]["consistency_ok"]:
            recovered = True
            break
    assert recovered


def test_readback_deviation_also_freezes(platform, clock):
    platform.start()
    platform.disturb_collector(200.0)
    settle(platform, clock, 60)
    base = platform.gas_status()["ovens"]["oven02"]["readback_deg"]
    platform.report_flap_readback("oven02", base + 50.0)
    clock.advance(1.0)
    result = platform.cycle(dt=1.0)
    assert result["gas"]["frozen"]
    assert result["gas"]["ovens"]["oven02"]["fault_reason"] == "readback_deviation"


def test_audit_and_console_expose_opening_and_totals(platform, clock):
    platform.start()
    settle(platform, clock, 5)
    actions = [event.action for event in platform.bus.history("gas")]
    assert "flap_regulated" in actions
    assert "pressure_allocated" in actions
    assert "gas_regulation" in platform.status()
    snapshot = platform.snapshot()["trend"]
    assert any(key.startswith("flap_deg.") for key in snapshot)
    assert any(key.startswith("gas_total_nm3.") for key in snapshot)

    api = Api(platform)
    status, body = api.handle("GET", "/api/gas", {}, {})
    assert status == 200 and "ovens" in body
    status, body = api.handle("GET", "/api/gas/oven/oven01", {}, {})
    assert status == 200 and body["oven"] == "oven01"
    assert api.handle("GET", "/api/gas/oven/nope", {}, {})[0] == 409
    status, body = api.handle("POST", "/api/gas/phase", {}, {"oven": "x", "phase": "steady"})
    assert status == 409


def test_healthy_operation_has_no_consistency_violations(platform, clock):
    platform.start()
    settle(platform, clock, 40)
    platform.disturb_collector(120.0)
    settle(platform, clock, 20)
    platform.disturb_collector(-120.0)
    violations = 0
    for _ in range(60):
        clock.advance(1.0)
        result = platform.cycle(dt=1.0)
        if not result["gas"]["consistency_ok"] and not result["gas"]["frozen"]:
            violations += 1
    assert violations == 0


def test_config_requires_flap_control_section():
    from coke.config import PlatformConfig, config_to_mapping

    mapping = config_to_mapping(default_config())
    del mapping["limits"]["flap_control"]
    with pytest.raises(ConfigError):
        PlatformConfig.from_mapping(mapping)


def test_unknown_oven_and_bad_phase_rejected(platform):
    with pytest.raises(CokeError):
        platform.set_oven_phase("nope", "steady")
    with pytest.raises(CokeError):
        platform.set_oven_phase("oven01", "bogus")
