# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 单文件打包：掼蛋（Windows 独立 exe）。

构建：  python -m PyInstaller guandan.spec --noconfirm
产物：  dist/Guandan.exe（onefile，双击运行）
说明：
- 只读资源（sounds/、头像、背景、logo）随包打进 exe，运行时解包到 _MEIPASS；
- settings.json 不打包，首次运行后在 exe 同目录生成（可持久化）；
- Windows 下字体走系统 msyh / Segoe UI Symbol，fonts/Noto 不打进 exe
  （仅 Android/Buildozer 需要，见 buildozer.spec）；
- 不含 gstreamer：音频走 SDL2_mixer（kivy_deps.sdl2）。
"""
import os
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

datas = [
    ("sounds", "sounds"),
    ("avatar0.png", "."), ("avatar1.png", "."), ("avatar2.png", "."), ("avatar3.png", "."),
    ("background.jpg", "."), ("logo.png", "."),
]
datas += collect_data_files("kivy")

binaries = []
for _pkg in ("kivy_deps.sdl2", "kivy_deps.glew"):
    try:
        binaries += collect_dynamic_libs(_pkg)
    except Exception:
        pass

a = Analysis(
    ["main.py"],
    pathex=[SPECPATH],
    binaries=binaries,
    datas=datas,
    hiddenimports=[
        "kivy.core.window.window_sdl2",
        "kivy.core.text.text_sdl2",
        "kivy.core.image.img_pil",
        "kivy.core.audio.audio_sdl2",
        "kivy.core.gl.gl_sdl2",
    ],
    excludes=["tests", "pytest", "soundfile"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Guandan",
    icon=os.path.join(SPECPATH, "logo.ico"),
    console=False,
    disable_windowed_traceback=False,
    upx=False,
)
