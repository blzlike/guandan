"""音效与背景乐管理（Kivy SoundLoader，无新增第三方依赖）。

资源约定：``sounds/`` 目录下放置 ogg（安卓推荐）或 wav 文件，
文件名（不含扩展名）见 ``SFX_FILES`` 与 ``BGM_FILES``。
缺失的素材在加载时静默跳过，播放调用不产生错误——
便于先接好全部代码、后续按清单补充音频文件。

事件→音效映射（参考通用掼蛋习惯）：
- deal        发牌（每局开始）
- play_*      出牌，按牌型区分（单张/对子/复合牌型/炸弹/同花顺/天王炸）
- pass        过牌（低沉提示音）
- tribute     进贡；return_tribute 还贡
- alert       报牌警示（剩牌≤阈值）
- round_win / round_lose    一局胜负结算
- match_win / match_lose    整场比赛终局
- bgm_track1 / bgm_track2   背景乐（循环播放，设置三选一）
"""
from __future__ import annotations

import os
import sys
from typing import Callable, Dict, Optional

from kivy.core.audio import SoundLoader

from rules import HandType

SOUND_DIR = os.path.join(
    getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))), "sounds")

# 事件名 -> 音频文件名（不含扩展名，依次尝试 .ogg / .wav）
SFX_FILES: Dict[str, str] = {
    "deal": "deal",
    "pass": "pass",
    "tribute": "tribute",
    "return_tribute": "return_tribute",
    "alert": "alert",
    "play_single": "play_single",
    "play_pair": "play_pair",
    "play_multi": "play_multi",       # 三同张/三带二/顺子/连对/钢板
    "play_bomb": "play_bomb",
    "play_sf": "play_sf",
    "play_rocket": "play_rocket",
    "round_win": "round_win",
    "round_lose": "round_lose",
    "match_win": "match_win",
    "match_lose": "match_lose",
}

BGM_FILES: Dict[str, str] = {
    "track1": "bgm_track1",
    "track2": "bgm_track2",
}


def event_for_combo(combo) -> str:
    """按牌型返回出牌音效事件名。"""
    t = combo.type
    if t is HandType.SINGLE:
        return "play_single"
    if t is HandType.PAIR:
        return "play_pair"
    if t is HandType.ROCKET:
        return "play_rocket"
    if t is HandType.STRAIGHT_FLUSH:
        return "play_sf"
    if t is HandType.BOMB:
        return "play_bomb"
    return "play_multi"


class SoundManager:
    """统一管理音效与背景乐。

    ``settings_getter``：返回设置字典的可调用（实时读取开关/曲目）。
    所有方法在素材缺失、音频后端不可用时静默降级，绝不抛错。
    """

    def __init__(self, settings_getter: Callable[[], dict]) -> None:
        self._get = settings_getter
        self._sfx: Dict[str, object] = {}
        self._bgm = None
        self._bgm_name: Optional[str] = None
        self._loaded = False

    # ---- 内部
    def _load_one(self, stem: str):
        for ext in (".ogg", ".wav"):
            path = os.path.join(SOUND_DIR, stem + ext)
            if os.path.exists(path):
                try:
                    return SoundLoader.load(path)
                except Exception:
                    return None
        return None

    def ensure_loaded(self) -> None:
        """懒加载全部音效素材（仅首次生效）。"""
        if self._loaded:
            return
        self._loaded = True
        for ev, stem in SFX_FILES.items():
            self._sfx[ev] = self._load_one(stem)

    # ---- 对外
    def play(self, event: str) -> None:
        """播放一次音效；开关关闭或素材缺失时静默跳过。"""
        if not self._get().get("sound", True):
            return
        self.ensure_loaded()
        snd = self._sfx.get(event)
        if snd is not None:
            try:
                snd.stop()
                snd.play()
            except Exception:
                pass

    def update_bgm(self) -> None:
        """按当前设置切换/启停背景乐（设置保存后调用）。"""
        name = self._get().get("bgm", "off")
        if name == "off":
            self.stop_bgm()
            return
        if name == self._bgm_name and self._bgm is not None:
            return          # 曲目未变，不打断
        self.stop_bgm()
        snd = self._load_one(BGM_FILES.get(name, ""))
        if snd is not None:
            try:
                snd.loop = True
                snd.play()
            except Exception:
                snd = None
        self._bgm = snd
        self._bgm_name = name if snd is not None else None

    def stop_bgm(self) -> None:
        if self._bgm is not None:
            try:
                self._bgm.stop()
            except Exception:
                pass
        self._bgm = None
        self._bgm_name = None

    def stop_all(self) -> None:
        """App 退出时调用。"""
        self.stop_bgm()
        for snd in self._sfx.values():
            if snd is not None:
                try:
                    snd.stop()
                except Exception:
                    pass
