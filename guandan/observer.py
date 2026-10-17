"""教学观察者：基于「公开信息 + 玩家0手牌」口径的牌局数据展示。

教学体系模块一（阶段三）。信息口径（架构决策②）：
- 公开信息：本局已出牌（game.played_cards）、各家剩牌数、当前出牌展示、
  名次、级牌/队伍等局面字段；
- 玩家0手牌：人类玩家自己的手牌；
- 对手手牌只做统计推断（未见牌分布），绝不读取、绝不显示。

与 AI 体系互不引用：本模块只依赖 cards/rules/game 与 inference（无状态
纯函数，任务书五.2 双方各自合法输入调用），不 import ai/registry/decision/
plan_b/strategy。

PlayTracker 为已出牌数据库（listener 挂载，比赛起点 attach），
配合 PlayTracker.predict_rows 预留的剩牌推测接口构成面板数据层。
配额口径：点数 2..A 各 8 张，王各 2 张；
每花色 13 点 × 2 副 = 26 张；逢人配 = 红桃级牌（2 张，含于级牌配额）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from cards import BIG_JOKER, SMALL_JOKER, Card, RANK_LABELS, Suit

import inference

_SUIT_ORDER = (Suit.SPADE, Suit.HEART, Suit.CLUB, Suit.DIAMOND)
_SUIT_QUOTA = 26          # 每花色配额（13 点 × 2 副）


class Observer:
    """以玩家 ``pid``（默认人类 0）视角观察牌局，产出教学数据。"""

    def __init__(self, game, pid: int = 0) -> None:
        self.game = game
        self.pid = pid
        self.level = game.level
        self._counts()      # 预计算核心统计

    # ------------------------------------------------------------ 基础统计
    def _counts(self) -> None:
        """统计我手/已出，得到按点数与按花色的未见分布。"""
        mine = self.game.hands.get(self.pid, [])
        played = self.game.played_cards
        self.mine_by_rank: Dict[int, int] = {}
        self.mine_by_suit: Dict[Suit, int] = {}
        self.mine_cards: List[Card] = list(mine)
        for c in mine:
            self.mine_by_rank[c.rank] = self.mine_by_rank.get(c.rank, 0) + 1
            if not c.is_joker:
                self.mine_by_suit[c.suit] = self.mine_by_suit.get(c.suit, 0) + 1
        seen: Dict[int, int] = dict(self.mine_by_rank)
        for c in played:
            seen[c.rank] = seen.get(c.rank, 0) + 1
        totals = {**{r: 8 for r in range(2, 15)}, SMALL_JOKER: 2, BIG_JOKER: 2}
        self.unseen: Dict[int, int] = {r: totals[r] - seen.get(r, 0)
                                       for r in totals}
        # 花色：已出非王牌按花色归并；未见 = 配额 - 我手 - 已出
        played_by_suit: Dict[Suit, int] = {}
        for c in played:
            if not c.is_joker:
                played_by_suit[c.suit] = played_by_suit.get(c.suit, 0) + 1
        self.unseen_by_suit: Dict[Suit, int] = {
            s: _SUIT_QUOTA - self.mine_by_suit.get(s, 0) - played_by_suit.get(s, 0)
            for s in _SUIT_ORDER}

    # ------------------------------------------------------------ 查询接口
    def core_rows(self) -> List[Tuple[str, int, int, int]]:
        """核心牌行：(名称, 我持, 已出, 未见)。逢人配含于级牌。"""
        played = self.game.played_cards
        lv = self.level

        def stat(pred) -> Tuple[int, int]:
            return (sum(1 for c in self.mine_cards if pred(c)),
                    sum(1 for c in played if pred(c)))

        rows: List[Tuple[str, int, int, int]] = []
        for name, pred, quota in (
            ("大王", lambda c: c.rank == BIG_JOKER, 2),
            ("小王", lambda c: c.rank == SMALL_JOKER, 2),
            ("级牌" + RANK_LABELS[lv],
             lambda c: c.rank == lv and not c.is_wild(lv), 6),
            ("逢人配", lambda c: c.is_wild(lv), 2),
        ):
            m, o = stat(pred)
            rows.append((name, m, o, quota - m - o))
        return rows

    def my_voids(self) -> List[Suit]:
        """我手缺门花色（按黑红梅方顺序）。"""
        return [s for s in _SUIT_ORDER if self.mine_by_suit.get(s, 0) == 0]

    def core_residue(self) -> List[int]:
        """教学面板模块1 七列剩余（未见）张数：
        王(大小王合计) / 级(非红桃级牌) / 配(逢人配) / A / K / 10 / 5。"""
        rows = self.core_rows()
        return [self.unseen.get(SMALL_JOKER, 0) + self.unseen.get(BIG_JOKER, 0),
                rows[2][3], rows[3][3],
                self.unseen[14], self.unseen[13], self.unseen[10], self.unseen[5]]

    def bomb_candidates(self) -> List[int]:
        """未见张数 ≥4 的点数（可能仍在场上成炸），升序。"""
        return sorted(r for r, n in self.unseen.items() if n >= 4 and r < 15)


# ---------------------------------------------------------------- 已出牌数据库
# 牌型列（模块2 第3行标题顺序）：炸=炸弹+同花顺+天王炸（炸弹族）
COMBO_COLS = ["炸", "钢", "连", "顺", "32", "三", "对", "单"]
_COL_OF_TYPE = {
    "BOMB": 0, "STRAIGHT_FLUSH": 0, "ROCKET": 0,
    "TRIPLE_CHAIN": 1, "PAIR_CHAIN": 2, "STRAIGHT": 3,
    "TRIPLE_PAIR": 4, "TRIPLE": 5, "PAIR": 6, "SINGLE": 7,
}


class PlayTracker:
    """玩家已出牌数据库（教学体系，listener 挂载，仅公开信息）。

    - 已出：``play`` 事件 → played[pid][列] += 1（按 combo.type 归列）；
    - ``round_start`` 清空本局数据；attach 校验比赛起点（第1局且无已出牌）。
    """

    def __init__(self) -> None:
        self.reset()
        self._fn = None

    def reset(self) -> None:
        self.played: Dict[int, List[int]] = {p: [0] * len(COMBO_COLS)
                                             for p in range(4)}

    def attach(self, game) -> bool:
        """挂载到比赛起点；非起点（已有出牌/非第1局/已挂载）返回 False。"""
        if self._fn is not None or game.round_number != 1 or game.played_cards:
            return False
        self._fn = self._on_event
        game.listeners.append(self._fn)
        return True

    def detach(self, game) -> None:
        if self._fn is not None and self._fn in game.listeners:
            game.listeners.remove(self._fn)
        self._fn = None

    def _on_event(self, game, event: str, data: dict) -> None:
        if event == "round_start":
            self.reset()
        elif event == "play":
            col = _COL_OF_TYPE.get(data["combo"].type.name)
            if col is not None:
                self.played[data["pid"]][col] += 1

    def predict_rows(self, game=None, pid: int = 0):
        """模块3「剩牌推测」行（阶段四实装，inference 纯函数驱动）。

        ``game`` 缺省 None 时返回 None（保留预留接口语义，历史调用兼容）。
        返回 3 行 [{pid, label, text}]（对家/上家/下家），每行给出该玩家
        持炸弹概率最高点与可能牌型（剩牌 >10 张时无区分度，如实说明）。
        推断仅用公开口径数据（``pid`` 视角未见分布 + 各家剩牌数），
        「不接牌」不作无牌认定（B.4 信息约束）。好友联机时传入本端
        座位号，方位标签按 (对手座位 - 本座位) mod 4 换算。
        """
        if game is None:
            return None
        unseen = inference.unseen_counts(game.hands.get(pid, []),
                                         game.played_cards)
        total = sum(unseen.values())
        conf_cn = {"high": "高", "medium": "中", "low": "低"}
        pats = {d["pid"]: d for d in inference.pattern_prediction(game, pid)}
        rows = []
        for pos, label in ((2, "对家"), (3, "上家"), (1, "下家")):
            other = (pid + pos) % 4
            if other in game.finish_order or game.hand_count(other) <= 0:
                rows.append({"pid": other, "label": label, "text": "已出完"})
                continue
            n = game.hand_count(other)
            parts = []
            best = None
            for r, u in unseen.items():
                if u < 4 or r >= 15:
                    continue
                p = inference.hypergeom_at_least(u, n, total, 4)
                if best is None or p > best[1]:
                    best = (r, p)
            if best is not None and best[1] >= 0.05:
                parts.append(f"炸{RANK_LABELS.get(best[0], best[0])}"
                             f" {best[1] * 100:.0f}%·"
                             f"{conf_cn[inference.confidence_of(best[1])]}")
            pat = pats.get(other)
            if pat is not None:
                parts.append(f"{pat['pattern']} {pat['prob'] * 100:.0f}%")
            if not parts:
                parts.append("剩牌较多，暂难推测" if n > inference._PATTERN_MAX_HAND
                             else "暂无显著推测")
            rows.append({"pid": other, "label": label, "text": " ".join(parts)})
        return rows


# ---------------------------------------------------------------- 培训快照
def _public_view(game) -> dict:
    """公开信息视图（培训快照 B.2 public 字段）。

    注意：与 decision.py 的 public 序列化**格式相似但生成路径独立**
    （任务书 B.4.3）——本模块不 import decision/ai，仅读 game 公开字段。
    """
    lp = None
    if game.last_play is not None:
        owner, combo = game.last_play
        lp = {"owner": owner, "type": combo.type.value, "rank": combo.rank,
              "size": len(combo.cards), "eids": sorted(c.eid for c in combo.cards)}
    return {
        "played_eids": [c.eid for c in game.played_cards],
        "hand_counts": {str(p): game.hand_count(p) for p in range(4)},
        "last_play": lp,
        "finish_order": list(game.finish_order),
        "levels": {str(k): v for k, v in game.levels.items()},
        "playing_team": game.playing_team,
    }


def build_training_snapshot(game, pid: int = 0, seed: int = 0, trick: int = 0,
                            advice: Optional[dict] = None) -> Dict[str, Any]:
    """构建受训玩家视角培训快照（任务书附录 B.2，阶段二骨架）。

    口径：纯公开信息 + 该玩家手牌，由 Observer 现算现显（无状态）；
    严禁引用 ai/registry/decision/plan_b/strategy（tests/test_decision.py 以
    源码 import 检查 + 字段白名单固化）。``advice`` 由调用方以教学面板数据源
    （advisor.advise 结果）传入，本模块不依赖 advisor。

    bomb_prediction / pattern_prediction 阶段四实装：调 inference 无状态
    纯函数（双方各自以自己合法输入调用，杜绝状态共享）。概率/置信等级
    含 evidence 可解释口径。seed/trick 由调用方提供（snapshot_id 定位键，
    未知时为 0）。
    """
    obs = Observer(game, pid)
    rows = obs.core_rows()
    snap: Dict[str, Any] = {
        "data_version": 1,
        "snapshot_id": f"seed={seed}|r={game.round_number}|t={trick}",
        "seed": seed, "round": game.round_number, "trick": trick, "pid": pid,
        "my_hand": sorted(c.eid for c in game.hands.get(pid, [])),
        "public": _public_view(game),
        "memory": {
            "unseen": {str(r): n for r, n in sorted(obs.unseen.items()) if n > 0},
            "core": {"big_joker": rows[0][3], "small_joker": rows[1][3],
                     "level": rows[2][3], "wild": rows[3][3]},
        },
        "bomb_prediction": inference.bomb_prediction(game, pid),
        "pattern_prediction": inference.pattern_prediction(game, pid),
        "threats": [],
        "advice": advice,
        "uncertainty": f"未见牌 {sum(obs.unseen.values())} 张，以下推断置信度有限",
    }
    # 威胁提示（公开信息：对手剩牌数）
    for p in game.active_players():
        if game.team_of(p) == game.team_of(pid):
            continue
        n = game.hand_count(p)
        if n <= 2:
            snap["threats"].append({"pid": p, "desc": f"剩{n}张，报牌状态"})
        elif n <= 6:
            snap["threats"].append({"pid": p, "desc": f"剩{n}张，临近出完"})
    return snap
