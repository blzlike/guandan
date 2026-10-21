"""夯级AI（value 档）：胜利值公式决策内核（平行新增，不改动既有档位）。

胜利值 = 牌力 × 压制系数 × 对家增益 × 终局乘数，取最高值出牌。
经典/分级/PIMC 各档与规则引擎、训练框架均不动；本模块只读 game 公开
状态，复用 rules 的牌型与同花顺枚举，不自行实现合法性校验。

步骤①（已完成）：手牌分解器 + 牌力因子。
- decompose(): 手牌→牌组。火牌（天王炸/炸弹/同花顺）优先独立成组，
  逢人配分配（配火 4/5 头炸、自然炸弹升头、不配留作级牌）枚举为多个
  火牌方案逐一定（上限 MAX_FIRE_PLANS，2026-10-21 用户定「以最大牌力
  作为基准」，取代旧配火贪心）；其余枚举序列窗口组合（上限
  MAX_SEQ_PLANS 个叶方案，确定性）按「最大牌力」选基础方案：
  牌力=控制系数÷(非炸手数+1) 最大，平局取组数少、用配多、目标点值
  高、先枚举者（2026-10-20 用户定：全程组合最大牌力的组合作为基础
  方案，其他方案作为备选）。同手牌结果缓存（键=eids+级牌，纯函数可
  跨局复用）。窗口约定与 rules.classify 一致（A 可作 1、2 可入序列、
  王只作单/对）。
- power_of(): 控制力按底表 v2 逐组计值（每组一值：顶级组 1.0——单/对
  大王、级牌顶三张/三带二，其余非火组=顶点×0.001 小补偿；全部非炸牌型
  含「顶级类出完→次大牌组继承 1.0」提升规则）+ 非炸弹手数（每组
  恒 1 手）→ 牌力 = 控制力 ÷ (非炸弹手数 + 1)。
- Group.point_str(): 点数串（快照面板手牌清单行），组内牌按
  rules.display_order 升序，10→X、逢人配→$、天王炸固定名。

步骤②（已完成）：三因子 + 决策入口。
- estimate_hands(): 三阶段手数估计（>20 张开局不参与、中局剩张÷2.2、
  残局 ≤10 张按分解手数×近期接/过牌修正）。
- pressure_factor(): 压制系数 = 1 + Σ_对手(手数增量×影响力)，增量 v1
  简化式=拆组/动炸各+1×被压概率（复用 inference 公开口径），影响力
  =1/(估计手数^1.5)；推演式接口预留（步骤⑤）。
- partner_gain(): 对家增益 = 1 + 手数减量÷(对家估计手数+1)，可送由
  历史接牌率 >60% 判定。
- endgame_multiplier(): 终局乘数四条件连乘（对手剩1手可阻断×3.0｜
  对手≤2手用炸×2.0｜自己一手出完×5.0｜送对家冲刺×2.5），替代独立
  硬约束层，拆牌成本由牌力因子自动体现。
- choose/choose_detail(): 候选（同型去重保最优，上限 12）按胜利值
  取最高，平局按 (牌型定义序, rank, eid) 确定性决胜；不用 rng。
  进贡/还贡不经过本档（牌局流程统一走 ai.choose_tribute/choose_return）。

步骤③（已完成）：三级记录 + 校准检测。
- 候选记录：choose_detail 候选含 score_total（value_total 别名，快照
  final 汇总读取）、cur_power 当前整手牌力与 power 牌力明细键，另含
  context 局面级参数（当前牌力/对手·对家估计手数/对家可送），经
  ui._ai_step → DecisionLogger.capture 落 DecisionStore
  （decisions/<seed>.jsonl）。
- 校准检测：calib.py 局终对照校准常量（手数/出炸频率偏离 ±25%），
  偏离经 review.py 标记复盘；本模块不 import review/calib（AI 侧不得
  反向依赖校核层）。

后续步骤：④ 设置页/复盘面板按 sid 切列/bench A/B；⑤ 调优。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

import inference
from cards import BIG_JOKER, RANK_LABELS, SMALL_JOKER, Card, point_value
from rules import (Combo, HandType, RuleConfig, beats, classify,
                   display_order, find_straight_flushes,
                   generate_legal_moves)

# ---------------------------------------------------------------- 参数
# 控制力底表 v2（2026-10-20 用户定，训练基础——后续调参只改本节常量，
# 不动 _group_control 逻辑）：非火牌型控制力只有两档——顶级组 1.0、
# 其余 = 顶点×CTRL_STEP 小补偿（无档基，仅作同型排序区分，2026-10-20
# 第二次修订：废除 0/0.2/0.4 与王/级牌固定档值）。
CTRL_STEP = 0.001
TOP_CTRL = 1.0             # 顶级控制力（提升规则目标值，只升不降）

# 顶级组（控制力 1.0）：单张/对子=大王（顶级牌）、三张/三带二=级牌顶；
# 序列组（顺子/连对/钢板）级牌按面值参与、无静态顶级，仅经提升规则
# 达到 1.0。单张/对子其余点值：小王 0.016、级牌 0.015、A 0.014…
TRIPLE_LEVEL_CTRL = 1.0    # 级牌三张/级牌三带二（封顶牌组）

# 火牌底表（层级序与 rules 一致：四炸<五炸<同花顺<六炸<七炸<八炸<天王炸）
BOMB_TIERS = ((2.0, 5), (2.2, 10), (2.4, 14))   # 四头炸档基（5-2/10-6/J-A）
BOMB_BASE = {5: 2.6, 6: 3.0, 7: 3.1, 8: 3.2}    # 五头起不分档
BOMB_LEVEL_CTRL = {4: 2.5, 5: 2.7, 6: 3.02, 7: 3.12, 8: 3.22}  # 级牌炸专用值
SF_BASE_CTRL = 2.8         # 同花顺：2.8 + 顶点×CTRL_STEP（AKQJ10=2.814）
ROCKET_CTRL = 3.3          # 天王炸

# 提升规则（全部非炸牌型，2026-10-20 用户定）：顶牌类之上任一牌类剩余
# 张数 ≥ 成牌所需张数 → 维持底表值；全部不足（更高牌已无法成牌）→ 提升
# 为顶级 TOP_CTRL（级联 大王→小王→级牌→A→…，max 保底只升不降）。
# 「出完」按 game.played_cards 计数，剩余 = 总张数（普通/级牌各 8、王 2）
# − 已出；成牌所需张数（压制方视角）见 _PROMO_NEED。序列组顶牌类按面值
# （级牌按面值参与序列，与底表口径一致，保型内排序）；decompose 仍用
# 静态底表（缓存键不变），提升只作用于计值与快照显示。
_PROMO_NEED = {
    HandType.SINGLE: 1, HandType.PAIR: 2, HandType.TRIPLE: 3,
    HandType.TRIPLE_PAIR: 3, HandType.STRAIGHT: 1,
    HandType.PAIR_CHAIN: 2, HandType.TRIPLE_CHAIN: 3,
    HandType.STRAIGHT_FLUSH: 1,
}

# 牌型短名（快照面板统一行格式「类型+点数串」，2026-10-20 用户定：
# 炸弹7777、三二55544；三带二缩写「三二」、三同张缩写「三张」）
_TYPE_SHORT = {
    HandType.SINGLE: "单张",
    HandType.PAIR: "对子",
    HandType.TRIPLE: "三张",
    HandType.TRIPLE_PAIR: "三二",
    HandType.STRAIGHT: "顺子",
    HandType.PAIR_CHAIN: "连对",
    HandType.TRIPLE_CHAIN: "钢板",
    HandType.BOMB: "炸弹",
    HandType.STRAIGHT_FLUSH: "同花顺",
    HandType.ROCKET: "天王炸",
}


def _cards_str(cards: Sequence[Card], level_rank: int) -> str:
    """点数串：10→X、逢人配→$、王/级牌按 RANK_LABELS（牌序由调用方定）。"""
    return "".join(
        "$" if c.is_wild(level_rank)
        else "X" if c.rank == 10
        else RANK_LABELS.get(c.rank, str(c.rank))
        for c in cards)

# ---------------------------------------------------------------- 牌组
@dataclass(frozen=True)
class Group:
    """分解出的一个牌组。rank 口径同 rules.Combo：火/单张为 point_value
    编码，序列类为顶牌原始点数（A2345 顶牌记 5）。"""

    type: HandType
    rank: int
    cards: Tuple[Card, ...]

    @property
    def size(self) -> int:
        return len(self.cards)

    @property
    def eids(self) -> Tuple[int, ...]:
        return tuple(sorted(c.eid for c in self.cards))

    @property
    def is_fire(self) -> bool:
        return self.type in (HandType.BOMB, HandType.STRAIGHT_FLUSH,
                             HandType.ROCKET)

    def max_point(self, level_rank: int) -> int:
        return max(point_value(c.rank, level_rank) for c in self.cards)

    def is_weak(self, level_rank: int) -> bool:
        """弱牌组：组内最高点 < 级牌（仅对非火牌组有意义）。"""
        return self.max_point(level_rank) < point_value(level_rank, level_rank)

    def label(self) -> str:
        """资源标签（控制系数明细键 / 面板行）：如 炸弹5x4 / 同花顺A /
        天王炸 / 单A / 对K / 顺子Q。非火组带点数以区分双副牌同点跨组
        （2026-10-20 用户反馈重复观感，序列组原仅显示类型名）。"""
        if self.type is HandType.ROCKET:
            return "天王炸"
        if self.type is HandType.STRAIGHT_FLUSH:
            return f"同花顺{RANK_LABELS.get(self.rank, self.rank)}"
        if self.type is HandType.BOMB:
            raw = self.cards[0].rank
            return f"炸弹{RANK_LABELS.get(raw, raw)}x{self.size}"
        if self.type is HandType.PAIR:
            return f"对{RANK_LABELS.get(self.cards[0].rank, self.cards[0].rank)}"
        if self.type is HandType.TRIPLE:
            return f"三{RANK_LABELS.get(self.cards[0].rank, self.cards[0].rank)}"
        if self.type is HandType.TRIPLE_PAIR:
            return "三带二"
        if self.type is HandType.SINGLE:
            return f"单{RANK_LABELS.get(self.cards[0].rank, self.cards[0].rank)}"
        if self.type is HandType.STRAIGHT:
            return f"顺子{RANK_LABELS.get(self.rank, self.rank)}"
        if self.type is HandType.PAIR_CHAIN:
            return f"连对{RANK_LABELS.get(self.rank, self.rank)}"
        if self.type is HandType.TRIPLE_CHAIN:
            return f"钢板{RANK_LABELS.get(self.rank, self.rank)}"
        return self.type.value

    def point_str(self, level_rank: int) -> str:
        """点数串（快照面板手牌清单行）：组内牌按 rules.display_order
        升序（三带二三张在前），10→X、逢人配→$；天王炸固定名
        （2026-10-20 用户定：顺子45678、炸弹7777、钢板444555、三带二88822）。"""
        if self.type is HandType.ROCKET:
            return "天王炸"
        return _cards_str(display_order(self.cards, level_rank), level_rank)

    def desc(self, level_rank: int) -> str:
        """类型短名+点数串（快照三表统一行格式，2026-10-20 用户定）：
        如 炸弹7777 / 三二55544 / 顺子45678 / 单张大王。"""
        if self.type is HandType.ROCKET:
            return "天王炸"
        return _TYPE_SHORT[self.type] + self.point_str(level_rank)


# ---------------------------------------------------------------- 分解器
MAX_SEQ_PLANS = 256        # 序列窗口叶方案枚举上限（超出确定性截断）
MAX_FIRE_PLANS = 64        # 逢人配分配方案枚举上限（双逢人配极端局面截断）
_DEC_CACHE: Dict[Tuple[Tuple[int, ...], int], List[Group]] = {}
_DEC_CACHE_MAX = 4096      # 缓存上限（满则清空；eid 唯一可跨局复用）


def _enumerate_windows(counts: Dict[int, List[Card]]
                       ) -> List[Tuple[int, int, HandType, Tuple[Card, ...]]]:
    """枚举初始可用的全部序列窗口实例（与 rules.classify 窗口约定一致：
    A 可作 1、2 可入序列、王不参与）。

    返回 (每点张数, 顶牌原始点数, 牌型, 窗口牌) 列表，按 (-每点张数, 顶牌)
    排序：钢板→连对→顺子先探索，同型起点升序，先枚举者平局居先；同
    (型,起点) 多份实例（两副牌同点重复）按各点计数逐份切块——同点牌
    等价，切块不影响牌力。
    """
    specs = ((3, RuleConfig.TRIPLE_CHAIN_SETS, HandType.TRIPLE_CHAIN),
             (2, RuleConfig.PAIR_CHAIN_PAIRS, HandType.PAIR_CHAIN),
             (1, RuleConfig.STRAIGHT_LENGTH, HandType.STRAIGHT))
    out: List[Tuple[int, int, HandType, Tuple[Card, ...]]] = []
    for per, length, t in specs:
        for start in range(1, 16 - length):   # 起点域与 classify 同
            pts = [14 if r == 1 else r for r in range(start, start + length)]
            k = min(len(counts.get(p, ())) // per for p in pts)
            for i in range(k):
                taken: List[Card] = []
                for p in pts:
                    taken.extend(counts[p][i * per:(i + 1) * per])
                out.append((per, start + length - 1, t, tuple(taken)))
    out.sort(key=lambda w: (-w[0], w[1]))
    return out


def decompose(cards: Sequence[Card], level_rank: int) -> List[Group]:
    """手牌 → 牌组分解（确定性：同输入同输出，带结果缓存）。

    火牌优先独立成组：天王炸 → 自然炸弹（同点≥4，恒保持）→ 同花顺
    （每花色取顶牌最大者，自然牌优先）。逢人配分配（恰好 3 张级配火
    4/5 头炸、自然炸弹升头、不配留作级牌）枚举为多个火牌方案
    （上限 MAX_FIRE_PLANS），每方案独立搜索非火分组，全局取
    牌力=控制系数÷(非炸手数+1) 最大者；平局取组数少、用配多、目标
    点值高、先枚举者（2026-10-21 用户定「以最大牌力作为基准」，取代
    旧配火贪心——贪心会把逢人配焊给点值最高的三张级，错失更大组合，
    如 KKK$ 挤掉 888$+同花顺）。逢人配未配火时按自然点（级牌）参与
    非火分组/同花顺补位。
    """
    key = (tuple(sorted(c.eid for c in cards)), level_rank)
    hit = _DEC_CACHE.get(key)
    if hit is not None:
        return list(hit)
    if len(_DEC_CACHE) >= _DEC_CACHE_MAX:
        _DEC_CACHE.clear()
    result = _decompose_impl(cards, level_rank)
    _DEC_CACHE[key] = result
    return list(result)


def _wild_assignments(cnt: Dict[int, List[Card]], wild_n: int,
                      level_rank: int) -> List[Dict[int, int]]:
    """逢人配分配方案枚举（2026-10-21 用户定「以最大牌力作为基准」）。

    每方案 = {rank: 配入数}（仅列非零项）。目标级域 = 恰好 3 张的级
    （配火成 4/5 头炸）∪ ≥4 张自然炸弹级（升头，炸身 ≤8 头）；含空
    分配 {}（逢人配按自然点参与非火分组/同花顺补位）。双逢人配注入
    式分配：可集中同级（升两头/配五头）或分给两级。方案顺序 = 用配
    总数降序 → 目标点值降序（平局保配火倾向，与旧贪心一致），超出
    MAX_FIRE_PLANS 按 DFS 序确定性截断。无逢人配时恒返回 [{}]，路径
    与旧实现零差异。
    """
    if wild_n <= 0:
        return [{}]
    targets: List[Tuple[int, int]] = []
    for r, cs in cnt.items():
        n = len(cs)
        cap = (8 - n) if n >= RuleConfig.BOMB_MIN_SIZE else (2 if n == 3 else 0)
        if cap >= 1:
            targets.append((r, min(cap, wild_n)))
    if not targets:
        return [{}]
    ranks = sorted((r for r, _c in targets),
                   key=lambda r: -point_value(r, level_rank))
    cap_of = dict(targets)
    plans: List[Tuple[Tuple, Dict[int, int]]] = []

    def _rec(i: int, left: int, cur: Dict[int, int]) -> None:
        if i == len(ranks):
            if len(plans) < MAX_FIRE_PLANS:
                skey = tuple(sorted((-point_value(r, level_rank), k)
                                    for r, k in cur.items()))
                plans.append((skey, dict(cur)))
            return
        r = ranks[i]
        for k in range(min(cap_of[r], left), -1, -1):
            if k:
                cur[r] = k
            _rec(i + 1, left - k, cur)
            cur.pop(r, None)

    _rec(0, wild_n, {})
    plans.sort(key=lambda t: (-sum(t[1].values()), t[0]))
    return [m for _skey, m in plans]


def _nonfire_search(counts: Dict[int, List[Card]], left_jokers: List[Card],
                    fire_ctrl: float,
                    level_rank: int) -> Tuple[float, int, List[Group]]:
    """非火部分最大牌力搜索（原步骤5，逐火牌方案调用）：枚举序列窗口
    子集（上限 MAX_SEQ_PLANS 叶）× 贪心收尾（三带二→三/对/单→王组），
    返回 (牌力值, 非火组数, 非火组列表)；牌力 = (fire_ctrl+非火控制)
    ÷ (非火手数+1)，叶内平局取组数少、先枚举者（严格 < 保确定性）。
    控制力按底表逐组计值（_group_control 静态底表，与缓存纯函数性
    一致）；非炸手数=非火组数（每组恒 1 手，2026-10-20 用户定）。"""
    windows = _enumerate_windows(counts)
    used: Set[int] = set()
    best: List[Group] = []
    best_key: Optional[Tuple[float, int]] = None
    best_val = 0.0
    plan_n = 0

    def _finish() -> List[Group]:
        """剩余计数贪心收尾：三带二（三张配最小对，升序确定性）→
        三/对/单（while 拆分，级牌计数可因未配火 wild 达 4 张）→
        王组（同种对，其余单张）。"""
        cc = {r: [c for c in cs if c.eid not in used]
              for r, cs in counts.items()}
        cc = {r: v for r, v in cc.items() if v}
        out: List[Group] = []
        triples = sorted((r for r, v in cc.items() if len(v) == 3),
                         key=lambda r: point_value(r, level_rank))
        pairs = sorted((r for r, v in cc.items() if len(v) == 2),
                       key=lambda r: point_value(r, level_rank))
        for t in triples:
            pr = next((p for p in pairs if p != t), None)
            if pr is None:
                break
            pairs.remove(pr)
            out.append(Group(HandType.TRIPLE_PAIR, point_value(t, level_rank),
                             tuple(cc[t] + cc[pr])))
            cc[t] = []
            cc[pr] = []
        for r in sorted((r for r, v in cc.items() if v),
                        key=lambda r: point_value(r, level_rank)):
            cs = list(cc[r])
            while cs:
                take = 3 if len(cs) >= 3 else len(cs)
                chunk = cs[:take]
                del cs[:take]
                tt = {1: HandType.SINGLE, 2: HandType.PAIR,
                      3: HandType.TRIPLE}[take]
                out.append(Group(tt, point_value(r, level_rank), tuple(chunk)))
        same_jok: Dict[int, List[Card]] = {}
        for c in left_jokers:
            same_jok.setdefault(c.rank, []).append(c)
        for r in sorted(same_jok):
            cs = same_jok[r]
            if len(cs) == 2:
                out.append(Group(HandType.PAIR, point_value(r, level_rank),
                                 tuple(cs)))
            else:
                for c in cs:
                    out.append(Group(HandType.SINGLE,
                                     point_value(r, level_rank), (c,)))
        return out

    def _leaf(seq_gs: List[Group]) -> None:
        nonlocal plan_n, best_key, best, best_val
        plan_n += 1
        extra = _finish()
        ctrl = fire_ctrl + sum(_group_control(g, level_rank)
                               for g in seq_gs + extra)
        hands = float(len(seq_gs) + len(extra))
        value = ctrl / (hands + 1.0)
        key = (-round(value, 9), len(seq_gs) + len(extra))
        if best_key is None or key < best_key:
            best_key = key
            best_val = value
            best = seq_gs + extra

    def _dfs(i: int, seq_gs: List[Group]) -> None:
        if plan_n >= MAX_SEQ_PLANS:
            return
        if i == len(windows):
            _leaf(seq_gs)
            return
        _per, top, t, wcs = windows[i]
        if all(c.eid not in used for c in wcs):
            used.update(c.eid for c in wcs)
            seq_gs.append(Group(t, top, wcs))
            _dfs(i + 1, seq_gs)
            seq_gs.pop()
            used.difference_update(c.eid for c in wcs)
        _dfs(i + 1, seq_gs)

    _dfs(0, [])
    if best_key is None:
        return 0.0, 0, []
    return best_val, best_key[1], best


def _decompose_impl(cards: Sequence[Card], level_rank: int) -> List[Group]:
    if not cards:
        return []
    rocket: List[Group] = []
    rest = list(cards)

    # 1) 天王炸（与逢人配无关，先独立）
    jokers = sorted((c for c in rest if c.is_joker),
                    key=lambda c: (c.rank, c.eid))
    if len(jokers) == 4:
        rocket.append(Group(HandType.ROCKET, 100, tuple(jokers)))
        rest = [c for c in rest if not c.is_joker]
        jokers = []

    # 2) 计数：自然炸弹级（≥4 张，恒保持成组——放弃实测均更差）、
    #    配火候选级（恰好 3 张）、逢人配
    wilds = sorted((c for c in rest if c.is_wild(level_rank)),
                   key=lambda c: c.eid)
    wild_set = {c.eid for c in wilds}
    plain = [c for c in rest if c.eid not in wild_set and not c.is_joker]
    cnt: Dict[int, List[Card]] = {}
    for c in sorted(plain, key=lambda c: (c.suit, c.eid)):
        cnt.setdefault(c.rank, []).append(c)
    natural_ranks = {r for r, cs in cnt.items()
                     if len(cs) >= RuleConfig.BOMB_MIN_SIZE}

    # 3) 逢人配分配方案枚举：每方案独立走「自然炸弹+配火/升头 → 同花顺
    #    （自然牌优先，留余逢人配仅补位）→ 非火最大牌力搜索」，全局取
    #    牌力=控制÷(非炸手数+1) 最大者（2026-10-21 用户定，取代旧配火
    #    贪心）。平局取组数少、用配多、目标点值高、先枚举者（严格 <
    #    保证确定性）。
    best: List[Group] = []
    best_key: Optional[Tuple[float, int, int]] = None
    for pi, assign in enumerate(
            _wild_assignments(cnt, len(wilds), level_rank)):
        gs = list(rocket)
        cc = {r: list(v) for r, v in cnt.items()}
        wi = 0
        for r in sorted(natural_ranks | set(assign),
                        key=lambda r: -point_value(r, level_rank)):
            k = assign.get(r, 0)
            cs = cc.pop(r) + wilds[wi:wi + k]
            wi += k
            gs.append(Group(HandType.BOMB, point_value(r, level_rank),
                            tuple(cs)))
        used_eids = {x.eid for g in gs for x in g.cards}
        remaining = ([c for cs in cc.values() for c in cs]
                     + wilds[wi:] + jokers)

        # 同花顺：自然牌优先（每花色顶牌最大者），留余逢人配仅补位成火
        if len(remaining) >= RuleConfig.STRAIGHT_LENGTH:
            naturals = [c for c in remaining
                        if not c.is_wild(level_rank) and not c.is_joker]
            for pool in (naturals, remaining):
                for combo in sorted(find_straight_flushes(pool, level_rank),
                                    key=lambda c: (-c.rank, int(c.suit or 0))):
                    if any(x.eid in used_eids for x in combo.cards):
                        continue
                    gs.append(Group(HandType.STRAIGHT_FLUSH, combo.rank,
                                    tuple(combo.cards)))
                    used_eids.update(x.eid for x in combo.cards)

        # 非火计数（逢人配按自然点=级牌参与）+ 最大牌力搜索
        left_jokers: List[Card] = []
        counts: Dict[int, List[Card]] = {}
        for c in remaining:
            if c.eid in used_eids:
                continue
            if c.is_joker:
                left_jokers.append(c)
            else:
                counts.setdefault(c.rank, []).append(c)
        value, nf_n, nf_groups = _nonfire_search(
            counts, left_jokers,
            sum(_group_control(g, level_rank) for g in gs), level_rank)
        key = (-round(value, 9), len(gs) + nf_n, pi)
        if best_key is None or key < best_key:
            best_key = key
            best = gs + nf_groups
    return best


# ---------------------------------------------------------------- 牌力
@dataclass
class PowerDetail:
    """牌力明细（步骤③候选记录直接引用）。"""

    control: float                    # 控制力总量
    control_detail: Dict[str, float] = field(default_factory=dict)
    non_bomb_hands: float = 0.0       # 非炸弹手数（每组恒 1 手）
    non_bomb_groups: int = 0          # 非火牌组数
    weak_groups: int = 0              # 弱牌组数（仅作显示信息，不再加权）
    value: float = 0.0                # 牌力 = control / (non_bomb_hands + 1)
    groups: Tuple[Group, ...] = ()


def _tier_value(top: int, tiers: Tuple[Tuple[float, int], ...]) -> float:
    """顶牌面值 → 档基 + 顶点×CTRL_STEP（四头炸弹三档专用）。"""
    for base, hi in tiers:
        if top <= hi:
            return base + top * CTRL_STEP
    return tiers[-1][0] + top * CTRL_STEP


def _promoted(top_pv: int, need: int, level_rank: int,
              played: Optional[Dict[int, int]]) -> bool:
    """提升判定（played=None 静态底表恒 False）：顶牌类之上任一牌类剩余
    张数 ≥ need 则可被更高同型牌压制（False）；全部不足 → 顶级（True）。
    牌类按 point_value 序（大王17>小王16>级牌15>A14…>2）；剩余=总张数
    （普通/级牌 8、王 2）−已出。"""
    if played is None:
        return False
    for pv in range(top_pv + 1, 18):
        if pv == 15:
            rank, total = level_rank, 8
        elif pv == 16:
            rank, total = SMALL_JOKER, 2
        elif pv == 17:
            rank, total = BIG_JOKER, 2
        else:
            rank, total = pv, 8
        if total - played.get(rank, 0) >= need:
            return False
    return True


def _group_control(g: Group, level_rank: int,
                   played: Optional[Dict[int, int]] = None) -> float:
    """单组控制力（底表 v2 2026-10-20 用户定，power_of/context/decompose 叶
    共用保证口径一致）：顶级组 1.0（单/对大王、级牌顶三张/三带二），其余
    非火组 = 顶点×CTRL_STEP 小补偿；非炸牌型含提升规则——顶级类出完 →
    次大牌组继承 1.0（max 保底只升不降，级联至当前有效顶）。
    「顶点」= 牌型比较点：序列类取顶牌面值（级牌按面值、A2345 记 5）、
    三张/三带二取三张部分、单/对取牌面（点值编码：级牌 15、小王 16）。"""
    t = g.type
    if t is HandType.ROCKET:
        return ROCKET_CTRL
    if t is HandType.BOMB:
        raw = g.cards[0].rank
        if raw == level_rank:
            return BOMB_LEVEL_CTRL.get(g.size, 2.5)
        if g.size == 4:
            return _tier_value(g.rank, BOMB_TIERS)
        return BOMB_BASE.get(g.size, 2.0) + g.rank * CTRL_STEP
    if t is HandType.STRAIGHT_FLUSH:
        base = SF_BASE_CTRL + g.rank * CTRL_STEP
        if _promoted(g.rank, _PROMO_NEED[t], level_rank, played):
            return max(base, TOP_CTRL)
        return base
    if t in (HandType.STRAIGHT, HandType.PAIR_CHAIN, HandType.TRIPLE_CHAIN):
        base = g.rank * CTRL_STEP            # 顶牌面值×0.001（级牌按面值）
        if _promoted(g.rank, _PROMO_NEED[t], level_rank, played):
            return max(base, TOP_CTRL)
        return base
    if t in (HandType.TRIPLE, HandType.TRIPLE_PAIR):
        raw = g.cards[0].rank                # 三张部分点值编码
        base = TRIPLE_LEVEL_CTRL if raw == level_rank else raw * CTRL_STEP
        if _promoted(g.rank, _PROMO_NEED[t], level_rank, played):
            return max(base, TOP_CTRL)
        return base
    # 单张/对子：大王顶级 1.0，其余 = 点值×0.001（小王 0.016、级牌 0.015…）
    if g.rank == point_value(BIG_JOKER, level_rank):
        base = 1.0
    else:
        base = g.rank * CTRL_STEP
    if _promoted(g.rank, _PROMO_NEED[t], level_rank, played):
        return max(base, TOP_CTRL)
    return base


def _played_counts(game) -> Dict[int, int]:
    """已出牌按牌面点数计数（提升规则「出完」的数据源，决策时刻口径）。"""
    pc: Dict[int, int] = {}
    for c in game.played_cards:
        pc[c.rank] = pc.get(c.rank, 0) + 1
    return pc


def power_of(cards: Sequence[Card], level_rank: int,
             played_counts: Optional[Dict[int, int]] = None) -> PowerDetail:
    """牌力 = 出牌后控制力 ÷ (出牌后非炸弹手数 + 1)。

    - 控制力：底表逐组计值（_group_control，每组一值）；played_counts
      提供时启用提升规则（顶级牌出完→该组提升，evaluate/context 传入）。
    - 非炸弹手数：非火牌组数，每组恒 1 手（弱组不再加权）。
    """
    groups = decompose(cards, level_rank)
    detail: PowerDetail = PowerDetail(control=0.0)

    def add(key: str, v: float) -> None:
        detail.control += v
        detail.control_detail[key] = detail.control_detail.get(key, 0.0) + v

    for g in groups:
        add(g.label(), _group_control(g, level_rank, played_counts))
        if not g.is_fire:
            detail.non_bomb_groups += 1
            detail.non_bomb_hands += 1.0
            if g.is_weak(level_rank):
                detail.weak_groups += 1

    hands = detail.non_bomb_hands
    detail.value = detail.control / (hands + 1.0)
    detail.groups = tuple(groups)
    return detail


# ---------------------------------------------------------------- 手数估计
OPEN_CARDS = 20           # 开局阈值：剩牌 >20 张不参与压制/增益比较
MID_DIVISOR = 2.2         # 中局手数估计：剩牌张数 ÷ 2.2
ENDGAME_CARDS = 10        # 残局阈值：剩牌 ≤10 张按分解手数+近期动作修正
RECENT_WINDOW = 6         # 残局修正观察窗：每家最近 N 个动作
RECENT_STEP = 0.1         # 每点净信号的手数修正幅度（净信号 clamp ±2）
ACCEPT_RATE = 0.6         # 历史接牌率 >60% 视为可送


def _count_hands(groups: Sequence[Group], level_rank: int) -> float:
    """出完手数（残局口径）：每组恒 1 手（2026-10-20 用户定，弱组不再加权）。"""
    return float(len(groups))


def recent_activity(game, pid: int) -> int:
    """pid 最近 RECENT_WINDOW 个动作的净信号：出牌 +1、过牌 −1。"""
    acts = [1 if kind == "play" else -1
            for p, kind, _c, _l in getattr(game, "round_history", ()) or ()
            if p == pid]
    return sum(acts[-RECENT_WINDOW:])


def estimate_hands(game, pid: int) -> Optional[float]:
    """三阶段手数估计（无状态，公开口径）。

    - 开局（剩牌 > OPEN_CARDS）：None，压制/增益不参与比较；
    - 中局（ENDGAME_CARDS < 剩牌 ≤ OPEN_CARDS）：剩牌张数 ÷ MID_DIVISOR；
    - 残局（剩牌 ≤ ENDGAME_CARDS）：分解手数 ×(1 − RECENT_STEP×净信号)
      （净接牌下调、净过牌上调，净信号 clamp ±2 → 修正 0.8~1.2），
      下限 0.5。
    """
    n = game.hand_count(pid)
    if n > OPEN_CARDS:
        return None
    if n > ENDGAME_CARDS:
        return n / MID_DIVISOR
    level = game.level
    base = _count_hands(decompose(game.hands[pid], level), level)
    net = max(-2, min(2, recent_activity(game, pid)))
    return max(0.5, base * (1.0 - RECENT_STEP * net))


# ---------------------------------------------------------------- 被压/接牌
def beat_prob(game, pid: int, combo: Combo) -> float:
    """pid 视角能压过 combo 的估计概率 [0,1]（inference 公开口径，与
    ai._beat_risk_estimate 同型的启发式近似）。

    - 单张：池内含更高点的精确概率（pool_contains_prob）；
    - 对子：更高成对点的容斥上界（hypergeom_at_least 求和）；
    - 天王炸不可压；炸弹族未见更大同点 ≥4 张 → 0.9（更大头数/同花顺
      的细分 v1 不做）；其余复合牌型保守常数 0.5。
    """
    unseen = inference.unseen_counts(game.hands[pid], game.played_cards)
    total = sum(unseen.values())
    pool = game.hand_count(pid)
    if pool <= 0 or total <= 0:
        return 0.0
    level = game.level
    t = combo.type
    if t is HandType.SINGLE:
        u = sum(n for r, n in unseen.items()
                if point_value(r, level) > combo.rank)
        return inference.pool_contains_prob(u, pool, total)
    if t is HandType.PAIR:
        p = 0.0
        for r, n in unseen.items():
            if n >= 2 and point_value(r, level) > combo.rank:
                p += inference.hypergeom_at_least(n, pool, total, 2)
        return min(1.0, p)
    if t is HandType.ROCKET:
        return 0.0
    if combo.is_bomb_family():
        big = any(n >= 4 and point_value(r, level) > combo.rank
                  for r, n in unseen.items())
        return 0.9 if big else 0.0
    return 0.5


def accept_rate(game, pid: int) -> float:
    """pid 本局历史接牌率：非自己领出局面下的出牌机会中实际出牌比例；
    无机会记录返回 0.5（中性，不触发可送）。"""
    opps = takes = 0
    standing: Optional[int] = None
    for p, kind, _c, lead in getattr(game, "round_history", ()) or ():
        if kind == "play":
            if p == pid and standing is not None and p != standing and not lead:
                opps += 1
                takes += 1
            standing = p
        elif p == pid and standing is not None and p != standing:
            opps += 1
    return takes / opps if opps else 0.5


# ---------------------------------------------------------------- 三因子
def pressure_factor(game, pid: int, combo: Combo) -> float:
    """压制系数 = 1 + Σ_对手(手数增量 × 影响力)。

    增量 v1 简化式：本牌可压住对手时其需 拆组/动炸 应对，各记 +1 手 ×
    被压概率（概率档见 beat_prob）；天王炸不可压 → 增量 0。开局对手
    （估计 None）不参与。影响力 = 1/(估计手数^1.5)。
    推演式增量接口预留：步骤⑤以候选推演替代简化式（同签名替换）。
    """
    inc = 0.0
    for o in game.active_players():
        if o == pid or game.team_of(o) == game.team_of(pid):
            continue
        est = estimate_hands(game, o)
        if not est:
            continue
        p = beat_prob(game, o, combo)
        if p > 0.0:
            inc += p / (est ** 1.5)
    return 1.0 + inc


def partner_gain(game, pid: int, combo: Combo) -> float:
    """对家增益 = 1 + 对家手数减量 ÷ (对家估计手数 + 1)。

    减量 v1：对家可送（历史接牌率 > ACCEPT_RATE）且其能接住本牌时，
    记 接牌概率 ×1 手（对家接管领出，省一手开路）；对家开局（估计
    None）或已完赛 → 1.0。
    """
    mate = game.partner(pid)
    if mate in game.finish_order:
        return 1.0
    est = estimate_hands(game, mate)
    if not est:
        return 1.0
    if accept_rate(game, mate) <= ACCEPT_RATE:
        return 1.0
    return 1.0 + beat_prob(game, mate, combo) / (est + 1.0)


# ---------------------------------------------------------------- 终局乘数
ENDGAME_BLOCK = 3.0    # 对手剩 1 手且本牌可阻断
ENDGAME_BOMB = 2.0     # 对手 ≤2 手且本动作为炸弹族
ENDGAME_FINISH = 5.0   # 本动作一手出完
ENDGAME_FEED = 2.5     # 送对家冲刺
FEED_PROB = 0.5        # 送对家冲刺的最低接牌概率


def _single_hand_combo(hand: Sequence[Card], level: int) -> Optional[Combo]:
    """整手恰为一组牌时返回其牌型解释，否则 None。"""
    if len(decompose(hand, level)) != 1:
        return None
    interps = classify(hand, level)
    return interps[0] if interps else None


def endgame_multiplier(game, pid: int, combo: Combo) -> float:
    """终局乘数（四条件连乘，替代独立硬约束层；过牌不参与=1.0）。

    - 本动作一手出完 ×ENDGAME_FINISH；
    - 对手剩 1 手（整手恰一组）且该组压不住本牌（可阻断）×ENDGAME_BLOCK，
      与 ≤2 手用炸按分支互斥（同对手不重复计）；
    - 存在对手分解 ≤2 手且本动作为炸弹族 ×ENDGAME_BOMB；
    - 送对家冲刺：对家未完赛 + 残局（≤ENDGAME_CARDS 张且分解 ≤2 手）
      + 可送（接牌率）+ 对家能接住本牌（概率 ≥ FEED_PROB）×ENDGAME_FEED。
    """
    m = 1.0
    if combo.size == game.hand_count(pid):
        m *= ENDGAME_FINISH
    level = game.level
    for o in game.active_players():
        if o == pid or game.team_of(o) == game.team_of(pid):
            continue
        n_groups = len(decompose(game.hands[o], level))
        if n_groups == 1:
            their = _single_hand_combo(game.hands[o], level)
            if their is not None and not beats(their, combo):
                m *= ENDGAME_BLOCK
        elif n_groups <= 2 and combo.is_bomb_family():
            m *= ENDGAME_BOMB
    mate = game.partner(pid)
    if mate not in game.finish_order \
            and game.hand_count(mate) <= ENDGAME_CARDS \
            and len(decompose(game.hands[mate], level)) <= 2 \
            and accept_rate(game, mate) > ACCEPT_RATE \
            and beat_prob(game, mate, combo) >= FEED_PROB:
        m *= ENDGAME_FEED
    return m


# ---------------------------------------------------------------- 候选与决策
MAX_CANDIDATES = 12     # 候选上限（同型去重保最优后；跟牌另含过牌）
POWER_FLOOR = 0.001     # 零控制牌力下限（保证终局乘数可生效、并列可分序）

_TYPE_ORDER = {t: i for i, t in enumerate(HandType)}


def build_candidates(game, pid: int) -> List[Tuple[str, Optional[Combo]]]:
    """候选动作：同型 (type, rank) 去重保成本序首个（=最优），全部炸弹
    族必入，其余按成本序补足至上限 MAX_CANDIDATES；跟牌附过牌。"""
    prev = game.last_play[1] if game.last_play else None
    moves = generate_legal_moves(game.hands[pid], prev, game.level)
    bombs: List[Combo] = []
    others: List[Combo] = []
    seen = set()
    for m in moves:                      # generate_legal_moves 成本升序
        key = (m.type, m.rank)
        if key in seen:
            continue
        seen.add(key)
        (bombs if m.is_bomb_family() else others).append(m)
    plays = bombs + others[:max(0, MAX_CANDIDATES - len(bombs))]
    cands: List[Tuple[str, Optional[Combo]]] = [("play", m) for m in plays]
    if prev is not None:
        cands.append(("pass", None))
    return cands


def _tie_key(kind: str, m: Optional[Combo]) -> Tuple[int, int, tuple]:
    """平局决胜键（确定性，不用 rng）：过牌殿后；出牌按 牌型定义序 →
    比较点 → eid 序。"""
    if kind == "pass":
        return (len(_TYPE_ORDER), 0, ())
    return (_TYPE_ORDER[m.type], m.rank, m.eids)


def evaluate(game, pid: int,
             cands: List[Tuple[str, Optional[Combo]]]) -> List[dict]:
    """候选胜利值评估：胜利值 = 牌力 × 压制 × 对家增益 × 终局乘数。

    牌力取「出牌后」手牌；一手出完（rest 为空）时取当前整手牌力
    （整手价值兑现，否则空手牌力恒 0 会使终局乘数失效）；零控制手牌
    记 POWER_FLOOR 下限。返回条目 {kind, combo, eids, value,
    cur_power, power_detail, parts}（cur_power=决策时刻当前整手牌力
    （调试参照），power_detail 为 PowerDetail，步骤③候选记录数据源）。
    """
    level = game.level
    pc = _played_counts(game)               # 提升规则「出完」计数（出牌前）
    cur = power_of(game.hands[pid], level, pc)   # 当前整手牌力（出牌前）
    out: List[dict] = []
    for kind, m in cands:
        if kind == "pass":
            pw = cur
            pres = part = endm = 1.0
        else:
            rest = [c for c in game.hands[pid] if c.eid not in set(m.eids)]
            pc_m = dict(pc)
            for c in m.cards:               # 本动作牌计入已出（出牌后口径）
                pc_m[c.rank] = pc_m.get(c.rank, 0) + 1
            pw = power_of(rest if rest else game.hands[pid], level, pc_m)
            pres = pressure_factor(game, pid, m)
            part = partner_gain(game, pid, m)
            endm = endgame_multiplier(game, pid, m)
        base = pw.value if pw.value > 0.0 else \
            (POWER_FLOOR if game.hand_count(pid) else 0.0)
        value = base * pres * part * endm
        out.append({"kind": kind, "combo": m, "eids": m.eids if m else (),
                    "value": value, "cur_power": cur.value,
                    "power_detail": pw,
                    "parts": {"power": base, "pressure": pres,
                              "partner": part, "endgame": endm}})
    return out


def _power_dict(pd: PowerDetail) -> dict:
    """PowerDetail → 候选记录 power 键（JSON 安全，三级记录落盘前提）。"""
    return {"control": round(pd.control, 4),
            "control_detail": {k: round(v, 4)
                               for k, v in pd.control_detail.items()},
            "non_bomb_hands": round(pd.non_bomb_hands, 2),
            "non_bomb_groups": pd.non_bomb_groups,
            "weak_groups": pd.weak_groups,
            "value": round(pd.value, 6)}


def _combo_desc(m: Combo, level_rank: int) -> str:
    """候选动作描述（快照候选表首列，与手牌清单行同格式，2026-10-20
    用户定）：类型短名+点数串，逢人配按实际替代位（display_order+combo）。"""
    if m.type is HandType.ROCKET:
        return "天王炸"
    return _TYPE_SHORT[m.type] + _cards_str(
        display_order(m.cards, level_rank, m), level_rank)


def _entry(e: dict, selected: bool, level: int) -> dict:
    """评估条目 → 快照候选键位（与 ai._candidate_entry 同构）。

    score_total 为 value_total 别名（decision.build_decision_snapshot 的
    final.score_total 与候选汇总按 score_total 读取）；power 为牌力明细
    （PowerDetail 序列化）；cur_power 为当前整手牌力（出牌前，调试参照）；
    desc 为类型+点数串（候选表首列，与手牌清单行同格式）；四因子分项在
    score_parts，复盘面板列切换在步骤④按 sid 接入。
    """
    m = e["combo"]
    return {"kind": e["kind"], "type": m.type.value if m else None,
            "rank": m.rank if m else None, "eids": list(e["eids"]),
            "desc": _combo_desc(m, level) if m else "过牌",
            "value_total": e["value"], "score_total": e["value"],
            "score_parts": dict(e["parts"]),
            "cur_power": round(e["cur_power"], 6),
            "power": _power_dict(e["power_detail"]),
            "selected": selected}


def _group_ctx(g: Group, level: int,
               played: Optional[Dict[int, int]] = None) -> dict:
    """组清单行（快照面板左右两表共用数据源）：desc 类型+点数串（控制
    构成/手牌清单两表统一行格式）+ label 资源标签（旧显示）+ cards 点数串
    （10→X、逢人配→$）+ 控制值/火/配/弱。played 提供时控制值含提升。"""
    return {"label": g.label(),
            "cards": g.point_str(level),
            "desc": g.desc(level),
            "n": len(g.cards),
            "ctrl": round(_group_control(g, level, played), 4),
            "fire": g.is_fire,
            "wild_used": any(c.is_wild(level) for c in g.cards),
            "weak": (not g.is_fire) and g.is_weak(level)}


def context_of(game, pid: int) -> dict:
    """局面级参数（4 因子输入的基本数据，快照 context 键，快照面板左列
    数据源）：当前牌力/总控制/非炸手数 + 分解组清单（控制构成与手牌清单
    （出牌前）两表共用）+ 各对手估计手数 + 对家估计手数/可送。"""
    level = game.level
    pc = _played_counts(game)
    cur = power_of(game.hands[pid], level, pc)
    opp_ests = []
    for o in game.active_players():
        if o != pid and game.team_of(o) != game.team_of(pid):
            est = estimate_hands(game, o)
            if est:
                opp_ests.append(round(est, 2))
    mate = game.partner(pid)
    mate_est = estimate_hands(game, mate)
    return {"cur_power": round(cur.value, 4),
            "cur_control": round(cur.control, 4),
            "cur_hands": round(cur.non_bomb_hands, 2),
            "cur_groups": [_group_ctx(g, level, pc) for g in cur.groups],
            "opp_ests": opp_ests,
            "mate_est": round(mate_est, 2) if mate_est else None,
            "mate_accept": (accept_rate(game, mate) > ACCEPT_RATE)
                           if mate_est else None}


def _attach_after_context(ctx: dict, game, pid: int, best: dict) -> None:
    """ctx 增补「出牌后」组清单（快照面板手牌清单（出牌后）列数据源，
    2026-10-20 用户定）：实际所选动作出完后剩余手牌按最大牌力分解；
    过牌=出牌前同清单；一手出完 after_groups=[] 且 finished=True。"""
    m = best["combo"]
    level = game.level
    pc = _played_counts(game)
    if best["kind"] == "pass" or m is None:
        rest = list(game.hands[pid])
        pc_m = pc
    else:
        played = set(m.eids)
        rest = [c for c in game.hands[pid] if c.eid not in played]
        pc_m = dict(pc)
        for c in m.cards:                   # 本动作牌计入已出（出牌后口径）
            pc_m[c.rank] = pc_m.get(c.rank, 0) + 1
    if rest:
        pd = power_of(rest, level, pc_m)
        ctx["after_groups"] = [_group_ctx(g, level, pc_m) for g in pd.groups]
        ctx["after_power"] = round(pd.value, 4)
        ctx["after_control"] = round(pd.control, 4)
        ctx["after_hands"] = round(pd.non_bomb_hands, 2)
    else:
        ctx["after_groups"] = []
        ctx["after_power"] = 0.0
        ctx["after_control"] = 0.0
        ctx["after_hands"] = 0.0
    ctx["finished"] = not rest


def choose_detail(game, pid: int, profile=None,
                  **opts) -> Tuple[str, Optional[List[Card]], dict]:
    """夯级决策入口（registry 明细签名）：胜利值最高者出牌。

    detail 与 ai.choose_action_detail 同构（candidates/n_candidates/cost）：
    candidates 按胜利值降序（平局按 _tie_key）排列至多 MAX_CANDIDATES+过牌，
    每项 value_total=胜利值、score_parts=四因子分项、selected 唯一；
    context 为局面级参数（出牌前/出牌后手牌组清单见 _attach_after_context）；
    cost.search_depth/n_rollouts 恒 0（无搜索/推演，v1）。
    profile/opts 仅为签名兼容（v1 无档内参数）。
    """
    cands = build_candidates(game, pid)
    evs = evaluate(game, pid, cands)
    ranked = sorted(evs, key=lambda e: (-e["value"],
                                        _tie_key(e["kind"], e["combo"])))
    best = ranked[0]
    kind = best["kind"]
    cards = list(best["combo"].cards) if kind == "play" else None
    ctx = context_of(game, pid)
    _attach_after_context(ctx, game, pid, best)
    detail = {"candidates": [_entry(e, e is best, game.level)
                             for e in ranked],
              "n_candidates": len(cands),
              "cost": {"search_depth": 0, "n_rollouts": 0},
              "context": ctx}
    return kind, cards, detail


def choose(game, pid: int, profile=None,
           **opts) -> Tuple[str, Optional[List[Card]]]:
    """夯级决策入口（registry 基础签名）。"""
    kind, cards, _ = choose_detail(game, pid, profile, **opts)
    return kind, cards
