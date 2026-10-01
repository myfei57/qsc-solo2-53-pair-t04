"""集气管与桥管。

装煤前必须先把集气管压力调到目标带，再把桥管翻板打开放荒煤气；翻板闩锁防
止同一炉被重复开阀。压力、翻板、闩锁三者共同构成"先调压、后开翻板"的硬
顺序。

自动找开度**按集气管整体调**：一个总管压力控制器把集气管压力折成一条开度
令，经开度时序仲裁（死区、驻留、反向延时、变化率）后同时下发给在役各炉；
按炉只做翻板执行／回读与荒煤气累计量。快调／缓调按在役炉况切换，翻板回读
跳动按部分失败隔离，压力／开度／每炉累计量逐周期写审计并可重放核对。
"""

from __future__ import annotations

from .audit import reconcile_gas
from .bank import FlapBank
from .collector import CollectorMain
from .controller import MODE_FAST, MODE_SLOW, CollectorPressureController, choose_mode
from .flap import BridgeFlap
from .latch import FlapLatch
from .ledger import GasLedger, OvenGasBill
from .meter import OvenGasMeter, estimate_flow_m3_s
from .pressure import regulate_pressure
from .sequence import OpeningSequencer
from .snapshot import GasSnapshot

__all__ = [
    "CollectorMain",
    "BridgeFlap",
    "FlapBank",
    "FlapLatch",
    "CollectorPressureController",
    "OpeningSequencer",
    "GasLedger",
    "OvenGasBill",
    "OvenGasMeter",
    "choose_mode",
    "estimate_flow_m3_s",
    "reconcile_gas",
    "regulate_pressure",
    "GasSnapshot",
    "MODE_FAST",
    "MODE_SLOW",
]
