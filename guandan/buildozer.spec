[app]

# 应用基本信息
title = 掼蛋
package.name = guandan
version = 1.0
package.domain = org.guandan
author = guandan
author.email = unknown@example.com

# 源码目录与入口
source.dir = .
source.mainclass = main
source.include_exts = py,png,jpg,jpeg,ttf,otf,json
source.exclude_dirs = tests, assets, 图片资源, bin, .buildozer, .gradle
source.exclude_patterns = .pytest_cache/*, __pycache__/*, */__pycache__/*, .idea/*

# 依赖：Kivy 2.3.1（与桌面开发版本一致）
requirements = python3,kivy==2.3.1

# 锁定 p4a 到 2026-10-11 的 master：旧版 p4a 会在构建 venv 中升级 pip 至 26.2.1，
# 该版本存在 BuildDependencyInstallError 导入缺陷导致安装依赖崩溃
p4a.branch = master
p4a.commit = d97d80710c014bf2b65e0a979e3176562e924562

# 横屏 + 全屏
orientation = landscape
fullscreen = 1

# 安卓构建参数
android.api = 34
android.minapi = 21
android.archs = arm64-v8a, armeabi-v7a
android.accept_sdk_license = True
android.allow_backup = True

# 调试版 APK（云构建产物，安装时需允许未知来源）
android.mode = debug

[buildozer]

log_level = 2
warn_on_root = 0
