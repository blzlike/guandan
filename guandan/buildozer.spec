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

# 锁定 p4a 到 develop 分支（p4a 默认分支为 develop，master 分支落后且不含修复）：
# develop 已移除构建 venv 中的 pip 自我升级逻辑，规避 pip 26.2.1 的
# BuildDependencyInstallError 导入缺陷导致安装依赖崩溃
p4a.branch = develop
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
