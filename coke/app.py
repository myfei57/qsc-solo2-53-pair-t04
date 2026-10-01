"""平台装配与控制循环。

``Platform`` 把各组件按真实调用链接起来，并暴露四件事：操作编排（装煤、
推焦、熄焦、换向、炉门清扫）、控制循环、并发仲裁探测和状态快照。控制循环
每个周期做四件事：按集气管压力自动找桥管翻板开度（整体调节、快／缓调按炉
况切、开度有时序）、按炉累计荒煤气量、按推焦联动时序驱动除尘挡板、采集趋
势；换向与推焦这类跨模块动作都由编排一次性走完并写审计。
"""

from __future__ import annotations

import dataclasses
import threading

from .audit.log import AuditLog
from .audit.record import AuditRecord
from .charger.machine import ChargingCar
from .clock import Clock, SystemClock
from .config import PlatformConfig, default_config
from .door.latch import DoorLatch
from .door.machine import DoorCleaner
from .dust.damper import DustDamper
from .dust.schedule import DamperSchedule
from .dust.snapshot import DustSnapshot
from .dust.system import DustSystem
from .errors import CokeError
from .eventbus import Event, EventBus
from .gas.bank import FlapBank
from .gas.collector import CollectorMain
from .gas.controller import MODE_FAST, CollectorPressureController, choose_mode
from .gas.eventkeys import (
    GAS_ACCOUNT_CLOSED,
    GAS_ACCOUNT_OPENED,
    GAS_BILLED,
    GAS_CYCLE,
    GAS_READBACK_FAULT,
)
from .gas.ledger import GasLedger
from .gas.audit import reconcile_gas
from .gas.sequence import OpeningSequencer
from .guide.machine import GuideCar
from .heating.machine import HeatingReversal
from .heating.snapshot import HeatingSnapshot
from .heating.temperature import OvenTemperature
from .heating.valvetrain import ValveTrain
from .ids import IdFactory
from .ns.registry import NamespaceRegistry
from .ns.topology import Topology, build_default_topology
from .pusher.machine import PusherMachine
from .pusher.speed import SpeedArbiter
from .pusher.stroke import StrokeBaseline
from .quench.machine import QuenchCar
from .sequence import charge_oven, clean_door, push_oven, quench_oven, reverse_heating
from .store.journal import JsonlJournal
from .store.jsonfile import JsonStore
from .telemetry import TrendBuffer

DEFAULT_UNIT = "battery1"
DEFAULT_OVEN = "oven01"
DEFAULT_OVENS = ("oven01", "oven02", "oven03")


class Platform:
    """焦炉推装熄顺序控制平台。"""

    def __init__(
        self,
        config: PlatformConfig | None = None,
        clock: Clock | None = None,
        unit: str = DEFAULT_UNIT,
    ) -> None:
        self.config = config or default_config()
        self.clock = clock or SystemClock()
        self.bus = EventBus()
        self.unit = unit

        store = JsonStore(self.config.storage_root)
        self.store = store
        self.audit = AuditLog(self.clock, self.bus, JsonlJournal(store.root, "audit", segment_limit=512))
        self.namespaces = NamespaceRegistry(unit)
        self.topology: Topology = build_default_topology()
        self.trend = TrendBuffer(period_s=5.0)
        self.ids = IdFactory("op")

        self.pusher = PusherMachine(self.config.pusher)
        self.stroke = StrokeBaseline(
            self.config.pusher.stroke_baseline_mm, self.config.pusher.stroke_tolerance_mm
        )
        self.speed = SpeedArbiter(
            self.config.pusher.speed_min_mm_s,
            self.config.pusher.speed_max_mm_s,
            self.config.pusher.speed_default_mm_s,
        )
        self.guide = GuideCar(self.config.guide)
        self.charger = ChargingCar(self.config.charger)
        self.quench_car = QuenchCar(self.config.quench)
        self.collector = CollectorMain(self.config.collector)
        self.flaps = FlapBank(DEFAULT_OVENS, self.config.collector)
        # 兼容单翻板旧引用：装煤序列与闩锁检查仍走 oven01 的翻板。
        self.flap = self.flaps.flap(DEFAULT_OVEN)
        self.flap_controller = CollectorPressureController(self.config.collector)
        self.flap_sequencer = OpeningSequencer(
            self.config.collector.flap_deadband_deg,
            self.config.collector.flap_reverse_dwell_s,
        )
        self.gas_ledger = GasLedger(self.config.collector)
        self._gas_mode_override: str | None = None
        self.door = DoorCleaner(self.config.door, DoorLatch(self.config.door.latch_travel_mm))
        self.reversal = HeatingReversal(self.config.heating, self.clock)
        self.valves = ValveTrain()
        self.temperature = OvenTemperature(self.config.heating)
        self.dust = DustSystem()
        self.damper = DustDamper(self.config.dust.damper_open_deg, self.config.dust.damper_rate_deg_per_s)
        self.schedule = DamperSchedule(self.config.dust.linkage_lag_s)

        self._cycles = 0
        self._operations = 0
        self._gas_faults: dict[str, bool] = {}
        self._last_mean_open_deg: float | None = None
        self._lock = threading.RLock()
        self._register_namespaces()
        self._restore()

    def _register_namespaces(self) -> None:
        """登记现场设备命名空间（控制台与审计共用同一套名字）。"""

        for area, device, kind in (
            ("battery", "pusher", "pusher-machine"),
            ("battery", "guide", "guide-car"),
            ("battery", "charger", "charging-car"),
            ("battery", "quench", "quench-car"),
            ("battery", "collector", "collector-main"),
            ("battery", "door", "door-cleaner"),
            ("battery", "reversal", "reversing-valve"),
            ("battery", "dust", "dust-damper"),
        ):
            self.namespaces.register(area, device, kind)
        # 桥管翻板按炉登记，审计与控制台按炉寻址。
        for oven in self.flaps.ovens():
            self.namespaces.register("gas", oven, "bridge-flap")

    def _restore(self) -> None:
        """从落盘审计回放最后一次换向，恢复换向间隔计时。"""

        try:
            records = self.audit.to_records(self.audit.replay_from_journal(limit=100_000))
        except CokeError:
            return
        reversals = [item for item in records if item.action == "reverse_complete"]
        # 只接受不晚于当前时钟的回放记录；可推进时钟重置后，上一轮留下的未来
        # 时间戳属于过期状态，不能拿它把换向间隔计时顶到当前时刻之前。
        if reversals and reversals[-1].ts <= self.clock.now():
            self.reversal.mark_reversed(reversals[-1].ts)
        self._restore_gas(records)
        state = self.store.read("platform_state")
        if isinstance(state, dict):
            self._operations = int(state.get("operations", 0))
            self._cycles = int(state.get("cycles", 0))

    def _restore_gas(self, records: list) -> None:
        """从气体流水重建在役账期、已关账单与开度时序状态。"""

        gas_events = [
            {"ts": item.ts, "action": item.action, "oven": item.oven, "payload": dict(item.payload)}
            for item in records
            if item.component == "gas"
        ]
        self.gas_ledger.restore(gas_events)
        cycles = [item for item in gas_events if item["action"] == GAS_CYCLE]
        if cycles:
            last = cycles[-1]
            payload = last["payload"]
            self.flap_sequencer.restore(
                float(payload.get("command_deg", 0.0)),
                last["ts"],
            )
            self.flaps.restore_positions(
                {
                    oven: float(item.get("angle_deg", 0.0))
                    for oven, item in dict(payload.get("ovens", {})).items()
                }
            )
            # 回读故障是现场硬件态，重启不自动继承；账期内的故障标记随账单走。
            self._gas_faults = {}

    def _persist(self) -> None:
        """落盘平台计数，供重启后对照。"""

        self.store.write(
            "platform_state",
            {"operations": self._operations, "cycles": self._cycles, "unit": self.unit},
        )

    def _record_operation(self, op_id: str, operation: str, report: dict) -> dict:
        """把编排步骤逐条写审计，并记一条汇总事件。"""

        oven = str(report.get("oven", ""))
        for step in report.get("steps", []):
            detail = {
                key: value
                for key, value in step.get("detail", {}).items()
                if not isinstance(value, (dict, list))
                and key not in ("oven", "op_id", "severity", "message")
            }
            self.bus.publish(
                Event.of(
                    step["component"],
                    step["component"],
                    step["action"],
                    oven=oven,
                    op_id=op_id,
                    severity="notice",
                    **detail,
                )
            )
        for violation in report.get("violations", []):
            self.bus.publish(
                Event.of(
                    "audit",
                    "audit",
                    "interlock_rejected",
                    oven=oven,
                    op_id=op_id,
                    severity="alarm",
                    message=f"{operation} 被拒绝：{violation}",
                    violation=violation,
                )
            )
        summary = {
            "op_id": op_id,
            "operation": operation,
            "oven": oven,
            "ok": bool(report.get("ok")),
            "violations": list(report.get("violations", [])),
        }
        self.bus.publish(
            Event.of(
                "audit",
                "audit",
                f"{operation}_complete",
                severity="notice" if report.get("ok") else "warning",
                message=f"{operation} 完成" if report.get("ok") else f"{operation} 未完成",
                **summary,
            )
        )
        return summary

    def start(self) -> dict:
        """启动平台：启动除尘风机并核对拓扑。"""

        with self._lock:
            self.dust.start()
            report = self.topology.verify(set())
            return {
                "fan_running": self.dust.is_running(),
                "topology_missing": report["missing"],
                "operations": self._operations,
            }

    def stop(self) -> dict:
        """停运平台：停除尘风机。"""

        with self._lock:
            self.dust.stop()
            return {"fan_running": self.dust.is_running(), "operations": self._operations}

    def charge(
        self,
        oven: str = DEFAULT_OVEN,
        hopper_target_mm: float = 0.0,
        hopper_measured_mm: float = 0.0,
        dt: float = 1.0,
    ) -> dict:
        """装煤编排：煤斗对位 → 调压 → 开翻板 → 给煤。"""

        with self._lock:
            op_id = self.ids.next()
            flap = self.flaps.flap(oven)
            report = charge_oven(
                self.charger, self.collector, flap, oven, hopper_target_mm, hopper_measured_mm, dt
            )
            # 装煤成功即给该炉开一炉荒煤气账期，自动找开度环从下一周期接管。
            if report.get("ok"):
                moment = self.clock.now()
                opened = self.gas_ledger.open_account(oven, moment, condition="charging")
                if opened:
                    self.bus.publish(
                        Event.of(
                            "gas", "gas", GAS_ACCOUNT_OPENED, oven=oven, severity="notice"
                        )
                    )
                report["gas_account"] = "opened" if opened else "already_open"
            summary = self._record_operation(op_id, "charge", report)
            summary["op_id"] = op_id
            self._operations += 1
            self._persist()
            return {**report, **summary}

    def push(
        self,
        oven: str = DEFAULT_OVEN,
        guide_target_mm: float = 0.0,
        guide_measured_mm: float = 0.0,
        stroke_target_mm: float | None = None,
        speed_mm_s: float | None = None,
        dt: float = 1.0,
    ) -> dict:
        """推焦编排：导焦栅对位 → 锁定 → 推焦 → 退杆；同时触发除尘联动。"""

        with self._lock:
            op_id = self.ids.next()
            target = self.stroke.baseline_mm if stroke_target_mm is None else stroke_target_mm
            self.speed.request("auto_cycle", speed_mm_s or self.config.pusher.speed_default_mm_s, priority=50)
            arbitration = self.speed.resolve()
            self.schedule.start_link(self.clock.now())
            report = push_oven(
                self.pusher,
                self.guide,
                oven,
                guide_target_mm,
                guide_measured_mm,
                target,
                arbitration.speed_mm_s,
                dt,
            )
            stroke_check = self.stroke.check(self.pusher.ram_mm)
            report["speed_arbitration"] = arbitration.as_dict()
            report["stroke_check"] = stroke_check
            # 推焦成功即关账定格本炉荒煤气总量，供按炉对账。
            if report.get("ok"):
                moment = self.clock.now()
                bill = self.gas_ledger.close_account(oven, moment)
                if bill is not None:
                    bill_dict = bill.as_dict()
                    report["gas_bill"] = bill_dict
                    self.bus.publish(
                        Event.of(
                            "gas",
                            "gas",
                            GAS_BILLED,
                            severity="notice",
                            **bill_dict,
                        )
                    )
                    self.bus.publish(
                        Event.of(
                            "gas", "gas", GAS_ACCOUNT_CLOSED, oven=oven, severity="notice"
                        )
                    )
            summary = self._record_operation(op_id, "push", report)
            summary["op_id"] = op_id
            self._operations += 1
            self._persist()
            return {**report, **summary}

    def quench(self, oven: str = DEFAULT_OVEN, dt: float = 1.0) -> dict:
        """熄焦编排：接焦 → 走行 → 喷淋。"""

        with self._lock:
            op_id = self.ids.next()
            report = quench_oven(self.quench_car, oven, dt)
            report["travel_eta_s"] = self.quench_car.plan.eta_s()
            summary = self._record_operation(op_id, "quench", report)
            summary["op_id"] = op_id
            self._operations += 1
            self._persist()
            return {**report, **summary}

    def reverse(self, oven: str = DEFAULT_OVEN, temperature_c: float | None = None) -> dict:
        """换向编排：废气阀 → 煤气阀 → 炉温确认。"""

        with self._lock:
            op_id = self.ids.next()
            target_temp = self.config.heating.temp_target_c if temperature_c is None else temperature_c
            report = reverse_heating(self.reversal, self.valves, self.temperature, oven, target_temp)
            report["offset_c"] = self.temperature.offset_c()
            summary = self._record_operation(op_id, "reverse", report)
            summary["op_id"] = op_id
            self._operations += 1
            self._persist()
            return {**report, **summary}

    def clean(self, oven: str = DEFAULT_OVEN, dt: float = 1.0) -> dict:
        """炉门清扫编排：松开闩锁 → 开炉门 → 清扫 → 复位闩锁。"""

        with self._lock:
            op_id = self.ids.next()
            report = clean_door(self.door, oven, dt)
            summary = self._record_operation(op_id, "clean", report)
            summary["op_id"] = op_id
            self._operations += 1
            self._persist()
            return {**report, **summary}

    def _gas_control_cycle(self, moment: float, dt: float) -> dict:
        """自动找开度闭环的一个周期：模式 → 总管令 → 时序仲裁 → 下发 → 计量。"""

        limits = self.config.collector
        active = self.gas_ledger.active_ovens()
        pressure = self.collector.pressure_pa
        urgent = len(self.gas_ledger.urgent_ovens())
        mode = choose_mode(limits, pressure, urgent_ovens=urgent, override=self._gas_mode_override)
        tuning = self.flap_controller.tuning(mode)
        wanted = self.flap_controller.command(pressure, mode)
        command = self.flap_sequencer.step(moment, wanted, tuning, dt)

        drive_report = self.flaps.drive(active, command, dt)
        prev_faults = dict(self._gas_faults)
        self._gas_faults = self.flaps.active_faults()

        ovens_payload: dict[str, dict] = {}
        active_readbacks: list[float] = []
        for oven, item in drive_report.items():
            is_active = oven in active
            # 计量与抽吸一律采用通过回读校验的可信开度；回读跳动冻结时计入的
            # 是冻结在现场的最后可信开度，故障回读不进账。
            trusted_deg = float(item["angle_deg"])
            if is_active:
                active_readbacks.append(trusted_deg)
                delta = self.gas_ledger.accumulate(oven, trusted_deg, dt)
            else:
                delta = 0.0
            self.gas_ledger.mark_fault(oven, bool(item["readback_fault"]))
            ovens_payload[oven] = {
                **item,
                "active": is_active,
                "total_m3": round(self.gas_ledger.total(oven), 4),
                "delta_m3": round(delta, 5),
                "condition": self.gas_ledger.condition(oven),
            }
            # 回读跳动从无到有：部分失败告警；故障翻板已在 drive 内冻结。
            if item["readback_fault"] and not prev_faults.get(oven, False):
                self.bus.publish(
                    Event.of(
                        "gas",
                        "gas",
                        GAS_READBACK_FAULT,
                        oven=oven,
                        severity="alarm",
                        message=f"{oven} 桥管翻板回读跳动，已冻结最后可信开度",
                        command_deg=item["command_deg"],
                        readback_deg=item["readback_deg"],
                        frozen_deg=item["angle_deg"],
                    )
                )

        mean_open = sum(active_readbacks) / len(active_readbacks) if active_readbacks else None
        return {
            "mode": mode,
            "tuning": tuning.as_dict(),
            "wanted_deg": round(wanted, 2),
            "command_deg": round(command, 2),
            "mean_open_deg": round(mean_open, 2) if mean_open is not None else None,
            "active_ovens": active,
            "ovens": ovens_payload,
        }

    def cycle(self, dt: float = 1.0) -> dict:
        """推进一个控制周期，返回本周期摘要。"""

        if dt <= 0:
            raise ValueError("步长必须为正")
        with self._lock:
            self._cycles += 1
            moment = self.clock.now()
            before = self.collector.pressure_pa
            # 用上一拍在役翻板平均开度算抽吸，本拍再据压力重算开度令。
            pressure = self.collector.regulate(dt, mean_open_deg=self._last_mean_open_deg)

            gas = self._gas_control_cycle(moment, dt)
            self._last_mean_open_deg = gas["mean_open_deg"]

            linkage = self.schedule.due(moment)
            target_deg = self.damper.open_deg if self.schedule.links() else 0.0
            angle = self.damper.drive_to(target_deg, dt)
            if self.damper.at_open():
                self.schedule.clear()
            self.trend.append("collector_pressure", moment, pressure)
            self.trend.append("damper_angle", moment, angle)
            self.trend.append("flap_command_deg", moment, gas["command_deg"])

            # 每个周期一条气体流水：压力、模式、总管令、各炉令／回读／累计量。
            # 审计核对只依赖这条负载即可逐步重算，不读任何运行态。
            self.bus.publish(
                Event.of(
                    "gas",
                    "gas",
                    GAS_CYCLE,
                    severity="notice",
                    dt_s=dt,
                    pressure_pa=round(pressure, 3),
                    target_pa=self.config.collector.pressure_target_pa,
                    mode=gas["mode"],
                    urgent_ovens=len(self.gas_ledger.urgent_ovens()),
                    wanted_deg=gas["wanted_deg"],
                    command_deg=gas["command_deg"],
                    mean_open_deg=gas["mean_open_deg"] if gas["mean_open_deg"] is not None else 0.0,
                    active_ovens=list(gas["active_ovens"]),
                    ovens=gas["ovens"],
                )
            )
            self._persist()
            return {
                "cycle": self._cycles,
                "pressure_pa": round(pressure, 2),
                "pressure_delta": round(pressure - before, 3),
                "settled": self.collector.settled(),
                "gas_mode": gas["mode"],
                "flap_command_deg": gas["command_deg"],
                "flap_mean_open_deg": gas["mean_open_deg"],
                "gas_active_ovens": gas["active_ovens"],
                "gas_live": self.gas_ledger.live(),
                "damper_deg": round(angle, 2),
                "linkage_due": linkage,
                "linkage_elapsed_s": round(self.schedule.elapsed_s(moment), 2),
            }

    def concurrency_probe(self, rounds: int = 24) -> dict:
        """用真实并行调用探测推焦速度仲裁的收敛结果。

        两个线程分别代表自动循环与手动控制台，同时向推焦杆提交速度。探测只
        读取最终提交值，用来确认共享执行器始终收敛到唯一值。
        """

        if rounds < 1:
            raise ValueError("探测轮数必须为正")

        def worker(writer: str, speed: float, priority: int) -> None:
            for _ in range(rounds):
                self.speed.request(writer, speed, priority)

        threads = [
            threading.Thread(target=worker, args=("auto_cycle", self.config.pusher.speed_max_mm_s, 50)),
            threading.Thread(target=worker, args=("manual_console", self.config.pusher.speed_min_mm_s, 80)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        outcome = self.speed.resolve()
        self.pusher.set_speed(outcome.speed_mm_s)
        return {
            "rounds": rounds,
            "winner": outcome.winner,
            "committed_mm_s": round(outcome.speed_mm_s, 2),
            "contenders": outcome.contenders,
            "writers": self.speed.writers(),
            "committed_on_machine": round(self.pusher.committed_speed(), 2),
        }

    def calibrate_stroke(self, value_mm: float) -> dict:
        """重新标定推焦行程基线，返回标定前后的差值。"""

        if value_mm <= 0:
            raise CokeError("标定值必须为正", component="pusher")
        before = self.stroke.baseline_mm
        after = self.stroke.calibrate(value_mm)
        self.bus.publish(
            Event.of(
                "pusher",
                "pusher",
                "stroke_recalibrated",
                severity="notice",
                baseline_before_mm=before,
                baseline_after_mm=after,
                epoch=self.stroke.epoch,
            )
        )
        return {"before_mm": before, "after_mm": after, "epoch": self.stroke.epoch}

    def disturb_collector(self, delta_pa: float) -> dict:
        """模拟一次集气管扰动，返回扰动后压力。"""

        before = self.collector.pressure_pa
        after = self.collector.disturb(delta_pa)
        return {"before_pa": round(before, 2), "after_pa": round(after, 2), "settled": self.collector.settled()}

    def set_gas_condition(self, oven: str, condition: str) -> dict:
        """设置在役炉炉况（charging/pushing/coking/idle），决定快调／缓调。"""

        with self._lock:
            if oven not in self.flaps.ovens():
                raise CokeError(f"未知炉号: {oven}", component="gas")
            if condition not in ("charging", "pushing", "coking", "idle"):
                raise CokeError(f"未知炉况: {condition}", component="gas")
            updated = self.gas_ledger.set_condition(oven, condition)
            if not updated:
                raise CokeError(f"{oven} 未开荒煤气账期，先装煤开账", component="gas")
            return {"oven": oven, "condition": condition}

    def set_gas_mode(self, mode: str | None) -> dict:
        """显式锁死快调／缓调（``None`` 交回炉况自动选择）。"""

        if mode is not None and mode not in (MODE_FAST, "slow"):
            raise CokeError("调节模式只接受 fast / slow / 自动", component="gas")
        with self._lock:
            self._gas_mode_override = mode
            return {"mode_override": mode, "automatic": mode is None}

    def open_gas_account(self, oven: str, condition: str = "charging") -> dict:
        """给一炉开荒煤气账期（演练／补账用）。"""

        with self._lock:
            if oven not in self.flaps.ovens():
                raise CokeError(f"未知炉号: {oven}", component="gas")
            moment = self.clock.now()
            opened = self.gas_ledger.open_account(oven, moment, condition=condition)
            if opened:
                self.bus.publish(
                    Event.of("gas", "gas", GAS_ACCOUNT_OPENED, oven=oven, severity="notice")
                )
            return {"oven": oven, "opened": opened, "condition": condition}

    def close_gas_account(self, oven: str) -> dict:
        """给一炉关账并返回账单（演练／补账用）。"""

        with self._lock:
            if oven not in self.flaps.ovens():
                raise CokeError(f"未知炉号: {oven}", component="gas")
            moment = self.clock.now()
            bill = self.gas_ledger.close_account(oven, moment)
            if bill is None:
                raise CokeError(f"{oven} 没有在役荒煤气账期", component="gas")
            bill_dict = bill.as_dict()
            self.bus.publish(
                Event.of(
                    "gas", "gas", GAS_BILLED, severity="notice", **bill_dict
                )
            )
            self.bus.publish(
                Event.of("gas", "gas", GAS_ACCOUNT_CLOSED, oven=oven, severity="notice")
            )
            return bill_dict

    def gas_bills(self, oven: str | None = None) -> list[dict]:
        """返回已关账单，可按炉过滤。"""

        return [bill.as_dict() for bill in self.gas_ledger.bills(oven)]

    def gas_status(self) -> dict:
        """返回集气管自动找开度与按炉累计量的完整状态。"""

        with self._lock:
            return {
                "pressure_pa": round(self.collector.pressure_pa, 2),
                "target_pa": self.config.collector.pressure_target_pa,
                "settled": self.collector.settled(),
                "mode_override": self._gas_mode_override,
                "command_deg": round(self.flap_sequencer.committed_deg, 2),
                "active_ovens": self.gas_ledger.active_ovens(),
                "urgent_ovens": self.gas_ledger.urgent_ovens(),
                "readback_faults": dict(self._gas_faults),
                "ovens": {
                    oven: {
                        "command_deg": round(flap.command_deg, 2),
                        "readback_deg": round(flap.readback_deg(), 2),
                        "angle_deg": round(flap.angle_deg, 2),
                        "readback_strikes": flap.readback_bad_strikes(),
                        "readback_fault": flap.readback_fault(),
                        "latch_engaged": flap.latch.is_engaged(),
                    }
                    for oven, flap in ((name, self.flaps.flap(name)) for name in self.flaps.ovens())
                },
                "live": self.gas_ledger.live(),
                "bills": self.gas_bills(),
            }

    def inject_flap_readback(self, oven: str, angle_deg: float) -> dict:
        """注入某炉翻板回读值（回读跳动故障演练用）。"""

        with self._lock:
            if oven not in self.flaps.ovens():
                raise CokeError(f"未知炉号: {oven}", component="gas")
            self.flaps.inject_readback(oven, angle_deg)
            return {"oven": oven, "readback_deg": self.flaps.flap(oven).readback_deg()}

    def reset_flap_fault(self, oven: str) -> dict:
        """现场确认后复位某炉翻板回读故障，恢复追随总管开度令。"""

        with self._lock:
            if oven not in self.flaps.ovens():
                raise CokeError(f"未知炉号: {oven}", component="gas")
            recovered = self.flaps.reset_fault(oven)
            self._gas_faults = self.flaps.active_faults()
            self.gas_ledger.mark_fault(oven, False)
            if recovered:
                self.bus.publish(
                    Event.of(
                        "gas", "gas", "gas_readback_recovered", oven=oven, severity="notice",
                    )
                )
            return {"oven": oven, "recovered": recovered, "readback_fault": False}

    def reconcile_gas(self, *, full_journal: bool = False, limit: int = 20_000) -> dict:
        """重放气体审计流水，核对压力、开度与每炉累计量的一致性。"""

        with self._lock:
            if full_journal:
                raw = self.audit.replay_from_journal(limit=limit)
            else:
                raw = self.audit.tail(limit)
            return reconcile_gas(raw, self.config.collector)

    def note_error(self, error: CokeError) -> dict:
        """把平台异常写入审计，返回落盘记录。"""

        payload = error.as_record()
        record = self.audit.record(
            AuditRecord.create(
                self.clock.now(),
                "",
                payload["component"],
                "error",
                severity="warning",
                message=payload["message"],
            )
        )
        return record

    def reset_state(self) -> bool:
        """清掉落盘的平台计数（交付前重新标定用）。"""

        if not self.store.exists("platform_state"):
            return False
        return self.store.remove("platform_state")

    def heating_snapshot(self) -> HeatingSnapshot:
        """返回加热换向状态快照。"""

        return HeatingSnapshot(
            waste_gas_open=self.valves.waste_open,
            gas_open=self.valves.gas_open,
            temperature_c=self.temperature.value_c,
            confirmed=self.temperature.confirmed(),
            reversals=self.reversal.count(),
            seconds_to_due=self.reversal.seconds_to_due(),
        )

    def dust_snapshot(self) -> DustSnapshot:
        """返回除尘状态快照。"""

        return DustSnapshot(
            fan_running=self.dust.is_running(),
            damper_deg=self.damper.angle_deg,
            damper_open=self.damper.at_open(),
            linkage_pending=self.schedule.links() > 0,
            links=self.schedule.links(),
        )

    def topology_summary(self) -> dict:
        """返回工艺链拓扑摘要。"""

        return self.topology.summary()

    def namespaces_describe(self) -> dict:
        """返回命名空间摘要。"""

        return self.namespaces.describe()

    def status(self) -> dict:
        """汇总状态快照，供控制台与命令行读取。"""

        return {
            "platform": self.config.name,
            "unit": self.unit,
            "operations": self._operations,
            "cycles": self._cycles,
            "now": round(self.clock.now(), 3),
            "utc": self.clock.utc_iso(),
            "pusher": self.pusher.snapshot().as_dict(),
            "speed": {"committed_mm_s": round(self.speed.committed(), 2)},
            "stroke": {
                "baseline_mm": self.stroke.baseline_mm,
                "tolerance_mm": self.stroke.tolerance_mm,
                "epoch": self.stroke.epoch,
            },
            "guide": self.guide.snapshot().as_dict(),
            "charger": self.charger.snapshot().as_dict(),
            "quench": self.quench_car.snapshot().as_dict(),
            "gas": {
                "pressure_pa": round(self.collector.pressure_pa, 2),
                "target_pa": self.collector.limits.pressure_target_pa,
                "settled": self.collector.settled(),
                # 下列 flap_* 为 oven01 旧口径，保持向后兼容。
                "flap_deg": round(self.flap.angle_deg, 2),
                "flap_open": self.flap.is_open(),
                "latch_engaged": self.flap.latch.is_engaged(),
                "latch_cycles": self.flap.latch.cycles(),
                "latch_resets": self.flap.latch.resets(),
                # 自动找开度与按炉累计量（控制台展示用）。
                "mode_override": self._gas_mode_override,
                "command_deg": round(self.flap_sequencer.committed_deg, 2),
                "active_ovens": self.gas_ledger.active_ovens(),
                "urgent_ovens": self.gas_ledger.urgent_ovens(),
                "readback_faults": dict(self._gas_faults),
                "ovens": {
                    oven: {
                        "command_deg": round(self.flaps.flap(oven).command_deg, 2),
                        "readback_deg": round(self.flaps.flap(oven).readback_deg(), 2),
                        "angle_deg": round(self.flaps.flap(oven).angle_deg, 2),
                        "readback_fault": self.flaps.flap(oven).readback_fault(),
                    }
                    for oven in self.flaps.ovens()
                },
                "live": self.gas_ledger.live(),
                "bills": [bill.as_dict() for bill in self.gas_ledger.bills()],
            },
            "door": {**self.door.snapshot().as_dict(), "latch_cycles": self.door.latch.cycles()},
            "heating": self.heating_snapshot().as_dict(),
            "dust": self.dust_snapshot().as_dict(),
            "dust_starts": self.dust.starts(),
            "audit": self.audit.summary(),
            "ids_issued": self.ids.issued(),
        }

    def snapshot(self) -> dict:
        """返回只读的整体快照（状态 + 拓扑 + 命名空间 + 趋势）。"""

        return {
            "status": self.status(),
            "topology": self.topology_summary(),
            "namespaces": self.namespaces_describe(),
            "trend": self.trend.snapshot(),
        }


def build_platform(
    config: PlatformConfig | None = None,
    clock: Clock | None = None,
    storage_root: str | None = None,
    unit: str = DEFAULT_UNIT,
) -> Platform:
    """构造一个平台；``storage_root`` 可覆盖配置里的存储目录。"""

    base = config or default_config()
    if storage_root is not None:
        base = dataclasses.replace(base, storage_root=storage_root)
    return Platform(base, clock=clock, unit=unit)
