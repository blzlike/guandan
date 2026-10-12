"""单机掼蛋程序入口。

用法：
    python main.py                 # 启动 Kivy 图形界面（1人类 + 3AI）
    python main.py --simulate 5    # 无界面，AI 全自动模拟 5 场完整比赛（冒烟/验证）
    python main.py --seed 42       # 指定随机种子（可复现）

打包 Windows EXE：见 build.spec / README，使用 PyInstaller 独立打包。
"""
from __future__ import annotations

import os
import sys

# Kivy 配置必须在导入任何 kivy.uix 之前设置
os.environ.setdefault("KIVY_NO_CONSOLELOG", "0")
os.environ["KIVY_NO_ARGS"] = "1"  # 不让 Kivy 解析 sys.argv（我们自行解析）
try:
    from kivy.config import Config
    Config.set("graphics", "width", "1280")
    Config.set("graphics", "height", "720")
    Config.set("graphics", "resizable", "1")
    Config.set("kivy", "window_state", "visible")
    # 双击判定放宽到 350ms（默认 250ms），触屏上双击快速选组更易触发
    Config.set("input", "double_tap_time", "350")
    # 安卓启动黑屏修复①：关闭 MSAA。部分机型 multisamples=2 的 EGL 配置
    # 首帧不合成（黑屏直到触摸）；卡牌游戏无需抗锯齿
    Config.set("graphics", "multisamples", "0")
    if sys.platform not in ("android", "ios"):
        # 桌面用横屏；安卓朝向由 buildozer.spec orientation 控制，
        # 避免窗口创建后再改朝向触发 surface 重建丢帧（黑屏修复②）
        Config.set("graphics", "orientation", "landscape")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _simulate(n: int, seed):
    import random
    from ai import AIProfile, simulate_match
    from game import GuandanGame
    profiles = [AIProfile("balanced"), AIProfile("aggressive"),
                AIProfile("balanced"), AIProfile("conservative")]
    total_actions = 0
    for i in range(n):
        sd = None if seed is None else seed + i
        g = GuandanGame(rng=random.Random(sd))
        res = simulate_match(g, profiles)
        total_actions += res["actions"]
        print(f"比赛{i + 1}: 胜队={res['winner_team']} 局数={res['rounds']} "
              f"等级={res['levels']} 动作={res['actions']}")
    print(f"共 {n} 场全部正常结束，无死循环。平均动作 {total_actions // max(1, n)}")


def main():
    args = sys.argv[1:]
    if "--simulate" in args:
        idx = args.index("--simulate")
        n = int(args[idx + 1]) if idx + 1 < len(args) else 1
        seed = None
        if "--seed" in args:
            seed = int(args[args.index("--seed") + 1])
        _simulate(n, seed)
        return
    from ui import run
    seed = None
    if "--seed" in args:
        seed = int(args[args.index("--seed") + 1])
    run(seed=seed)


if __name__ == "__main__":
    main()
