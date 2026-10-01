"""集气管自动找开度、按炉累计量与审计一致性。"""

from __future__ import annotations

import pytest

from coke.config import default_config
from coke.errors import InterlockError
from coke.gas.audit import reconcile_gas
from coke.gas.controller import (
    MODE_FAST,
    MODE_SLOW,
    CollectorPressureController,
    choose_mode,
)
from coke.gas.flap import FLAP_OPEN_DEG, BridgeFlap
from coke.gas.latch import FlapLatch
from coke.gas.ledger import GasLedger
from coke.gas.meter import estimate_flow_m3_s
from coke.gas.sequence import OpeningSequencer


# ---- 模式选择：快调抢时间 / 缓调求稳，按炉况定 ----


def test_mode_is_fast_for_charging_or_pushing_oven():
    limits = default_config().collector
    assert choose_mode(limits, limits.pressure_target_pa, urgent_ovens=1) == MODE_FAST
    assert choose_mode(limits, limits.pressure_target_pa, urgent_ovens=2) == MODE_FAST


def test_mode_is_slow_for_coking_within_fast_error_band():
    limits = default_config().collector
    assert choose_mode(limits, limits.pressure_target_pa + 5.0, urgent_ovens=0) == MODE_SLOW


def test_mode_falls_back_to_fast_when_pressure_error_is_large():
    limits = default_config().collector
    assert choose_mode(limits, limits.pressure_target_pa + limits.fast_error_pa + 1.0) == MODE_FAST
    assert choose_mode(limits, limits.pressure_target_pa - limits.fast_error_pa - 1.0) == MODE_FAST


def test_mode_override_pins_choice():
    limits = default_config().collector
    assert choose_mode(limits, 200.0, override=MODE_SLOW) == MODE_SLOW
    assert choose_mode(limits, limits.pressure_target_pa, urgent_ovens=1, override=MODE_FAST) == MODE_FAST


def test_controller_is_symmetric_around_bias_and_clamps():
    limits = default_config().collector
    controller = CollectorPressureController(limits)
    assert controller.command(limits.pressure_target_pa, MODE_SLOW) == limits.flap_bias_deg
    high = controller.command(limits.pressure_target_pa + 10.0, MODE_SLOW)
    low = controller.command(limits.pressure_target_pa - 10.0, MODE_SLOW)
    assert high > limits.flap_bias_deg > low
    assert 0.0 <= controller.command(-1000.0, MODE_FAST) <= 90.0
    assert 0.0 <= controller.command(10000.0, MODE_FAST) <= 90.0


# ---- 开度时序：变化率、死区、驻留、反向延时 ----


def test_sequencer_rate_limits_first_move():
    limits = default_config().collector
    tuning = CollectorPressureController(limits).tuning(MODE_FAST)
    seq = OpeningSequencer(limits.flap_deadband_deg, limits.flap_reverse_dwell_s, initial_deg=0.0)
    first = seq.step(0.0, 85.0, tuning, 1.0)
    assert first == pytest.approx(limits.flap_rate_fast_deg_per_s)


def test_sequencer_deadband_holds_command():
    limits = default_config().collector
    tuning = CollectorPressureController(limits).tuning(MODE_SLOW)
    seq = OpeningSequencer(limits.flap_deadband_deg, limits.flap_reverse_dwell_s, initial_deg=40.0)
    seq.step(0.0, 50.0, tuning, 10.0)
    committed = seq.committed_deg
    assert seq.step(1.0, committed + 0.5, tuning, 1.0) == committed


def test_sequencer_reverse_requires_longer_dwell():
    limits = default_config().collector
    tuning = CollectorPressureController(limits).tuning(MODE_SLOW)
    seq = OpeningSequencer(limits.flap_deadband_deg, limits.flap_reverse_dwell_s, initial_deg=40.0)
    seq.step(0.0, 60.0, tuning, 10.0)
    # 只过了同向驻留、没过反向驻留：维持不动，不来回跳。
    held = seq.step(limits.flap_dwell_slow_s, 20.0, tuning, 1.0)
    assert held == seq.committed_deg
    # 过了反向驻留才允许反向。
    moved = seq.step(limits.flap_reverse_dwell_s + 0.01, 20.0, tuning, 1.0)
    assert moved < seq.committed_deg or moved < 60.0


# ---- 翻板回读跳动：部分失败冻结，不继续按目标开度走 ----


def test_flap_follows_command_when_readback_is_healthy():
    flap = BridgeFlap(FlapLatch())
    assert flap.drive_to(30.0, 1.0) == pytest.approx(30.0)
    assert flap.readback_fault() is False


def test_flap_freezes_on_repeated_readback_jump_and_ignores_further_commands():
    flap = BridgeFlap(FlapLatch(), readback_strikes=2)
    flap.drive_to(40.0, 1.0)
    flap.inject_readback(5.0)
    first = flap.drive_to(40.0, 1.0)
    assert flap.readback_fault() is False  # 首拍跳动先复核
    second = flap.drive_to(40.0, 1.0)
    assert flap.readback_fault() is True
    frozen = flap.angle_deg
    # 冻结后目标开度再怎么变都不追随。
    flap.drive_to(80.0, 1.0)
    flap.drive_to(0.0, 1.0)
    assert flap.angle_deg == frozen
    assert first is not None and second == frozen


def test_flap_recovers_after_fault_reset():
    flap = BridgeFlap(FlapLatch(), readback_strikes=1)
    flap.drive_to(40.0, 1.0)
    flap.inject_readback(0.0)
    flap.drive_to(40.0, 1.0)
    assert flap.readback_fault() is True
    assert flap.reset_readback_fault() is True
    assert flap.drive_to(40.0, 1.0) == pytest.approx(40.0)


def test_latch_still_blocks_double_open():
    flap = BridgeFlap(FlapLatch())
    flap.open()
    assert flap.angle_deg == FLAP_OPEN_DEG
    with pytest.raises(InterlockError):
        flap.open()
    flap.reset_latch()
    assert flap.open() == FLAP_OPEN_DEG


# ---- 按炉累计量 ----


def test_flow_is_proportional_to_readback_and_meter_accumulates():
    limits = default_config().collector
    assert estimate_flow_m3_s(0.0, limits) == 0.0
    assert estimate_flow_m3_s(90.0, limits) == pytest.approx(limits.flow_full_open_m3_s)
    ledger = GasLedger(limits)
    ledger.open_account("oven01", 0.0)
    ledger.accumulate("oven01", 90.0, 10.0)
    ledger.accumulate("oven01", 45.0, 10.0)
    expected = limits.flow_full_open_m3_s * 10.0 + limits.flow_full_open_m3_s * 0.5 * 10.0
    assert ledger.total("oven01") == pytest.approx(expected)
    bill = ledger.close_account("oven01", 20.0)
    assert bill is not None and bill.total_m3 == pytest.approx(expected)
    assert ledger.bills("oven01")[0].oven == "oven01"


def test_open_account_is_idempotent_and_unknown_oven_does_not_accumulate():
    ledger = GasLedger(default_config().collector)
    assert ledger.open_account("oven01", 0.0) is True
    assert ledger.open_account("oven01", 1.0) is False
    assert ledger.accumulate("oven99", 40.0, 1.0) == 0.0
    assert ledger.close_account("oven99", 1.0) is None
