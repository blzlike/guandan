"""牌力测算中心（阶段二/五）：A/B 基准 + 策略天梯 + 固定 seed 段对照报告。

由 bench_ai.py 升级为模块，命令行用法保持兼容。标准训练/测试流程
（详见项目任务书附录 E「AI 牌力训练与测试接口」）：

    python bench.py --sids                      # 查看注册策略 sid 清单
    python bench.py --games 300 --a novice --b advanced   # 三档 sid 对战
    python bench.py --games 300 --a "enh+mem_lead" --b "enh"
                                                # 细粒度 token 调参（A/B 新权重）
    python bench.py --ladder --games 300        # registry 全对全循环赛，写入天梯
    python bench.py --list                      # 查看天梯（Elo 排名 + 对战聚合）
    python bench.py --games 60 --report         # 固定 seed 段对照，写 JSON 报告
    python bench.py --games 60 --report --compare 旧报告.json
                                                # 新旧策略同牌对比出报告

配置双通道（--a/--b）：
- 注册策略 sid（推荐）：registry.ids()，当前 classic_low / classic_high /
  pimc / novice / intermediate / advanced；设置界面三档 = 后三者，
  classic 与 pimc 保留仅供 bench/测试调用；
- 细粒度 token（A/B 调参）：``basic`` | ``enh``，可叠加 ``+mem_lead``
  ``+urgent_top`` ``+feed``（见 ai.py 模块头）。调参改 Weights/Budget 后
  先经 token 通道验证，再注册为新 sid 固化。

方法（沿用历史口径）：同 seed 同牌，0/2 队配置 A vs 1/3 队配置 B；
统计单元 = 单局头游队伍 + 整场比赛胜方；Wilson 95% CI。

天梯 ladder.json（与 bench.py 同目录，聚合 + Elo）：按配置标签聚合
（场次/总局数/轮胜/整场胜），Elo 按整场胜负整批更新（K=32，初始
1500），另存最近 50 条历史。A/B 模式默认不落盘（调参实验），--save
显式写入；--ladder 模式每对跑完即落盘。

固定 seed 段对照报告（阶段五）：``--report`` 在同 seed 同牌口径上额外
落盘逐 seed 明细（bench_reports/*.json），``--compare 旧报告.json`` 将
新跑结果与旧报告逐 seed 对比（轮胜率/整场胜/同 seed 结果一致率）——
人工校核闭环中策略调整前后的可复现对照依据。

阶段六统计口径扩展（--report/--compare）：头游率/双上率/双下率（协同
效果=双上率-双下率+场均游次奖励）、炸弹收益（用炸局胜率 A/B 对照）、
决策耗时（ms/手，仅计行牌决策）。旧报告缺扩展键时 --compare 自动跳过
扩展段，兼容阶段五报告。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import time
from concurrent.futures import ProcessPoolExecutor

import ai
import registry
from game import GuandanGame

KNOWN_FEATURES = set(ai.ALL_FEATURES)
MAX_ACTIONS = 30000

_HERE = os.path.dirname(os.path.abspath(__file__))
LADDER_PATH = os.path.join(_HERE, "ladder.json")
LADDER_VERSION = 1
LADDER_HISTORY = 50
ELO_K = 32.0
ELO_BASE = 1500.0
REPORTS_DIR = os.path.join(_HERE, "bench_reports")
REPORT_VERSION = 1


# ---------------------------------------------------------------- 配置通道
def parse_cfg(text: str) -> tuple:
    """解析配置 -> ("sid", sid) | ("token", (enhanced, feats frozenset))。"""
    text = (text or "").strip()
    if registry.get(text) is not None:
        return ("sid", text)
    parts = [p for p in text.lower().split("+") if p]
    base = parts[0] if parts else ""
    if base not in ("basic", "enh"):
        raise SystemExit(f"非法配置: {text!r}（应为 basic/enh[+特性] 或注册 sid "
                         f"{registry.ids()}）")
    feats = frozenset(parts[1:])
    unknown = feats - KNOWN_FEATURES
    if unknown:
        raise SystemExit(f"未知特性: {unknown}（可用: {sorted(KNOWN_FEATURES)}）")
    return ("token", (base == "enh", feats))


def label_of(cfg: tuple) -> str:
    kind, v = cfg
    if kind == "sid":
        return v
    base = "enh" if v[0] else "basic"
    return base + "".join("+" + f for f in sorted(v[1]))


# ---------------------------------------------------------------- 对局核心
def _step(g: GuandanGame, by_pid: dict) -> tuple:
    """驱动一步（token/sid 统一分发；tribute 阶段与 ai.auto_step 同逻辑）。

    返回 ``(side, elapsed)``：side=决策方配置侧（0=A 队 / 1=B 队），
    仅计行牌/过牌决策（进贡/还贡与开局为共用逻辑不计）；非决策步返回
    ``(None, 0.0)``。elapsed 为该次 AI 决策耗时（秒）。
    """
    if g.phase == "match_over":
        return None, 0.0
    if g.phase == "round_over":
        g.start_next_round()
        return None, 0.0
    if g.phase == "tribute":
        pending = [(gv, r) for gv, r in g.tribute_pairs if gv not in g.tribute_done]
        if pending:
            giver, _ = pending[0]
            g.submit_tribute(giver, ai.choose_tribute(g, giver))
        else:
            receiver = [r for r in g.tribute_done.values()
                        if r not in g.return_done][0]
            g.submit_return(receiver, ai.choose_return(g, receiver))
        return None, 0.0
    pid = g.turn
    kind, v = by_pid[pid]
    t0 = time.perf_counter()
    if kind == "sid":
        act, cards = registry.choose(g, pid, ai.AIProfile(), v)
    else:
        act, cards = ai.choose_action(g, pid, ai.AIProfile(),
                                      enhanced=v[0], features=v[1])
    elapsed = time.perf_counter() - t0
    if act == "play":
        g.play(pid, cards)
    else:
        g.pass_play(pid)
    return (0 if pid % 2 == 0 else 1), elapsed


_ROUND_REWARD = (3, 1, -1, -3)     # 头游/二游/三游/末游（plan_b._REWARD 同口径）


def _round_stats(g: GuandanGame) -> dict:
    """局末统计（round_result + round_history → A 队视角六口径原始量）。"""
    rr = g.round_result
    fo = rr["finish_order"]
    dd = bool(rr.get("double_down"))
    # 协同效果：游次奖励（头游+3 二游+1 三游-1 末游-3，双下翻倍）
    reward_a = 0.0
    for i, p in enumerate(fo):
        r = _ROUND_REWARD[i] * (2 if dd else 1)
        if p % 2 == 0:
            reward_a += r
    # 炸弹收益：本局各家炸弹族出牌（炸弹/同花顺/天王炸，与 AI 口径一致）
    bombs = [0, 0]
    used = [False, False]
    for pid, kind, combo, _lead in g.round_history:
        if kind == "play" and combo is not None and combo.is_bomb_family():
            side = 0 if pid % 2 == 0 else 1
            bombs[side] += 1
            used[side] = True
    win = rr["winner_team"]
    return {"double_up": 1 if (win == 0 and dd) else 0,
            "double_down": 1 if (win == 1 and dd) else 0,
            "reward_a": reward_a,
            "bombs_a": bombs[0], "bombs_b": bombs[1],
            "bomb_rounds_a": 1 if used[0] else 0,
            "bomb_round_wins_a": 1 if (used[0] and win == 0) else 0,
            "bomb_rounds_b": 1 if used[1] else 0,
            "bomb_round_wins_b": 1 if (used[1] and win == 1) else 0}


def play_match(seed: int, cfg_a: tuple, cfg_b: tuple) -> dict:
    """打一场比赛，返回逐局与累计统计（阶段六扩展口径）。

    键位：``rounds``（各局头游队伍序列）、``match_winner``、``double_up``/
    ``double_down``（A 队双上/双下局数）、``reward_a``（A 队游次奖励累计）、
    ``bombs_a/bombs_b``（炸弹族出牌总数）、``bomb_rounds_*``/``bomb_round_wins_*``
    （用炸局数/用炸且该队赢局数）、``ms_a/n_a`` 与 ``ms_b/n_b``（行牌决策
    累计耗时毫秒与次数）。
    """
    g = GuandanGame(random.Random(seed))
    by_pid = {p: cfg_a if p % 2 == 0 else cfg_b for p in range(4)}
    rounds: list = []
    agg = {"double_up": 0, "double_down": 0, "reward_a": 0.0,
           "bombs_a": 0, "bombs_b": 0,
           "bomb_rounds_a": 0, "bomb_round_wins_a": 0,
           "bomb_rounds_b": 0, "bomb_round_wins_b": 0}
    ms = [0.0, 0.0]
    n_dec = [0, 0]
    actions = 0

    def collect() -> None:
        rounds.append(g.round_result["winner_team"])
        rs = _round_stats(g)
        for k, v in rs.items():
            agg[k] += v

    while g.phase != "match_over":
        side, dt = _step(g, by_pid)
        if side is not None:
            ms[side] += dt
            n_dec[side] += 1
        actions += 1
        if actions >= MAX_ACTIONS:
            raise RuntimeError(f"seed={seed} 模拟超限，疑似死循环")
        if g.phase == "round_over" and g.round_result:
            collect()
    if g.round_result:                # 决胜局（phase 直达 match_over）不入上循环
        collect()
    out = dict(agg, rounds=rounds, match_winner=g.match_winner,
               ms_a=round(ms[0] * 1000.0, 3), n_a=n_dec[0],
               ms_b=round(ms[1] * 1000.0, 3), n_b=n_dec[1])
    return out


def _worker(job: tuple):
    """子进程入口：单 seed 对局，异常以哨兵返回避免整批中断。"""
    seed, cfg_a, cfg_b = job
    try:
        return play_match(seed, cfg_a, cfg_b)
    except Exception as e:  # noqa: BLE001
        return ("__error__", seed, repr(e))


def wilson(k: int, n: int, z: float = 1.96) -> tuple:
    """Wilson 95% 置信区间，返回 (p, low, high)。"""
    if n == 0:
        return 0.0, 0.0, 0.0
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / denom
    return p, centre - half, centre + half


def run_games(games: int, cfg_a: tuple, cfg_b: tuple, workers: int,
              seed0: int) -> tuple:
    """并行跑 games 场，返回 (总局数, A 队轮胜数, 异常数, A 队整场胜数)。"""
    jobs = [(seed0 + i, cfg_a, cfg_b) for i in range(games)]
    k = n = err = matches_won = 0
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for i, res in enumerate(ex.map(_worker, jobs, chunksize=1), 1):
            if isinstance(res, tuple) and res and res[0] == "__error__":
                err += 1
                print(f"  [异常] seed={res[1]}: {res[2]}")
                continue
            n += len(res["rounds"])
            k += sum(1 for t in res["rounds"] if t == 0)
            matches_won += 1 if res["match_winner"] == 0 else 0
            if i % 25 == 0 or i == games:
                print(f"  进度 {i}/{games}  A 队轮胜 {k}/{n} "
                      f"({k / max(n, 1) * 100:.1f}%)")
    return n, k, err, matches_won


# ---------------------------------------------------------------- 固定 seed 段对照报告（阶段五/六）
# 阶段六统计口径扩展键（summary/seeds 追加；旧报告缺键按 0 处理）
_REPORT_KEYS = ("double_up", "double_down", "reward_a",
                "bombs_a", "bombs_b", "bomb_rounds_a", "bomb_round_wins_a",
                "bomb_rounds_b", "bomb_round_wins_b")


def run_games_report(games: int, cfg_a: tuple, cfg_b: tuple, workers: int,
                     seed0: int) -> dict:
    """固定 seed 段对照：逐 seed 明细 + 聚合（``--report`` 数据源）。

    与 run_games 同口径（同 seed 同牌，0/2=A 配置 vs 1/3=B 配置），另记录
    每 seed 的局胜序列/整场胜方/六口径明细，供新旧策略同牌对比。阶段六
    summary 追加：头游率/双上率/双下率/游次奖励/炸弹收益/决策耗时。
    """
    jobs = [(seed0 + i, cfg_a, cfg_b) for i in range(games)]
    t0 = time.time()
    seeds: list = []
    rounds = k = err = mw = 0
    agg = {key: 0 for key in _REPORT_KEYS}
    ms = [0.0, 0.0]
    n_dec = [0, 0]
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for i, res in enumerate(ex.map(_worker, jobs, chunksize=1)):
            seed = seed0 + i
            if isinstance(res, tuple) and res and res[0] == "__error__":
                err += 1
                seeds.append({"seed": seed, "error": res[2]})
                print(f"  [异常] seed={seed}: {res[2]}")
                continue
            rounds += len(res["rounds"])
            k += sum(1 for t in res["rounds"] if t == 0)
            mw += 1 if res["match_winner"] == 0 else 0
            for key in _REPORT_KEYS:
                agg[key] += res[key]
            ms[0] += res["ms_a"]
            ms[1] += res["ms_b"]
            n_dec[0] += res["n_a"]
            n_dec[1] += res["n_b"]
            seeds.append(dict(
                {"seed": seed, "rounds": list(res["rounds"]),
                 "match_winner": res["match_winner"],
                 "ms_a": res["ms_a"], "n_a": res["n_a"],
                 "ms_b": res["ms_b"], "n_b": res["n_b"]},
                **{key: res[key] for key in _REPORT_KEYS}))
    p, lo, hi = wilson(k, rounds)
    n_ok = games - err
    summary = {
        "rounds": rounds, "a_round_wins": k,
        "a_match_wins": mw, "errors": err,
        "a_round_win_rate": round(k / max(rounds, 1), 4),
        "a_match_win_rate": round(mw / max(n_ok, 1), 4),
        "wilson95": [round(lo, 4), round(hi, 4)],
        "elapsed_s": round(time.time() - t0, 1),
        # 阶段六扩展口径（A 队视角；rounds=局数、n_ok=场数）
        "a_head_rate": round(k / max(rounds, 1), 4),           # 头游率（=轮胜率）
        "a_double_up_rate": round(agg["double_up"] / max(rounds, 1), 4),
        "a_double_down_rate": round(agg["double_down"] / max(rounds, 1), 4),
        "a_reward_avg": round(agg["reward_a"] / max(rounds, 1), 4),
        "a_bombs": agg["bombs_a"], "b_bombs": agg["bombs_b"],
        "a_bomb_rounds": agg["bomb_rounds_a"],
        "a_bomb_round_wins": agg["bomb_round_wins_a"],
        "a_bomb_win_rate": round(agg["bomb_round_wins_a"]
                                 / max(agg["bomb_rounds_a"], 1), 4),
        "b_bomb_win_rate": round(agg["bomb_round_wins_b"]
                                 / max(agg["bomb_rounds_b"], 1), 4),
        "a_ms_avg": round(ms[0] / max(n_dec[0], 1), 3),
        "b_ms_avg": round(ms[1] / max(n_dec[1], 1), 3),
    }
    return {
        "version": REPORT_VERSION,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "a": label_of(cfg_a), "b": label_of(cfg_b),
        "seed0": seed0, "games": games,
        "summary": summary,
        "seeds": seeds,
    }


def save_report(d: dict, path: str) -> str:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(d, fh, ensure_ascii=False, indent=1)
    return path


def load_report(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        d = json.load(fh)
    if d.get("version") != REPORT_VERSION:
        raise ValueError(f"不支持的报告版本: {d.get('version')}")
    return d


def compare_reports(old: dict, new: dict) -> str:
    """新旧报告同牌对比 → 文本报告（``--compare`` 输出，纯函数）。

    seed0 与场数一致视为同牌对照；逐 seed 比对整场胜方一致率，聚合层
    对比 A 队轮胜率/整场胜率变化（pp = 百分点）。阶段六扩展：两报告均含
    扩展口径时追加 头游/双上/双下/炸弹/协同/耗时 对照（旧报告缺键则跳过）。
    """
    o, n = old["summary"], new["summary"]
    same_deal = (old.get("seed0") == new.get("seed0")
                 and old.get("games") == new.get("games"))
    lines = ["== 新旧策略同牌对照 ==",
             f"旧报告 {old.get('created_at')}  A={old.get('a')}  "
             f"B={old.get('b')}  seed {old.get('seed0')}.."
             f"{old.get('seed0', 0) + old.get('games', 0) - 1}"
             f"（{old.get('games')} 场）",
             f"新报告 {new.get('created_at')}  A={new.get('a')}  "
             f"B={new.get('b')}  seed {new.get('seed0')}.."
             f"{new.get('seed0', 0) + new.get('games', 0) - 1}"
             f"（{new.get('games')} 场）",
             "同牌对照：" + ("是（seed0 与场数一致）" if same_deal
                             else "否（seed 段不同，仅粗略参考）")]
    d_round = (n["a_round_win_rate"] - o["a_round_win_rate"]) * 100
    d_match = (n["a_match_win_rate"] - o["a_match_win_rate"]) * 100
    lines.append(f"A 队轮胜率：{o['a_round_win_rate'] * 100:.1f}% → "
                 f"{n['a_round_win_rate'] * 100:.1f}%"
                 f"（{d_round:+.2f}pp）")
    lines.append(f"A 队整场胜：{o['a_match_win_rate'] * 100:.1f}% → "
                 f"{n['a_match_win_rate'] * 100:.1f}%"
                 f"（{d_match:+.2f}pp）")
    om = {s["seed"]: s for s in old.get("seeds") or [] if "match_winner" in s}
    nm = {s["seed"]: s for s in new.get("seeds") or [] if "match_winner" in s}
    common = sorted(set(om) & set(nm))
    if common:
        agree = sum(1 for s in common if om[s]["match_winner"] == nm[s]["match_winner"])
        lines.append(f"同 seed 整场结果一致：{agree}/{len(common)}"
                     f"（{agree / len(common) * 100:.1f}%）")
    # 阶段六扩展口径（任一报告缺键则跳过，兼容阶段五旧报告）
    if all(ky in o and ky in n for ky in
           ("a_double_up_rate", "a_double_down_rate", "a_reward_avg",
            "a_ms_avg", "b_ms_avg", "a_bomb_win_rate", "b_bomb_win_rate")):
        lines.append("-- 扩展口径（阶段六）--")
        for lbl, ky in (("头游率", "a_head_rate"),
                        ("双上率", "a_double_up_rate"),
                        ("双下率", "a_double_down_rate")):
            if ky in o and ky in n:
                lines.append(f"A 队{lbl}：{o[ky] * 100:.1f}% → "
                             f"{n[ky] * 100:.1f}%"
                             f"（{(n[ky] - o[ky]) * 100:+.2f}pp）")
        lines.append(f"A 队游次奖励/局：{o['a_reward_avg']:.2f} → "
                     f"{n['a_reward_avg']:.2f}"
                     f"（{n['a_reward_avg'] - o['a_reward_avg']:+.2f}）")
        lines.append(f"A 队用炸局胜率：{o['a_bomb_win_rate'] * 100:.1f}% → "
                     f"{n['a_bomb_win_rate'] * 100:.1f}%"
                     f"（B 队 {o['b_bomb_win_rate'] * 100:.1f}% → "
                     f"{n['b_bomb_win_rate'] * 100:.1f}%）")
        lines.append(f"决策耗时 ms/手：A {o['a_ms_avg']:.1f} → {n['a_ms_avg']:.1f}"
                     f"，B {o['b_ms_avg']:.1f} → {n['b_ms_avg']:.1f}")
    trend = "上升" if d_round > 1e-9 else ("下降" if d_round < -1e-9 else "持平")
    lines.append(f"结论：A 配置调整后 A 队轮胜率{trend}")
    return "\n".join(lines)


# ---------------------------------------------------------------- 天梯
def load_ladder(path: str = LADDER_PATH) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        if d.get("version") == LADDER_VERSION:
            d.setdefault("elo", {})
            d.setdefault("pairs", {})
            d.setdefault("history", [])
            return d
    except (OSError, ValueError):
        pass
    return {"version": LADDER_VERSION, "elo": {}, "pairs": {}, "history": []}


def save_ladder(d: dict, path: str = LADDER_PATH) -> None:
    d["history"] = d.get("history", [])[-LADDER_HISTORY:]
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(d, fh, ensure_ascii=False, indent=1)


def record_result(d: dict, la: str, lb: str, games: int, rounds: int,
                  a_round_wins: int, a_match_wins: int, seed0: int) -> None:
    """聚合累计 + Elo 逐场离散更新（exp 随演化重算，净分守恒）。"""
    pair = d["pairs"].setdefault(f"{la}|{lb}",
                                 {"games": 0, "rounds": 0,
                                  "a_round_wins": 0, "a_match_wins": 0})
    pair["games"] += games
    pair["rounds"] += rounds
    pair["a_round_wins"] += a_round_wins
    pair["a_match_wins"] += a_match_wins
    ea = d["elo"].get(la, ELO_BASE)
    eb = d["elo"].get(lb, ELO_BASE)
    # 交错胜负序列（模拟自然对局次序），避免「先全胜后全负」的 Elo 过冲
    wins, losses = a_match_wins, games - a_match_wins
    while wins > 0 or losses > 0:
        if wins:
            wins -= 1
            exp = 1.0 / (1.0 + 10 ** ((eb - ea) / 400.0))
            delta = ELO_K * (1.0 - exp)
            ea += delta
            eb -= delta
        if losses:
            losses -= 1
            exp = 1.0 / (1.0 + 10 ** ((eb - ea) / 400.0))
            delta = ELO_K * (0.0 - exp)
            ea += delta
            eb -= delta
    d["elo"][la] = round(ea, 1)
    d["elo"][lb] = round(eb, 1)
    d["history"].append({"ts": time.strftime("%Y-%m-%d %H:%M"), "a": la, "b": lb,
                         "games": games, "seed0": seed0, "rounds": rounds,
                         "a_round_wins": a_round_wins,
                         "a_match_wins": a_match_wins})


def show_ladder(d: dict) -> None:
    if not d["elo"] and not d["pairs"]:
        print("天梯为空（尚无记录）")
        return
    print("== Elo 排名 ==")
    for sid, elo in sorted(d["elo"].items(), key=lambda kv: -kv[1]):
        print(f"  {sid:<28} {elo:7.1f}")
    print("== 对战聚合（左队视角）==")
    for key, v in d["pairs"].items():
        la, lb = key.split("|", 1)
        rp = v["a_round_wins"] / max(v["rounds"], 1) * 100
        mp = v["a_match_wins"] / max(v["games"], 1) * 100
        print(f"  {la} vs {lb}: {v['games']} 场 {v['rounds']} 局，"
              f"轮胜 {rp:.1f}%，整场胜 {mp:.1f}%")
    print("== 最近记录 ==")
    for h in d["history"][-10:]:
        print(f"  [{h['ts']}] {h['a']} vs {h['b']}  {h['games']} 场 seed0={h['seed0']}"
              f"  轮胜 {h['a_round_wins']}/{h['rounds']}"
              f"  整场 {h['a_match_wins']}/{h['games']}")


def run_ladder(games: int, workers: int, seed0: int) -> None:
    """registry 全对全循环赛（A=i 队 0/2 座，B=j 队 1/3 座），逐对落盘。"""
    sids = registry.ids()
    if len(sids) < 2:
        raise SystemExit("注册策略不足 2 个，无法循环赛")
    d = load_ladder()
    for i in range(len(sids)):
        for j in range(i + 1, len(sids)):
            la, lb = sids[i], sids[j]
            print(f"== {la} (0/2) vs {lb} (1/3) ==")
            n, k, err, mw = run_games(games, ("sid", la), ("sid", lb),
                                      workers, seed0)
            record_result(d, la, lb, games - err, n, k, mw, seed0)
            save_ladder(d)
            seed0 += games          # 各对用不重叠 seed 段
    show_ladder(d)


def show_sids() -> None:
    """打印注册策略清单（--sids）：--a/--b 可用的 sid 及其说明。"""
    print("== 注册策略 sid（--a/--b 可用）==")
    for sid in registry.ids():
        spec = registry.get(sid)
        tier = "（设置界面档位）" if sid in ("novice", "intermediate",
                                            "advanced") else "（仅 bench/测试）"
        print(f"  {sid:<14} {spec.name:<6} {tier}")
        print(f"  {'':<14} {spec.desc}")
    print("== 细粒度 token（调参通道）==")
    print("  basic | enh[+mem_lead][+urgent_top][+feed]"
          "（enh=记牌器+残局搜索基座）")


# ---------------------------------------------------------------- CLI
def main() -> None:
    ap = argparse.ArgumentParser(
        prog="bench",
        description="牌力测算中心：A/B 基准（sid 或 token）+ 策略天梯"
                    "+ 固定 seed 段对照报告（标准流程见任务书附录 E）")
    ap.add_argument("--games", type=int, default=200,
                    help="比赛场数（每场约 6-15 局；校准建议 ≥300）")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 4) - 1))
    ap.add_argument("--seed0", type=int, default=1000,
                    help="起始 seed（同 seed 同牌；正式对照建议固定段并记录）")
    ap.add_argument("--a", default="enh",
                    help="0/2 队配置：注册 sid（--sids 查看）或 token（basic/enh[+特性]）")
    ap.add_argument("--b", default="basic",
                    help="1/3 队配置：同 --a")
    ap.add_argument("--sids", action="store_true",
                    help="列出注册策略 sid 与 token 通道后退出")
    ap.add_argument("--ladder", action="store_true",
                    help="registry 全对全循环赛并写入天梯（忽略 --a/--b）")
    ap.add_argument("--list", action="store_true", help="查看天梯后退出")
    ap.add_argument("--save", action="store_true",
                    help="A/B 模式结果也写入天梯（默认仅 --ladder 落盘）")
    ap.add_argument("--report", nargs="?", const="", default=None,
                    metavar="PATH",
                    help="固定 seed 段对照：逐 seed 明细写 JSON 报告"
                         "（缺省路径 bench_reports/report_<ts>.json）")
    ap.add_argument("--compare", default=None, metavar="OLD.json",
                    help="与旧报告同牌对比（配合 --report 使用）")
    args = ap.parse_args()

    if args.sids:
        show_sids()
        return
    if args.list:
        show_ladder(load_ladder())
        return
    if args.ladder:
        run_ladder(args.games, args.workers, args.seed0)
        return
    if args.games < 1:
        raise SystemExit("--games 须 ≥ 1")
    if args.compare is not None and args.report is None:
        raise SystemExit("--compare 须与 --report 同用（先跑新报告再对比）")

    cfg_a, cfg_b = parse_cfg(args.a), parse_cfg(args.b)
    label_a, label_b = label_of(cfg_a), label_of(cfg_b)
    print(f"A 队(0/2) = {label_a}   B 队(1/3) = {label_b}")
    print(f"场数 {args.games}，workers {args.workers}，"
          f"seed {args.seed0}..{args.seed0 + args.games - 1}")
    t0 = time.time()
    if args.report is not None:
        rep = run_games_report(args.games, cfg_a, cfg_b, args.workers,
                               args.seed0)
        n = rep["summary"]["rounds"]
        k = rep["summary"]["a_round_wins"]
        err = rep["summary"]["errors"]
        matches_won = rep["summary"]["a_match_wins"]
        dt = rep["summary"]["elapsed_s"]
    else:
        n, k, err, matches_won = run_games(args.games, cfg_a, cfg_b,
                                           args.workers, args.seed0)
        dt = time.time() - t0
        rep = None
    p, lo, hi = wilson(k, n)
    print("-" * 64)
    print(f"比赛场数 {args.games - err}（异常 {err}）  总局数 {n}  耗时 {dt:.0f}s"
          f"（{dt / max(args.games, 1):.2f}s/场）")
    print(f"A 队轮胜率  {k}/{n} = {p * 100:.2f}%  95%CI [{lo * 100:.2f}%, "
          f"{hi * 100:.2f}%]  ±{(hi - p) * 100:.2f}%")
    if args.games - err > 0:
        print(f"A 队整场胜  {matches_won}/{args.games - err} "
              f"= {matches_won / (args.games - err) * 100:.1f}%")
    verdict = "显著优于" if lo > 0.5 else ("显著劣于" if hi < 0.5 else "与 B 无显著差异")
    print(f"结论：A 队（{label_a}）{verdict} B 队（{label_b}）")
    if args.save and err < args.games:
        d = load_ladder()
        record_result(d, label_a, label_b, args.games - err, n, k,
                      matches_won, args.seed0)
        save_ladder(d)
        print(f"已写入天梯: {LADDER_PATH}")
    if rep is not None:
        path = args.report or os.path.join(
            REPORTS_DIR, "report_{}_{}_vs_{}.json".format(
                time.strftime("%Y%m%d_%H%M%S"), label_a, label_b))
        save_report(rep, path)
        print(f"已写报告: {path}")
        if args.compare is not None:
            old = load_report(args.compare)
            print(compare_reports(old, rep))


if __name__ == "__main__":
    main()
