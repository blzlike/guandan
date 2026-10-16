"""牌力测算中心（阶段二）：A/B 基准 + 策略天梯。

由 bench_ai.py 升级为模块，命令行用法保持兼容：

    python bench.py --games 300 --a "enh+feed" --b "basic"      # 细粒度 token
    python bench.py --games 500 --a classic_high --b classic_low  # 注册策略 sid
    python bench.py --ladder --games 300    # registry 全对全循环赛，写入天梯
    python bench.py --list                  # 查看天梯（Elo 排名 + 对战聚合）

配置双通道（--a/--b）：
- 细粒度 token（A/B 调参）：``basic`` | ``enh``，可叠加 ``+mem_lead``
  ``+urgent_top`` ``+feed``（见 ai.py 模块头）；
- 注册策略 sid：registry.ids()（首批 classic_low / classic_high，未来
  pimc/dmc 零改动接入）。

方法（沿用历史口径）：同 seed 同牌，0/2 队配置 A vs 1/3 队配置 B；
统计单元 = 单局头游队伍 + 整场比赛胜方；Wilson 95% CI。

天梯 ladder.json（与 bench.py 同目录，聚合 + Elo）：按配置标签聚合
（场次/总局数/轮胜/整场胜），Elo 按整场胜负整批更新（K=32，初始
1500），另存最近 50 条历史。A/B 模式默认不落盘（调参实验），--save
显式写入；--ladder 模式每对跑完即落盘。
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

LADDER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ladder.json")
LADDER_VERSION = 1
LADDER_HISTORY = 50
ELO_K = 32.0
ELO_BASE = 1500.0


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
def _step(g: GuandanGame, by_pid: dict) -> bool:
    """驱动一步（token/sid 统一分发；tribute 阶段与 ai.auto_step 同逻辑）。"""
    if g.phase == "match_over":
        return False
    if g.phase == "round_over":
        g.start_next_round()
        return True
    if g.phase == "tribute":
        pending = [(gv, r) for gv, r in g.tribute_pairs if gv not in g.tribute_done]
        if pending:
            giver, _ = pending[0]
            g.submit_tribute(giver, ai.choose_tribute(g, giver))
        else:
            receiver = [r for r in g.tribute_done.values()
                        if r not in g.return_done][0]
            g.submit_return(receiver, ai.choose_return(g, receiver))
        return True
    pid = g.turn
    kind, v = by_pid[pid]
    if kind == "sid":
        act, cards = registry.choose(g, pid, ai.AIProfile(), v)
    else:
        act, cards = ai.choose_action(g, pid, ai.AIProfile(),
                                      enhanced=v[0], features=v[1])
    if act == "play":
        g.play(pid, cards)
    else:
        g.pass_play(pid)
    return True


def play_match(seed: int, cfg_a: tuple, cfg_b: tuple) -> tuple:
    """打一场比赛，返回（各局头游队伍列表, 整场胜方队伍）。"""
    g = GuandanGame(random.Random(seed))
    by_pid = {p: cfg_a if p % 2 == 0 else cfg_b for p in range(4)}
    round_winners: list[int] = []
    actions = 0
    while g.phase != "match_over":
        _step(g, by_pid)
        actions += 1
        if actions >= MAX_ACTIONS:
            raise RuntimeError(f"seed={seed} 模拟超限，疑似死循环")
        if g.phase == "round_over" and g.round_result:
            round_winners.append(g.round_result["winner_team"])
    if g.round_result:
        round_winners.append(g.round_result["winner_team"])
    return round_winners, g.match_winner


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
            rws, mw = res
            n += len(rws)
            k += sum(1 for t in rws if t == 0)
            matches_won += 1 if mw == 0 else 0
            if i % 25 == 0 or i == games:
                print(f"  进度 {i}/{games}  A 队轮胜 {k}/{n} "
                      f"({k / max(n, 1) * 100:.1f}%)")
    return n, k, err, matches_won


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


# ---------------------------------------------------------------- CLI
def main() -> None:
    ap = argparse.ArgumentParser(
        description="牌力测算中心：A/B 基准（token 或 sid）+ 策略天梯")
    ap.add_argument("--games", type=int, default=200, help="比赛场数（每场约 6-15 局）")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 4) - 1))
    ap.add_argument("--seed0", type=int, default=1000, help="起始 seed（同 seed 同牌）")
    ap.add_argument("--a", default="enh", help="0/2 队配置：token 或注册 sid")
    ap.add_argument("--b", default="basic", help="1/3 队配置：token 或注册 sid")
    ap.add_argument("--ladder", action="store_true",
                    help="registry 全对全循环赛并写入天梯（忽略 --a/--b）")
    ap.add_argument("--list", action="store_true", help="查看天梯后退出")
    ap.add_argument("--save", action="store_true",
                    help="A/B 模式结果也写入天梯（默认仅 --ladder 落盘）")
    args = ap.parse_args()

    if args.list:
        show_ladder(load_ladder())
        return
    if args.ladder:
        run_ladder(args.games, args.workers, args.seed0)
        return

    cfg_a, cfg_b = parse_cfg(args.a), parse_cfg(args.b)
    label_a, label_b = label_of(cfg_a), label_of(cfg_b)
    print(f"A 队(0/2) = {label_a}   B 队(1/3) = {label_b}")
    print(f"场数 {args.games}，workers {args.workers}，"
          f"seed {args.seed0}..{args.seed0 + args.games - 1}")
    t0 = time.time()
    n, k, err, matches_won = run_games(args.games, cfg_a, cfg_b,
                                       args.workers, args.seed0)
    dt = time.time() - t0
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


if __name__ == "__main__":
    main()
