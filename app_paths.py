# -*- coding: utf-8 -*-
"""程序资源与用户数据的路径解析。

打包成 deb 安装到 /usr/share 后**程序目录是只读的**，因此需要写入的文件
（ui_settings.json、Telegram 会话、配置文件）必须落到用户目录：

- 资源文件（图标、默认封面）：随程序安装，只读访问
- 可写数据：源码目录可写时（开发模式）就地保存，保持原有习惯；
  系统安装后回退到 ``$XDG_CONFIG_HOME/ruiplayer``（默认 ``~/.config/ruiplayer``）

也可用环境变量 ``RUIPLAYER_HOME`` 强制指定可写目录（便于测试与便携使用）。
"""

import os

# 程序文件所在目录（安装后即 /usr/share/ruiplayer）
PROGRAM_DIR = os.path.dirname(os.path.abspath(__file__))

APP_NAME = "ruiplayer"


def resource_path(*parts):
    """程序自带资源（图标、默认封面等）的路径，只读"""
    return os.path.join(PROGRAM_DIR, *parts)


def _user_config_dir():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    return os.path.join(base, APP_NAME)


def writable_dir():
    """返回可写的配置目录（不存在时自动创建）"""
    override = os.environ.get("RUIPLAYER_HOME")
    if override:
        try:
            os.makedirs(override, exist_ok=True)
        except OSError:
            pass
        return override
    if os.access(PROGRAM_DIR, os.W_OK):
        return PROGRAM_DIR
    path = _user_config_dir()
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass
    return path


def writable_path(filename):
    """可写文件（配置、会话等）的完整路径"""
    return os.path.join(writable_dir(), filename)


def media_out_dir():
    """Telegram 音乐下载的默认目录

    开发模式沿用程序目录下的 music/（与原来一致）；
    系统安装后写到用户音乐目录，避免往 /usr/share 里塞文件。
    """
    override = os.environ.get("RUIPLAYER_MEDIA_DIR")
    if override:
        return override
    if writable_dir() == PROGRAM_DIR:
        return os.path.join(PROGRAM_DIR, "music")
    music = os.path.join(os.path.expanduser("~"), "Music")
    base = music if os.path.isdir(music) else writable_dir()
    return os.path.join(base, "RuiPlayer")


def is_installed():
    """是否运行在系统安装位置（而非源码目录）"""
    return writable_dir() != PROGRAM_DIR
