"""策略注册中心（阶段一 架构打底）。

所有 AI 牌力方案经此统一接入：``choose(game, pid, profile, sid)``。
首批注册 classic_low / classic_high，与直接调用 ``ai.choose_action``
逐字节等价（tests/test_arch.py 回归）。后续 pimc（夯档，阶段五）、
dmc（自博弈模型，远期）仅注册不实现。

座位解析：settings["seats"] 为 {str(pid): sid 或 None}，缺失 / None /
未知 sid 一律回退全局默认（settings["ai_strength"]："high"→classic_high，
否则 classic_low）。旧 settings.json 无 seats 键天然兼容。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

import ai
from ai import AIProfile

# choose(game, pid, profile, **opts) -> ("play", cards) | ("pass", None)
ChooseFn = Callable[..., Tuple[str, Optional[list]]]


@dataclass(frozen=True)
class StrategySpec:
    sid: str
    name: str
    desc: str
    choose: ChooseFn


_REGISTRY: Dict[str, StrategySpec] = {}
_ORDER: List[str] = []


def register(spec: StrategySpec) -> None:
    if spec.sid in _REGISTRY:
        raise ValueError(f"策略重复注册: {spec.sid}")
    _REGISTRY[spec.sid] = spec
    _ORDER.append(spec.sid)


def get(sid: str) -> Optional[StrategySpec]:
    return _REGISTRY.get(sid)


def ids() -> List[str]:
    return list(_ORDER)


def choose(game, pid: int, profile: Optional[AIProfile], sid: str,
           **opts) -> Tuple[str, Optional[list]]:
    spec = _REGISTRY.get(sid)
    if spec is None:
        raise KeyError(f"未注册策略: {sid}")
    return spec.choose(game, pid, profile, **opts)


_GLOBAL_STRENGTH = {"high": "classic_high", "pimc": "pimc"}


def resolve_seat(settings: dict, pid: int) -> str:
    """座位策略解析：每座覆盖优先，缺失/None/未知回退全局默认。"""
    seats = settings.get("seats") or {}
    sid = seats.get(str(pid)) if isinstance(seats, dict) else None
    if sid in _REGISTRY:
        return sid
    return _GLOBAL_STRENGTH.get(settings.get("ai_strength"), "classic_low")


# ---------------------------------------------------------------- 经典选手
def _classic_low(game, pid, profile, **opts):
    return ai.choose_action(game, pid, profile, enhanced=False, features=None)


def _classic_high(game, pid, profile, **opts):
    return ai.choose_action(game, pid, profile,
                            enhanced=True, features=ai.HIGH_FEATURES)


register(StrategySpec("classic_low", "经典·低", "V1.0 基础策略", _classic_low))
register(StrategySpec("classic_high", "经典·高",
                      "记牌器+残局搜索+已验证特性", _classic_high))

# ---------------------------------------------------------------- 夯档 PIMC（阶段五）
def _pimc(game, pid, profile, **opts):
    import plan_b
    return plan_b.choose(game, pid, profile, **opts)


register(StrategySpec("pimc", "夯", "关键时刻 PIMC 采样推演，其余沿用经典·高",
                      _pimc))

# 占位（远期注册实现）：
# register(StrategySpec("dmc", "模型", "Deep Monte-Carlo 自博弈策略", ...))
