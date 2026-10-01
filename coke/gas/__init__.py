"""集气管与桥管。

装煤前必须先把集气管压力调到目标带，再把桥管翻板打开放荒煤气；翻板闩锁防
止同一炉被重复开阀。压力、翻板、闩锁三者共同构成"先调压、后开翻板"的硬
顺序。

自动找开度定死在集气管整体层：:class:`GasRegulation` 内只有一个压力闭环，
其输出的总通流份额按炉况权重分到各炉连续翻板；单炉不持有压力闭环。每炉按
同一条通流曲线累计荒煤气抽走量，并由 :mod:`reconcile` 做压力、开度、累计量
三者的一致性核对。
"""

from __future__ import annotations

from .collector import CollectorMain
from .condition import OVEN_PHASES, TUNE_MODES, TuneDecision, decide_tune
from .controller import CollectorPressureController, PressureVerdict, allocate_opening
from .flap import BridgeFlap, ReadbackMonitor
from .latch import FlapLatch
from .pressure import regulate_pressure
from .reconcile import reconcile_cycle, reconcile_totals
from .regulation import GasRegulation, OvenChannel
from .snapshot import GasSnapshot
from .timing import FlapTiming, TimingVerdict
from .totalizer import ChargeTotal, OvenGasTotalizer, instantaneous_flow_nm3h

__all__ = [
    "CollectorMain",
    "BridgeFlap",
    "FlapLatch",
    "regulate_pressure",
    "GasSnapshot",
    "OVEN_PHASES",
    "TUNE_MODES",
    "TuneDecision",
    "decide_tune",
    "CollectorPressureController",
    "PressureVerdict",
    "allocate_opening",
    "ReadbackMonitor",
    "reconcile_cycle",
    "reconcile_totals",
    "GasRegulation",
    "OvenChannel",
    "FlapTiming",
    "TimingVerdict",
    "ChargeTotal",
    "OvenGasTotalizer",
    "instantaneous_flow_nm3h",
]
