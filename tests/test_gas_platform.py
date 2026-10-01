"""平台级集气管自动调节：闭环、按炉账期、部分失败、审计核对、控制台。"""

from __future__ import annotations

import pytest

from coke.app import build_platform
from coke.clock import ManualClock
from coke.config import default_config
from coke.console.api import Api
from coke.gas.audit import reconcile_gas


def _platform(tmp_path, clock):
    return build_platform(default_config(), clock=clock, storage_root=str(tmp_path / "state"))


def test_charge_opens_gas_account_and_cycles_accumulate(tmp_path, clock):
    platform = _platform(tmp_path, clock)
    platform.start()
    report = platform.charge(oven="oven01", hopper_target_mm=0.0, hopper_measured_mm=0.0)
    assert report["ok"] is True
    assert platform.gas_ledger.active_ovens() == ["oven01"]
    for _ in range(10):
        clock.advance(1.0)
        platform.cycle(dt=1.0)
    live = platform.gas_ledger.live()["oven01"]
    assert live["samples"] == 10
    assert live["total_m3"] > 0.0


def test_closed_loop_drives_pressure_toward_target_under_disturbance(tmp_path, clock):
    platform = _platform(tmp_path, clock)
    platform.start()
    platform.charge(oven="oven01", hopper_target_mm=0.0, hopper_measured_mm=0.0)
    platform.disturb_collector(70.0)
    pressures = []
    for _ in range(60):
        clock.advance(1.0)
        pressures.append(platform.cycle(dt=1.0)["pressure_pa"])
    # 顶压后先快调，开度随之上抬；最终压力被拉回目标带。
    assert platform.config.collector.settled(pressures[-1])


def test_mode_switches_with_oven_condition(tmp_path, clock):
    platform = _platform(tmp_path, clock)
    platform.start()
    # 先把压力调到目标带，缓调前提是偏差不大。
    for _ in range(20):
        platform.cycle(dt=1.0)
    platform.open_gas_account("oven01", condition="coking")
    clock.advance(1.0)
    slow = platform.cycle(dt=1.0)
    assert slow["gas_mode"] == "slow"
    platform.set_gas_condition("oven01", "pushing")
    clock.advance(1.0)
    assert platform.cycle(dt=1.0)["gas_mode"] == "fast"


def test_one_frozen_flap_does_not_stop_other_ovens(tmp_path, clock):
    platform = _platform(tmp_path, clock)
    platform.start()
    platform.charge(oven="oven01", hopper_target_mm=0.0, hopper_measured_mm=0.0)
    platform.set_gas_condition("oven01", "coking")
    platform.charge(oven="oven02", hopper_target_mm=0.0, hopper_measured_mm=0.0)
    for _ in range(6):
        clock.advance(1.0)
        platform.cycle(dt=1.0)
    platform.inject_flap_readback("oven02", 3.0)
    clock.advance(1.0)
    platform.cycle(dt=1.0)
    clock.advance(1.0)
    platform.cycle(dt=1.0)
    assert platform.flaps.flap("oven02").readback_fault() is True
    frozen = platform.flaps.flap("oven02").angle_deg
    for _ in range(4):
        clock.advance(1.0)
        platform.cycle(dt=1.0)
    assert platform.flaps.flap("oven02").angle_deg == frozen
    assert platform.flaps.flap("oven01").readback_fault() is False
    assert any(item["action"] == "gas_readback_fault" for item in platform.audit.alarms())
    # 冻结期间账仍按可信开度累计，不按故障回读。
    assert platform.gas_ledger.live()["oven02"]["last_deg"] == frozen


def test_push_closes_account_and_bill_matches_cycle_sum(tmp_path, clock):
    platform = _platform(tmp_path, clock)
    platform.start()
    platform.charge(oven="oven01", hopper_target_mm=0.0, hopper_measured_mm=0.0)
    for _ in range(8):
        clock.advance(1.0)
        platform.cycle(dt=1.0)
    report = platform.push(
        oven="oven01", guide_target_mm=0.0, guide_measured_mm=0.0, stroke_target_mm=3200.0
    )
    assert report["ok"] is True
    bill = report["gas_bill"]
    assert bill["oven"] == "oven01"
    assert bill["total_m3"] > 0.0
    result = platform.reconcile_gas()
    assert result["ok"] is True
    assert result["bills"][0]["matched"] is True


def test_reconcile_detects_tampered_command(tmp_path, clock):
    platform = _platform(tmp_path, clock)
    platform.start()
    platform.charge(oven="oven01", hopper_target_mm=0.0, hopper_measured_mm=0.0)
    for _ in range(6):
        clock.advance(1.0)
        platform.cycle(dt=1.0)
    raw = platform.audit.replay_from_journal()
    for record in raw:
        if record["action"] == "gas_cycle":
            record["payload"]["wanted_deg"] = 99.0
            break
    result = reconcile_gas(raw, platform.config.collector)
    assert result["ok"] is False
    assert any(item["kind"] == "command_recompute_mismatch" for item in result["violations"])


def test_gas_state_restores_across_restart(tmp_path, clock):
    storage = str(tmp_path / "state")
    first = build_platform(default_config(), clock=clock, storage_root=storage)
    first.start()
    first.charge(oven="oven01", hopper_target_mm=0.0, hopper_measured_mm=0.0)
    for _ in range(8):
        clock.advance(1.0)
        first.cycle(dt=1.0)
    total_before = first.gas_ledger.total("oven01")

    second = build_platform(default_config(), clock=clock, storage_root=storage)
    assert second.gas_ledger.active_ovens() == ["oven01"]
    assert second.gas_ledger.total("oven01") == pytest.approx(total_before, abs=1e-3)
    assert second.reconcile_gas(full_journal=True)["ok"] is True


def test_console_exposes_gas_endpoints(tmp_path, clock):
    platform = _platform(tmp_path, clock)
    api = Api(platform)
    platform.start()
    platform.charge(oven="oven01", hopper_target_mm=0.0, hopper_measured_mm=0.0)
    status, gas = api.handle("GET", "/api/gas", {}, {})
    assert status == 200
    assert set(gas) >= {"pressure_pa", "command_deg", "ovens", "live", "bills"}
    status, _ = api.handle("POST", "/api/gas/mode", {}, {"mode": "slow"})
    assert status == 200
    status, recon = api.handle("GET", "/api/gas/reconcile", {}, {})
    assert status == 200 and recon["ok"] is True
    assert any(route["path"] == "/api/gas" for route in api.routes())


def test_status_carries_per_oven_opening_and_totals(tmp_path, clock):
    platform = _platform(tmp_path, clock)
    platform.start()
    platform.charge(oven="oven01", hopper_target_mm=0.0, hopper_measured_mm=0.0)
    clock.advance(1.0)
    platform.cycle(dt=1.0)
    gas = platform.status()["gas"]
    assert set(gas["ovens"]) == {"oven01", "oven02", "oven03"}
    assert "oven01" in gas["live"]
    assert gas["ovens"]["oven01"]["readback_deg"] > 0.0
