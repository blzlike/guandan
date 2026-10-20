"""策略注册中心（阶段一 架构打底）。

所有 AI 牌力方案经此统一接入：``choose(game, pid, profile, sid)``。
首批注册 classic_low / classic_high，与直接调用 ``ai.choose_action``
逐字节等价（tests/test_arch.py 回归）。阶段五注册 pimc（夯档）；
阶段六注册分级档 novice / intermediate / advanced（经典档保留不动）。

档位解析：settings["ai_strength"] → sid（novice/intermediate/advanced
三档；low/high/pimc 为旧档位兼容映射）。座位策略覆盖已废除，全部
AI 座位跟随全局档位；settings["seats"] 键不再读取（保存时清除）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

import ai
from ai import AIProfile

# choose(game, pid, profile, **opts) -> ("play", cards) | ("pass", None)
ChooseFn = Callable[..., Tuple[str, Optional[list]]]
# choose_detail(...) -> (kind, cards, detail|None)；detail=候选评分明细（ai 侧）
ChooseDetailFn = Callable[..., Tuple[str, Optional[list], Optional[dict]]]


@dataclass(frozen=True)
class StrategySpec:
    sid: str
    name: str
    desc: str
    choose: ChooseFn
    choose_detail: Optional[ChooseDetailFn] = None   # 未实现时 choose_detail 回退


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


def choose_detail(game, pid: int, profile: Optional[AIProfile], sid: str,
                  **opts) -> Tuple[str, Optional[list], Optional[dict]]:
    """choose 的明细版：附带候选动作评分明细（决策只算一次）。

    classic 档返回 ``ai.choose_action_detail`` 的 detail（candidates/
    n_candidates）；未实现明细的策略（pimc）返回 detail=None，快照候选
    留空（阶段四统一收益口径后补齐）。
    """
    spec = _REGISTRY.get(sid)
    if spec is None:
        raise KeyError(f"未注册策略: {sid}")
    if spec.choose_detail is not None:
        return spec.choose_detail(game, pid, profile, **opts)
    kind, cards = spec.choose(game, pid, profile, **opts)
    return kind, cards, None


_GLOBAL_STRENGTH = {"novice": "novice", "intermediate": "intermediate",
                    "advanced": "advanced", "value": "value",
                    # 旧档位兼容映射（low/high/pimc 已退出设置界面，
                    # classic_low/classic_high/pimc 保留注册供 bench/测试调用）
                    "low": "classic_low", "high": "classic_high",
                    "pimc": "pimc"}


def resolve_seat(settings: dict, pid: int) -> str:
    """全局牌力解析：按 settings["ai_strength"] 返回 sid（pid 仅保留形参）。

    座位策略覆盖已废除——全部 AI 座位跟随全局档位；未知值回退 advanced
    （当前默认档）。"""
    return _GLOBAL_STRENGTH.get(settings.get("ai_strength"), "advanced")


# ---------------------------------------------------------------- 经典选手
def _classic_low(game, pid, profile, **opts):
    return ai.choose_action(game, pid, profile, enhanced=False, features=None)


def _classic_low_detail(game, pid, profile, **opts):
    return ai.choose_action_detail(game, pid, profile,
                                   enhanced=False, features=None)


def _classic_high(game, pid, profile, **opts):
    return ai.choose_action(game, pid, profile,
                            enhanced=True, features=ai.HIGH_FEATURES)


def _classic_high_detail(game, pid, profile, **opts):
    return ai.choose_action_detail(game, pid, profile,
                                   enhanced=True, features=ai.HIGH_FEATURES)


register(StrategySpec("classic_low", "经典·低", "V1.0 基础策略",
                      _classic_low, _classic_low_detail))
register(StrategySpec("classic_high", "经典·高",
                      "记牌器+残局搜索+已验证特性",
                      _classic_high, _classic_high_detail))

# ---------------------------------------------------------------- 夯档 PIMC（阶段五）
def _pimc(game, pid, profile, **opts):
    import plan_b
    return plan_b.choose(game, pid, profile, **opts)


register(StrategySpec("pimc", "夯", "关键时刻 PIMC 采样推演，其余沿用经典·高",
                      _pimc))

# ---------------------------------------------------------------- 分级档（阶段六）
# 梯度设计（经典档 classic_low/classic_high 保留不动，行为逐字节不变）：
#   novice       基础引擎（≈经典·低）：无记牌器/无残局搜索/无特性，算力最省；
#   intermediate 记牌器 + urgent_top（历史 A/B 唯一单开显著正收益特性）；
#   advanced     全特性 + gain_team 领出协同分项 + 快照候选 8（残局阈值保持
#                经典 8；risk 分项经 bench A/B 证伪关闭，见下）。
# bench A/B 校准（2026-10-17，300 场同牌口径 vs intermediate，seed 2000..2299）：
#   risk=25 → 轮胜 35.4%（双下率 38%、用炸 -30%，过度保守全面劣化）；
#   risk=6  → 46.3%；risk=2 → 50.2%；risk=0 → 51.6%（单调，risk 关闭）；
#   endgame_hand=10（risk=0）→ 50.7%，无增益，维持经典 8。
# 最终梯度见任务书附录 D 阶段六（intermediate 55.3% ≫ novice；
# advanced ≈ 51.6% 优于 intermediate，risk 分项保留代码、系数 0）。
# 协同修复（2026-10-20）：advanced 原同时带 feed 硬覆盖与 gain_team 评分
# （team_feed_lead=1.0），两者触发窗口重叠（领出+对家报牌 1-2 张），
# feed 提前 return 使评分从未生效 → advanced 与 classic_high 逐字节等价
# （40 场 25999 决策 0 差异）。修复：advanced 特性集去掉 FEATURE_FEED
# （硬覆盖送牌保留给 classic_high），改走 gain_team 评分式协同（含
# can_finish 守卫防一手出完被惩罚）；classic_low/classic_high/pimc/
# novice/intermediate 配置零变化（各档独立决策，互不影响）。
_BUDGET_NOVICE = ai.Budget(endgame_hand=0, max_candidates=4)
_BUDGET_INTERMEDIATE = ai.Budget(max_candidates=6)
_BUDGET_ADVANCED = ai.Budget(endgame_hand=8, max_candidates=8,
                             weights=ai.Weights(risk=0.0, team_feed_lead=1.0))


def _tier(enhanced: bool, feats: frozenset, budget: ai.Budget):
    """分级档工厂：固定 enhanced/特性集/预算的 choose/choose_detail 对。"""
    def choose(game, pid, profile, **opts):
        return ai.choose_action(game, pid, profile, enhanced=enhanced,
                                features=feats, budget=budget)

    def choose_detail(game, pid, profile, **opts):
        return ai.choose_action_detail(game, pid, profile, enhanced=enhanced,
                                       features=feats, budget=budget)
    return choose, choose_detail


_nov = _tier(False, frozenset(), _BUDGET_NOVICE)
_inter = _tier(True, frozenset({ai.FEATURE_URGENT_TOP}), _BUDGET_INTERMEDIATE)
_adv = _tier(True, ai.HIGH_FEATURES - {ai.FEATURE_FEED}, _BUDGET_ADVANCED)

register(StrategySpec("novice", "初级", "基础规则引擎，轻量快速",
                      _nov[0], _nov[1]))
register(StrategySpec("intermediate", "中级",
                      "记牌器+报牌压制（urgent_top）",
                      _inter[0], _inter[1]))
register(StrategySpec("advanced", "高级",
                      "记牌器+领出协同评分+深残局搜索",
                      _adv[0], _adv[1]))

# ---------------------------------------------------------------- 夯级（胜利值公式档）
# 平行新增档：胜利值 = 牌力 × 压制 × 对家增益 × 终局乘数（value_ai.py），
# 不改动经典/分级/PIMC 与规则引擎。步骤④起进入设置页（_GLOBAL_STRENGTH
# 含 "value"，resolve_seat 直返）；bench/测试按 sid 直调。
def _value(game, pid, profile, **opts):
    import value_ai
    return value_ai.choose(game, pid, profile, **opts)


def _value_detail(game, pid, profile, **opts):
    import value_ai
    return value_ai.choose_detail(game, pid, profile, **opts)


register(StrategySpec("value", "夯级",
                      "胜利值公式：牌力×压制×对家增益×终局乘数",
                      _value, _value_detail))

# 占位（远期注册实现）：
# register(StrategySpec("dmc", "模型", "Deep Monte-Carlo 自博弈策略", ...))
