"""理牌分组、布局、选牌及同花顺候选轮换。

四个独立状态（严格隔离，互不污染真实手牌）：
1. 真实手牌 real_hand —— 唯一持牌数据源，来自 game.hands[pid]，本模块绝不复制/删除。
2. 理牌分组 groups —— 仅管理展示分组（eid 引用），不改变真实持牌数量。
3. 当前选中牌 selected —— 待出牌组合的临时选择（eid 引用）。
4. 已提交出牌 submitted —— 最近一次已打出的牌（仅展示，出牌后由 sync 清理）。

同花顺候选：后台维护当前手牌全部合法候选，按花色分组，点击实心花色图标循环切换，
切换时仅保留当前候选为待出牌组合（selected），最终合法性仍由规则引擎验证。

本模块 UI 无关，可独立测试。
"""
from __future__ import annotations

from typing import Dict, List, Optional, Set, Tuple

from cards import Card, Suit, point_value, sort_key
from rules import (Combo, can_play, classify,
                   find_all_straight_flush_candidates, find_straight_flushes)


class HandManager:
    def __init__(self, game, pid: int = 0) -> None:
        self.game = game
        self.pid = pid
        self.groups: List[List[int]] = []          # 理牌分组：每组 eid 列表
        self.group_meta: List[dict] = []           # 与 groups 对齐：{"bomb","origin","tier","rank"}
        self.selected: Set[int] = set()            # 当前选中（待出牌）
        self.submitted: List[int] = []             # 已提交出牌（展示）
        self._normal_order: List[int] = []         # 正常手牌区展示顺序
        self.sf_candidates: Dict[Suit, List[Combo]] = {}
        self._sf_index: Dict[Suit, int] = {}
        self.sync()

    # ------------------------------------------------------------ 真实手牌
    @property
    def real_hand(self) -> List[Card]:
        return self.game.hands[self.pid]

    @property
    def level(self) -> int:
        return self.game.level

    def card_by_eid(self, eid: int) -> Optional[Card]:
        for c in self.real_hand:
            if c.eid == eid:
                return c
        return None

    # ------------------------------------------------------------ 状态同步
    def sync(self) -> None:
        """真实手牌变化后清理各展示状态，保证只引用现存实体牌、无重复。"""
        real = {c.eid for c in self.real_hand}
        seen: Set[int] = set()
        kept_groups: List[List[int]] = []
        kept_meta: List[dict] = []
        for gi, g in enumerate(self.groups):
            ng = [e for e in g if e in real and e not in seen]
            seen.update(ng)
            if ng:
                kept_groups.append(ng)
                kept_meta.append(self.group_meta[gi] if gi < len(self.group_meta) else {})
        self.groups = kept_groups
        self.group_meta = kept_meta
        self.selected = {e for e in self.selected if e in real}
        self.submitted = [e for e in self.submitted if e in real]
        grouped = self._grouped_eids()
        # 去重并保序
        norm: List[int] = []
        nset: Set[int] = set()
        for e in self._normal_order:
            if e in real and e not in grouped and e not in nset:
                norm.append(e)
                nset.add(e)
        # 补齐新出现的实体牌（如还贡收到）
        for c in self.real_hand:
            if c.eid not in grouped and c.eid not in nset:
                norm.append(c.eid)
                nset.add(c.eid)
        self._normal_order = norm
        self.refresh_sf_candidates()

    def _grouped_eids(self) -> Set[int]:
        out: Set[int] = set()
        for g in self.groups:
            out.update(g)
        return out

    # ------------------------------------------------------------ 展示集合
    def normal_eids(self) -> List[int]:
        """正常手牌区（未分组）的展示顺序。"""
        grouped = self._grouped_eids()
        order = [e for e in self._normal_order if e not in grouped and self.card_by_eid(e)]
        missing = [c.eid for c in self.real_hand
                   if c.eid not in grouped and c.eid not in order]
        order.extend(missing)
        return order

    def sort_normal(self) -> None:
        self._normal_order = sorted(
            self.normal_eids(),
            key=lambda e: sort_key(self.card_by_eid(e), self.level))

    # ------------------------------------------------------------ 选牌
    def toggle_select(self, eid: int) -> None:
        if not self.card_by_eid(eid):
            return
        if eid in self.selected:
            self.selected.discard(eid)
        else:
            self.selected.add(eid)

    def set_selected(self, eid: int, val: bool) -> None:
        """滑动选牌：直接把某张牌置为选中/取消。"""
        if not self.card_by_eid(eid):
            return
        if val:
            self.selected.add(eid)
        else:
            self.selected.discard(eid)

    def select(self, eids: List[int]) -> None:
        self.selected = {e for e in eids if self.card_by_eid(e)}

    def clear_selection(self) -> None:
        self.selected = set()

    def selected_cards(self) -> List[Card]:
        return [self.card_by_eid(e) for e in sorted(self.selected) if self.card_by_eid(e)]

    # ------------------------------------------------------------ 理牌分组
    def _origin_index(self, eids: List[int]) -> int:
        """记录这批牌在正常区中的最小位置，供恢复时放回原位。"""
        order = self._normal_order
        pos = [i for i, e in enumerate(order) if e in set(eids)]
        return min(pos) if pos else len(order)

    def add_group(self, eids: Optional[List[int]] = None) -> int:
        """将选中（或指定）牌从正常区移入新牌组。"""
        src = eids if eids is not None else sorted(self.selected)
        grouped = self._grouped_eids()
        new = [e for e in src if self.card_by_eid(e) and e not in grouped]
        if not new:
            return -1
        origin = self._origin_index(new)
        self.groups.append(new)
        self.group_meta.append({"bomb": False, "origin": origin, "tier": -1, "rank": 0})
        self.selected -= set(new)
        self._normal_order = [e for e in self._normal_order if e not in set(new)]
        self.refresh_sf_candidates()
        return len(self.groups) - 1

    def move_selected_to_group(self, gi: int) -> None:
        if not (0 <= gi < len(self.groups)):
            return
        grouped = self._grouped_eids()
        movable = [e for e in sorted(self.selected)
                   if self.card_by_eid(e) and e not in grouped]
        self.groups[gi].extend(movable)
        self.selected -= set(movable)
        self._normal_order = [e for e in self._normal_order if e not in movable]
        self.refresh_sf_candidates()

    def restore_groups(self, group_idx: Optional[List[int]] = None) -> None:
        """把整理好的牌组恢复到正常手牌区（无参数=全部恢复），并放回整理前位置。"""
        picks = list(range(len(self.groups))) if group_idx is None \
            else [i for i in group_idx if 0 <= i < len(self.groups)]
        if not picks:
            return
        items: List[Tuple[int, List[int]]] = []
        for i in sorted(picks, reverse=True):
            meta = self.group_meta[i] if i < len(self.group_meta) else {}
            items.append((meta.get("origin", len(self._normal_order)), self.groups[i]))
            del self.groups[i]
            if i < len(self.group_meta):
                del self.group_meta[i]
        for origin, eids in sorted(items, key=lambda t: t[0]):
            pos = min(origin, len(self._normal_order))
            for k, e in enumerate(eids):
                if e not in self._normal_order:
                    self._normal_order.insert(pos + k, e)
        self.refresh_sf_candidates()

    def restore_group(self, gi: int) -> None:
        """将某牌组的牌恢复至正常手牌区。"""
        self.restore_groups([gi])

    def clear_groups(self) -> None:
        """清空所有牌组，全部恢复至正常手牌区。"""
        self.restore_groups()

    def group_cards(self, gi: int) -> List[Card]:
        if not (0 <= gi < len(self.groups)):
            return []
        cards = [self.card_by_eid(e) for e in self.groups[gi] if self.card_by_eid(e)]
        cards.sort(key=lambda c: sort_key(c, self.level))
        return cards

    def arrange_selected(self) -> Tuple[str, str]:
        """整理选中的牌：能构成一首牌才成组；炸弹族放左侧（按炸弹大小），
        非炸弹放右侧。未选牌返回 ("empty","")，不能成牌返回 ("invalid","")。"""
        if not self.selected:
            return "empty", ""
        cards = self.selected_cards()
        cands = classify(cards, self.level)
        if not cands:
            return "invalid", ""
        # 多种解释时优先炸弹族（同花顺等放左侧），同级取更大点数
        combo = max(cands, key=lambda c: (c.tier(), c.rank))
        tier = combo.tier()
        bomb = tier >= 0
        eids = sorted(self.selected)
        origin = self._origin_index(eids)
        new = sorted(eids, key=lambda e: sort_key(self.card_by_eid(e), self.level))
        self.groups.append(new)
        self.group_meta.append({"bomb": bomb, "origin": origin,
                                "tier": tier, "rank": combo.rank})
        self.selected -= set(new)
        self._normal_order = [e for e in self._normal_order if e not in set(new)]
        self.refresh_sf_candidates()
        return "ok", ("bomb" if bomb else "combo")

    def arranged_layout(self) -> Tuple[List[Tuple[int, List[int]]], List[Tuple[int, List[int]]]]:
        """手牌区分区展示：(炸弹组列表, 非炸弹组列表)，炸弹按大小从小到大排左侧。"""
        bombs: List[Tuple[int, int, int, List[int]]] = []
        combos: List[Tuple[int, List[int]]] = []
        for gi, g in enumerate(self.groups):
            meta = self.group_meta[gi] if gi < len(self.group_meta) else {}
            if meta.get("bomb"):
                bombs.append((meta.get("tier", 0), meta.get("rank", 0), gi, list(g)))
            else:
                combos.append((gi, list(g)))
        bombs.sort(key=lambda t: (t[0], t[1]))
        return ([(gi, eids) for _, _, gi, eids in bombs], combos)

    def auto_arrange(self) -> None:
        """整理手牌：清空手动分组后，按预设规则自动识别炸弹/同花顺成组，其余排序。"""
        self.clear_groups()
        remaining = {c.eid for c in self.real_hand}
        level = self.level
        # 1) 同花顺（每花色取顶牌最大且不冲突者）
        for combo in sorted(find_straight_flushes(self.real_hand, level),
                            key=lambda c: (-c.rank, c.suit is Suit.HEART)):
            if all(e in remaining for e in (x.eid for x in combo.cards)):
                eids = [x.eid for x in combo.cards]
                self.groups.append(eids)
                self.group_meta.append({"bomb": True, "origin": self._origin_index(eids),
                                        "tier": combo.tier(), "rank": combo.rank})
                remaining -= set(eids)
        # 2) 炸弹（同点 >=4，不含王）
        by_rank: Dict[int, List[int]] = {}
        for c in self.real_hand:
            if c.eid in remaining and not c.is_joker:
                by_rank.setdefault(c.rank, []).append(c.eid)
        for r, eids in by_rank.items():
            if len(eids) >= 4:
                eids = sorted(eids)
                self.groups.append(eids)
                combo = can_play([self.card_by_eid(e) for e in eids], None, level)
                self.group_meta.append({"bomb": True, "origin": self._origin_index(eids),
                                        "tier": combo.tier() if combo else -1,
                                        "rank": combo.rank if combo else 0})
                remaining -= set(eids)
        # 3) 其余进入正常区并排序
        self._normal_order = sorted(remaining, key=lambda e: sort_key(self.card_by_eid(e), level))
        self.refresh_sf_candidates()

    # ------------------------------------------------------------ 同花顺候选
    def refresh_sf_candidates(self) -> None:
        self.sf_candidates = find_all_straight_flush_candidates(self.real_hand, self.level)
        for suit in list(self._sf_index):
            if suit not in self.sf_candidates:
                del self._sf_index[suit]

    def has_sf(self, suit: Suit) -> bool:
        return bool(self.sf_candidates.get(suit))

    def sf_count(self, suit: Suit) -> int:
        return len(self.sf_candidates.get(suit, []))

    def cycle_sf(self, suit: Suit) -> Optional[Combo]:
        """点击花色图标：循环切换到下一个候选，并设为当前待出牌组合。

        切换时取消上一组合的选中，仅保留当前候选。
        """
        cands = self.sf_candidates.get(suit)
        if not cands:
            return None
        idx = self._sf_index.get(suit, -1)
        idx = (idx + 1) % len(cands)
        self._sf_index[suit] = idx
        combo = cands[idx]
        self.clear_selection()
        self.select([c.eid for c in combo.cards])
        return combo

    # ------------------------------------------------------------ 提交出牌
    def commit_play(self, combo: Combo) -> None:
        """出牌成功后记录已提交展示；真实手牌由 game 已更新，随后 sync 清理。"""
        self.submitted = [c.eid for c in combo.cards]
        self.clear_selection()
        self.sync()

    # ------------------------------------------------------------ 布局数据
    def row_layout(self) -> List[int]:
        """手牌一行模式：正常区水平顺序。"""
        return self.normal_eids()

    def column_layout(self) -> List[List[int]]:
        """手牌竖排模式：按点数分列，相同点数上下排列（从下到上黑桃/红桃/
        梅花/方块，缺位由后续花色替补），不同点数从左到右（点值降序）。"""
        cols: Dict[int, List[int]] = {}
        for e in self.normal_eids():
            c = self.card_by_eid(e)
            cols.setdefault(c.rank, []).append(e)
        ordered_ranks = sorted(cols.keys(), key=lambda r: -point_value(r, self.level))
        return [sorted(cols[r], key=lambda e: (int(self.card_by_eid(e).suit), e))
                for r in ordered_ranks]

    def invariant_ok(self) -> bool:
        """校验：分组无重复、均为真实手牌子集；选中为真实手牌子集；元数据对齐。"""
        if len(self.group_meta) != len(self.groups):
            return False
        real = {c.eid for c in self.real_hand}
        seen: Set[int] = set()
        for g in self.groups:
            for e in g:
                if e in seen or e not in real:
                    return False
                seen.add(e)
        return self.selected <= real
