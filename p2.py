import os
import sys
import re
import json
import html
import hashlib
import tempfile
import time
from datetime import datetime
from io import BytesIO
from pathlib import Path
from urllib.parse import urljoin

from PyQt6.QtCore import QEvent, Qt, QTimer, QUrl
from PyQt6.QtGui import (
    QColor,
    QDesktopServices,
    QFont,
    QIcon,
    QImage,
    QKeySequence,
    QPainter,
    QPixmap,
    QShortcut,
)
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSlider,
    QSplitter,
    QSystemTrayIcon,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from PIL import Image

import mpris_player
from metadata_reader import MetadataReader
from music_scanner import (
    MusicScanWorker,
    format_duration,
    format_size,
    read_track_meta,
    track_display_text,
    track_tooltip,
)
from tag_editor import (
    CONTAINER_LABELS,
    FIELD_LABELS,
    TAG_FIELDS,
    probe_container,
    read_tags,
    save_tags,
)

try:
    from tg_music import HAS_TELETHON, TelegramMusicDialog
except Exception:  # 缺少 telethon 时程序仍可正常运行
    HAS_TELETHON = False
    TelegramMusicDialog = None

try:
    import vlc
except Exception:
    print("错误：请安装 VLC 播放器，执行 pip install python-vlc")
    sys.exit(1)

try:
    from mutagen import File as MutagenFile
    from mutagen.flac import FLAC
    from mutagen.mp3 import MP3
except Exception:
    MutagenFile = None
    MP3 = FLAC = None


# 各音频格式的标题/艺术家/专辑标签键（按优先级排列）
TAG_KEY_MAP = {
    "title": ("TIT2", "title", "TITLE", "\xa9nam"),
    "artist": ("TPE1", "artist", "ARTIST", "\xa9ART"),
    "album": ("TALB", "album", "ALBUM", "\xa9alb"),
}


def get_tag_text(tags, field):
    """从音频标签对象中读取首个文本值，兼容多种标签格式：
    - MP3: mutagen ID3 帧（如 TIT2/TPE1/TALB，值为含 .text 的帧对象）
    - FLAC/OGG: VorbisComment（小写键 title/artist/album，值为列表）
    - M4A/MP4: MP4Tags（©nam/©ART/©alb，值为列表）
    Vorbis 容器对不存在的键会抛异常，因此统一用成员判断 + try/except 兜底。
    """
    if not tags:
        return None
    for key in TAG_KEY_MAP.get(field, ()):
        try:
            if key not in tags:
                continue
            value = tags[key]
        except Exception:
            continue
        # ID3 文本帧：value.text 为字符串列表
        if hasattr(value, "text") and value.text:
            return str(value.text[0])
        # 列表/元组（Vorbis、MP4 等）
        if isinstance(value, (list, tuple)) and len(value) > 0:
            return str(value[0])
        # 纯字符串
        if isinstance(value, str) and value.strip():
            return str(value)
    return None


def get_embedded_lyrics(file_path):
    """从音频文件元数据中读取内嵌歌词（LRC 文本）。

    支持：MP3/ID3 的 USLT 帧、FLAC/OGG 的 VorbisComment（LYRICS 等）、
    M4A/MP4 的 ©lyr。找不到时返回 None。
    """
    if not MutagenFile:
        return None
    try:
        audio = MutagenFile(file_path)
    except Exception:
        return None
    if not audio:
        return None
    tags = getattr(audio, "tags", None)
    if not tags:
        return None

    def _first(value):
        if isinstance(value, (list, tuple)):
            return str(value[0]) if value else None
        if isinstance(value, str):
            return value
        return None

    # ID3（MP3）：USLT 帧的歌词文本在 .text
    try:
        keys = list(tags.keys())
    except Exception:
        keys = []
    for k in keys:
        if not (isinstance(k, str) and k.startswith("USLT")):
            continue
        try:
            frame = tags[k]
        except Exception:
            continue
        text = getattr(frame, "text", None)
        got = _first(text) if text is not None else None
        if got and got.strip():
            return got
    # VorbisComment（FLAC/OGG）
    for key in ("LYRICS", "lyrics", "UNSYNCEDLYRICS", "UNSYNCED LYRICS",
                "unsyncedlyrics"):
        try:
            if key in tags:
                got = _first(tags[key])
                if got and got.strip():
                    return got
        except Exception:
            continue
    # MP4 / M4A
    for key in ("\xa9lyr", "LYR", "\xa9lyrics"):
        try:
            if key in tags:
                got = _first(tags[key])
                if got and got.strip():
                    return got
        except Exception:
            continue
    return None


# 视频扩展名：本地文件与网络串流分开判定
LOCAL_VIDEO_EXTS = (".mp4", ".mkv", ".avi", ".mov", ".flv", ".webm", ".mpeg", ".mpg")
STREAM_VIDEO_EXTS = LOCAL_VIDEO_EXTS + (".ts", ".m3u8")

# 音乐库“短音频”过滤预设：(显示文本, 秒数)；0 表示不过滤
# 时长短于此值的文件（多为系统音效/提示音）不入库，但不会被从磁盘删除
MIN_DURATION_CHOICES = (
    ("不过滤", 0), ("5 秒", 5), ("10 秒", 10),
    ("15 秒", 15), ("30 秒", 30), ("60 秒", 60),
)
DEFAULT_MIN_DURATION = 30

# GitHub 网页(blob/raw)链接 → raw 直链，供网络串流输入框使用
_GITHUB_BLOB_RE = re.compile(
    r"^https?://github\.com/([^/]+)/([^/]+)/(?:blob|raw)/(.+)$", re.IGNORECASE
)


def normalize_stream_url(url):
    """规整用户输入的串流地址：
    - GitHub blob/raw 网页链接自动转成 raw.githubusercontent.com 直链
    - 缺少协议时补 http://
    - 本地存在的文件路径原样返回
    """
    url = (url or "").strip()
    if not url:
        return url
    match = _GITHUB_BLOB_RE.match(url)
    if match:
        user, repo, rest = match.group(1), match.group(2), match.group(3)
        return f"https://raw.githubusercontent.com/{user}/{repo}/{rest}"
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", url):
        return url
    if os.path.exists(url):
        return url
    return "http://" + url


def looks_like_hls(text):
    """HLS 播放列表特征（分段/主列表标签）"""
    return "#EXT-X-" in (text or "")


def load_stream_history():
    """读取网络串流历史记录"""
    hist = load_settings().get("stream_history")
    if not isinstance(hist, list):
        return []
    return [h for h in hist if isinstance(h, str) and h.strip()]


def save_stream_history(items):
    """保存网络串流历史记录（最多 10 条）"""
    data = load_settings()
    data["stream_history"] = [i for i in items if i][:10]
    save_settings(data)


def format_time_ms(ms):
    """把毫秒格式化为 mm:ss"""
    try:
        ms = int(ms)
    except (TypeError, ValueError):
        ms = 0
    if ms < 0:
        ms = 0
    total_s = ms // 1000
    return f"{total_s // 60:02d}:{total_s % 60:02d}"


class FullscreenControls(QWidget):
    """全屏浮动控制栏：叠加在画面之上，一段时间无操作自动隐藏。

    作为原生子窗口（WA_NativeWindow）创建，才能在 Linux/Windows 上都盖在
    VLC 嵌入的视频子窗口之上。
    """

    def __init__(self, player, parent):
        super().__init__(parent)
        self.player = player
        self.setObjectName("fsControls")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        scale = getattr(player, "ui_scale", 1.0)
        font_px = max(12, int(round(13 * scale)))
        self.bar_height = max(48, int(round(52 * scale)))
        self.setStyleSheet(
            f"#fsControls {{ background: #1b1b1b; border-top: 1px solid #3a3a3a; "
            f"font-size: {font_px}px; }}"
            "#fsControls QLabel { color: #ffffff; background: transparent; }"
            "#fsControls QPushButton { color: #ffffff; background: #2d2d2d; "
            "border: 1px solid #4a4a4a; border-radius: 4px; padding: 4px 10px; }"
            "#fsControls QPushButton:hover { background: #3d3d3d; }"
            "#fsControls QComboBox { color: #ffffff; background: #2d2d2d; "
            "border: 1px solid #4a4a4a; border-radius: 4px; padding: 2px 6px; }"
            "#fsControls QComboBox QAbstractItemView { color: #ffffff; "
            "background: #2d2d2d; selection-background-color: #4da3ff; }"
            "#fsControls QSlider::groove:horizontal { height: 4px; background: #555555; "
            "border-radius: 2px; }"
            "#fsControls QSlider::handle:horizontal { background: #4da3ff; width: 12px; "
            "margin: -5px 0; border-radius: 6px; }"
            "#fsControls QSlider::sub-page:horizontal { background: #4da3ff; "
            "border-radius: 2px; }"
            "#fsControls QCheckBox { color: #ffffff; background: transparent; }"
        )

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 6, 12, 6)
        lay.setSpacing(8)

        self.btn_play = QPushButton("播放")
        self.btn_stop = QPushButton("停止")
        self.lbl_cur = QLabel("00:00")
        self.slider_pos = QSlider(Qt.Orientation.Horizontal)
        self.slider_pos.setRange(0, 0)
        self.lbl_total = QLabel("00:00")
        self.lbl_vol = QLabel("音量")
        self.slider_vol = QSlider(Qt.Orientation.Horizontal)
        self.slider_vol.setRange(0, 100)
        self.slider_vol.setFixedWidth(max(80, int(round(100 * scale))))
        self.cbx_speed = QComboBox()
        self.cbx_speed.addItems(["0.5x", "0.7x", "1.0x", "1.2x", "1.5x", "2.0x"])
        self.btn_loop = QPushButton("循环")
        self.btn_lyrics = QPushButton("歌词")
        self.btn_lyrics.setToolTip("显示/隐藏全屏歌词（全屏下按 L）")
        self.btn_lyric_upload = QPushButton("上传歌词")
        self.btn_lyric_upload.setToolTip("选择 .lrc 歌词文件")
        self.cbx_lyrics = QCheckBox("内嵌歌词")
        self.cbx_lyrics.setToolTip("从文件元数据读取内嵌歌词")
        self.btn_monitor = QPushButton("显示器")
        self.btn_monitor.setToolTip("选择全屏显示器（全屏下按 M）")
        self.btn_exit = QPushButton("退出全屏")
        self.btn_exit.setToolTip("退出全屏（ESC）")
        self.btn_play.setToolTip("播放/暂停（空格）")

        lay.addWidget(self.btn_play)
        lay.addWidget(self.btn_stop)
        lay.addWidget(self.lbl_cur)
        lay.addWidget(self.slider_pos, stretch=1)
        lay.addWidget(self.lbl_total)
        lay.addWidget(self.lbl_vol)
        lay.addWidget(self.slider_vol)
        lay.addWidget(self.cbx_speed)
        lay.addWidget(self.btn_loop)
        lay.addWidget(self.btn_lyrics)
        lay.addWidget(self.btn_lyric_upload)
        lay.addWidget(self.cbx_lyrics)
        lay.addWidget(self.btn_monitor)
        lay.addWidget(self.btn_exit)

        self.btn_play.clicked.connect(self.player.play_pause)
        self.btn_stop.clicked.connect(self._stop)
        self.slider_pos.sliderMoved.connect(self.player.seek_pos)
        self.slider_vol.valueChanged.connect(self.player.set_volume)
        self.cbx_speed.currentTextChanged.connect(self.player.set_play_speed)
        self.btn_loop.clicked.connect(self.player.toggle_loop)
        self.btn_lyrics.clicked.connect(self.player.toggle_fullscreen_lyrics)
        self.btn_lyric_upload.clicked.connect(self._upload_lyrics)
        self.cbx_lyrics.toggled.connect(self.player.set_lyrics_meta_enabled)
        self.btn_monitor.clicked.connect(self.player.open_screen_chooser)
        self.btn_exit.clicked.connect(self.player.exit_fullscreen)
        self.refresh_monitor_button()

    def _upload_lyrics(self):
        """上传歌词：以全屏窗口为父窗口，避免文件对话框被全屏画面遮住"""
        self.player.load_lrc_file(self.window())

    def refresh_lyrics_state(self, meta_enabled, lyrics_visible):
        """同步歌词按钮文字与“内嵌歌词”复选框状态"""
        self.btn_lyrics.setText("歌词(开)" if lyrics_visible else "歌词")
        self.cbx_lyrics.blockSignals(True)
        self.cbx_lyrics.setChecked(bool(meta_enabled))
        self.cbx_lyrics.blockSignals(False)

    def refresh_monitor_button(self):
        """更新“显示器”按钮可用状态与提示（点击会弹出选择窗口）"""
        try:
            count = len(QApplication.screens())
        except Exception:
            count = 0
        self.btn_monitor.setEnabled(count > 0)
        self.btn_monitor.setToolTip(
            f"选择全屏显示器（共 {count} 个，全屏下按 M）" if count else "未检测到显示器"
        )

    def _stop(self):
        """控制栏内停止：只停止播放，不退出全屏"""
        self.player.media_player.stop()
        self.player.slider_pos.setValue(0)
        self.slider_pos.setValue(0)


class FullscreenWindow(QWidget):
    """全屏展示窗口：视频由 VLC 直接渲染铺满屏幕；音频显示放大封面。
    底部浮动控制栏鼠标靠近底部时浮现、无操作自动隐藏；支持全屏歌词。"""

    HIDE_DELAY = 3500

    def __init__(self, player):
        super().__init__()
        self.player = player
        self.setObjectName("fsRoot")
        self.setWindowTitle("全屏播放")
        self.setWindowFlags(
            Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint
        )
        self.setStyleSheet("#fsRoot { background: #000000; }")
        self.setMouseTracking(True)

        # 歌词状态
        self.lyric_lines = []
        self.lyric_idx = -1
        self.show_lyrics = True

        self.label = QLabel(self)
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setStyleSheet("background: #000000; color: #888888;")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.label)

        # 歌词覆盖层：同样需要原生子窗口才能盖在 VLC 视频之上
        self.lyric_label = QLabel(self)
        self.lyric_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lyric_label.setWordWrap(True)
        self.lyric_label.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.lyric_label.setStyleSheet(
            "background: rgba(0, 0, 0, 170); color: #ffffff; "
            "padding: 8px; border-radius: 8px;"
        )
        self.lyric_label.hide()

        # 浮动控制栏：原生子窗口，才能盖在 VLC 视频之上
        self.controls = FullscreenControls(player, self)
        self.controls.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)

        # 底部热区：控制栏隐藏后，鼠标移到底部即可召唤出来
        self.hotzone = QWidget(self)
        self.hotzone.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.hotzone.setMouseTracking(True)
        self.hotzone.setStyleSheet("background: #000000;")
        self.hotzone.installEventFilter(self)
        self.hotzone.setToolTip("移动鼠标到此处显示控制栏")

        self.hide_timer = QTimer(self)
        self.hide_timer.setSingleShot(True)
        self.hide_timer.setInterval(self.HIDE_DELAY)
        self.hide_timer.timeout.connect(self._auto_hide)

        self._raise_tick = 0

        # M 键：打开“更改全屏显示器”窗口（用快捷键，避免视频子窗口抢走键盘焦点）
        self.shortcut_monitor = QShortcut(QKeySequence("M"), self)
        self.shortcut_monitor.setContext(Qt.ShortcutContext.WindowShortcut)
        self.shortcut_monitor.activated.connect(self.player.open_screen_chooser)

    # ---------- 覆盖层布局与显隐 ----------
    def _layout_overlay(self):
        h = self.controls.bar_height
        self.controls.setGeometry(0, max(0, self.height() - h), self.width(), h)
        self.hotzone.setGeometry(0, max(0, self.height() - 8), self.width(), 8)
        # 歌词：水平居中，位于控制栏上方
        lw = int(self.width() * 0.86)
        lh = max(96, int(h * 2.2))
        self.lyric_label.setGeometry(
            max(0, (self.width() - lw) // 2),
            max(0, self.height() - h - lh - 24),
            lw,
            lh,
        )
        self.hotzone.raise_()
        self.lyric_label.raise_()
        self.controls.raise_()

    def show_controls(self):
        self.controls.show()
        self._layout_overlay()
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.label.setCursor(Qt.CursorShape.ArrowCursor)
        self._restart_hide_timer()

    def hide_controls(self):
        self.controls.hide()
        self.setCursor(Qt.CursorShape.BlankCursor)
        self.label.setCursor(Qt.CursorShape.BlankCursor)

    def _restart_hide_timer(self):
        self.hide_timer.start()

    def _auto_hide(self):
        # 暂停/停止时保持显示，播放中才自动隐藏
        try:
            playing = self.player.media_player.is_playing()
        except Exception:
            playing = False
        if playing:
            self.hide_controls()

    def on_enter_fullscreen(self):
        self._layout_overlay()
        self.controls.refresh_monitor_button()
        self.controls.refresh_lyrics_state(
            self.player.lyrics_from_metadata,
            bool(self.show_lyrics and self.lyric_lines),
        )
        self.show_controls()
        self.sync_from_player()

    def on_exit_fullscreen(self):
        self.hide_timer.stop()
        self.controls.hide()

    # ---------- 状态同步 ----------
    def sync_from_player(self):
        if not self.isVisible():
            return
        p = self.player
        c = self.controls
        try:
            playing = p.media_player.is_playing()
        except Exception:
            playing = False
        c.btn_play.setText("暂停" if playing else "播放")
        c.btn_loop.setText("循环(开)" if p.loop_single else "循环")

        vol = p.media_player.audio_get_volume()
        if vol is not None and vol >= 0:
            c.slider_vol.blockSignals(True)
            c.slider_vol.setValue(int(vol))
            c.slider_vol.blockSignals(False)

        c.cbx_speed.blockSignals(True)
        idx = c.cbx_speed.findText(f"{p.cur_speed:.1f}x")
        if idx >= 0:
            c.cbx_speed.setCurrentIndex(idx)
        c.cbx_speed.blockSignals(False)

        if not p.is_streaming and not c.slider_pos.isSliderDown():
            total = p.media_player.get_length()
            cur = p.media_player.get_time()
            if total and total > 0:
                c.slider_pos.setRange(0, total)
                c.slider_pos.setValue(max(0, cur))
                c.lbl_total.setText(format_time_ms(total))
            c.lbl_cur.setText(format_time_ms(cur))

        # VLC 新建视频输出时可能压住控制栏，低频重新置顶
        if c.isVisible():
            self._raise_tick += 1
            if self._raise_tick % 20 == 0:
                self._layout_overlay()

    # ---------- 歌词 ----------
    def set_lyrics(self, lines):
        """设置歌词行（外部加载后调用）"""
        self.lyric_lines = list(lines or [])
        self.lyric_idx = -1
        if self.lyric_lines:
            self.show_lyrics = True
            self.update_lyrics(0, force=True)
        else:
            self.lyric_label.clear()
        self._update_lyric_visibility()
        if self.controls is not None:
            self.controls.refresh_lyrics_state(
                self.player.lyrics_from_metadata,
                bool(self.show_lyrics and self.lyric_lines),
            )

    def clear_lyrics(self):
        """清空全屏歌词显示"""
        self.lyric_lines = []
        self.lyric_idx = -1
        self.lyric_label.clear()
        self._update_lyric_visibility()

    def update_lyrics(self, idx, force=False):
        """根据当前歌词行号刷新显示（上一句/当前句/下一句）"""
        if not self.lyric_lines:
            return
        if idx == self.lyric_idx and not force:
            return
        self.lyric_idx = idx
        n = len(self.lyric_lines)
        cur = self.lyric_lines[idx] if 0 <= idx < n else ""
        prev = self.lyric_lines[idx - 1] if 1 <= idx < n else ""
        nxt = self.lyric_lines[idx + 1] if 0 <= idx + 1 < n else ""
        self.lyric_label.setText(self._lyric_html(prev, cur, nxt))
        self._update_lyric_visibility()

    @staticmethod
    def _lyric_html(prev, cur, nxt):
        esc = html.escape

        def line(text, color, size, bold=False):
            content = esc(text) if text else "&nbsp;"
            weight = "bold" if bold else "normal"
            return (
                f"<div style='color:{color};font-size:{size}px;"
                f"font-weight:{weight};'>{content}</div>"
            )

        return (
            line(prev, "#bdbdbd", 15)
            + line(cur, "#ffffff", 22, bold=True)
            + line(nxt, "#bdbdbd", 15)
        )

    def _update_lyric_visibility(self):
        visible = bool(self.show_lyrics and self.lyric_lines)
        self.lyric_label.setVisible(visible)
        if visible:
            self._layout_overlay()

    def toggle_lyrics(self):
        """显示/隐藏全屏歌词"""
        self.show_lyrics = not self.show_lyrics
        self._update_lyric_visibility()
        if self.controls is not None:
            self.controls.refresh_lyrics_state(
                self.player.lyrics_from_metadata,
                bool(self.show_lyrics and self.lyric_lines),
            )
        self.show_controls()

    # ---------- 事件 ----------
    def eventFilter(self, obj, event):
        if obj is self.hotzone and self.isVisible():
            if event.type() == QEvent.Type.Enter:
                self.show_controls()
            elif event.type() == QEvent.Type.MouseButtonDblClick:
                self.player.exit_fullscreen()
            return False
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event):
        # ESC 退出全屏；其余按键转发给播放器（空格暂停/方向键等仍可用）
        if event.key() == Qt.Key.Key_Escape:
            self.player.exit_fullscreen()
            return
        if self.player.handle_global_key_press(event):
            self.show_controls()
            return
        self.show_controls()
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if self.player.handle_global_key_release(event):
            return
        super().keyReleaseEvent(event)

    def mouseMoveEvent(self, event):
        self.show_controls()
        super().mouseMoveEvent(event)

    def mouseDoubleClickEvent(self, event):
        self.player.exit_fullscreen()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._layout_overlay()
        # 音频全屏：封面随窗口尺寸自适应居中显示
        if self.player is not None and not self.player.is_video:
            self.player.update_fullscreen_art()


class MediaPlayer(QMainWindow):
    DEFAULT_COVER_FILENAME = "Xinjiang_Old_and_young_(Populus_diversifolia_胡杨)_(4973519309).jpg"

    def __init__(self):
        super().__init__()
        self.setWindowTitle("RuiPlayer - 多媒体播放器")
        self.resize(1024, 600)
        self.setWindowState(Qt.WindowState.WindowMaximized)

        vlc_args = [
            "--quiet",
            "--network-caching=10000",
            "--live-caching=10000",
            "--http-reconnect",
            "--no-video-title-show",
        ]
        import platform

        if platform.system() == "Windows":
            vlc_args.append("--aout=waveout")
        # Linux 下不强制指定音频输出，让 VLC 自动选择
        # （避免系统缺少 PulseAudio 时音频输出阻塞导致 UI 卡顿）

        self.vlc_instance = vlc.Instance(*vlc_args)
        self.media_player = self.vlc_instance.media_player_new()

        self.cur_media_path = ""
        self.is_video = False
        self.is_streaming = False
        self.lrc_lines = []
        self.lrc_time_list = []
        self.cur_lrc_idx = -1
        self.lyrics_from_metadata = load_lyrics_meta_pref()
        self.cur_speed = 1.0
        self.loop_single = False

        self.equalizer_enabled = True
        self.equalizer_bands = [0.0] * 10
        self.equalizer_preamp = 0.0
        self.equalizer_dialog = None

        self.ui_scale = load_ui_scale()
        self.scale_dialog = None

        self.network_manager = QNetworkAccessManager(self)
        self.channel_dialog = None
        self.tg_dialog = None

        # 音乐库（右侧“音乐库”标签页）：文件夹扫描 + SHA256 内容去重
        self.library_folder_history = load_music_folders()
        self.library_entries, self.library_hashes = load_music_library()
        self.library_items = {}        # path -> QListWidgetItem
        self.duplicate_records = []    # 上次扫描中因内容重复被跳过的文件
        self.short_records = []        # 上次扫描中因时长过短被跳过的文件
        self._last_min_duration = 0    # 上次扫描使用的短音频阀值
        self.scan_worker = None
        self._scan_stats = None

        # 标签编辑（右侧“标签编辑”标签页）：仅对正在播放的音乐文件生效
        self.tag_edits = {}            # key -> 输入框
        self.tag_original = {}         # 上次读取/保存时的值，用于统计改动
        self.tag_current_path = ""     # 当前可编辑的文件路径

        # 系统媒体控制：MPRIS（桌面媒体控件/耳机按键/playerctl）+ 系统托盘 + 媒体键
        self.mpris = None
        self.tray_icon = None
        self.tray_menu = None
        self._mpris_art_urls = {}      # path -> 封面 file:// URL
        self._mpris_length_us = 0      # 上次回传的时长（毫秒→微秒）
        self._last_media_key = 0.0     # 媒体键去重时间戳

        # 全屏播放状态（视频画面 / 音频封面铺满屏幕，ESC 退出）
        self.is_fullscreen = False
        self.fullscreen_window = None

        self.audio_metadata = {
            "sample_rate": "--",
            "channels": "--",
            "artist": "--",
            "album": "--",
            "title": "--",
            "bitrate": "--",
        }

        self.custom_default_cover = None
        self.load_default_cover_from_file()

        self.space_pressed = False
        self.is_space_long = False
        self.original_speed = 1.0
        self.space_timer = QTimer(self)
        self.space_timer.setSingleShot(True)
        self.space_timer.setInterval(200)
        self.space_timer.timeout.connect(self.on_space_long_press)

        self.init_ui()
        self.adapt_layout_to_screen()
        self.install_global_shortcuts()
        self.setup_system_media_control()

        # 系统媒体控制：每秒把播放状态同步给桌面（曲目信息在切换文件时更新）
        self.mpris_timer = QTimer(self)
        self.mpris_timer.setInterval(1000)
        self.mpris_timer.timeout.connect(self.sync_mpris_state)
        self.mpris_timer.start()

        self.timer = QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self.update_progress_and_lrc)
        self.timer.start()

        # 网络串流使用低频定时器，避免每 50ms 高频调用 libvlc 导致 UI 抽帧/无响应
        self.stream_timer = QTimer(self)
        self.stream_timer.setInterval(1000)
        self.stream_timer.timeout.connect(self.update_stream_status)

        # 串流状态跟踪（用于失败提示，避免重复弹窗）
        self._last_stream_state = None
        self._stream_error_shown = False

    def _set_stream_mode(self, streaming):
        """切换轮询模式：网络串流用低频定时器，本地媒体用高频定时器"""
        if streaming:
            self.timer.stop()
            if not self.stream_timer.isActive():
                self.stream_timer.start()
            # 网络串流无法编辑标签，切过去时同步置为不可编辑
            self.refresh_tag_editor("")
        else:
            self.stream_timer.stop()
            if not self.timer.isActive():
                self.timer.start()

    def on_space_long_press(self):
        self.is_space_long = True

    def install_global_shortcuts(self):
        self.video_label.installEventFilter(self)
        self.lrc_list.installEventFilter(self)
        self.info_panel.installEventFilter(self)
        self.cbx_speed.installEventFilter(self)
        self.slider_pos.installEventFilter(self)
        self.slider_vol.installEventFilter(self)
        self.btn_open.installEventFilter(self)
        self.btn_lyric.installEventFilter(self)
        self.cbx_lyrics_meta.installEventFilter(self)
        self.btn_sub.installEventFilter(self)
        self.btn_play.installEventFilter(self)
        self.btn_stop.installEventFilter(self)
        self.btn_loop.installEventFilter(self)
        self.btn_open_folder.installEventFilter(self)
        self.btn_set_cover.installEventFilter(self)

    def eventFilter(self, obj, event):
        if (
            obj is self.video_label
            and event.type() == QEvent.Type.MouseButtonDblClick
        ):
            # 双击画面/封面切换全屏
            self.toggle_fullscreen()
            return True
        if event.type() == QEvent.Type.KeyPress:
            return self.handle_global_key_press(event)
        if event.type() == QEvent.Type.KeyRelease:
            return self.handle_global_key_release(event)
        return super().eventFilter(obj, event)

    def handle_global_key_press(self, event):
        if event.isAutoRepeat():
            return False
        key = event.key()
        handled = True

        # 全屏相关：F 切换全屏；ESC 退出全屏（串流/本地均生效）
        if key == Qt.Key.Key_F:
            self.toggle_fullscreen()
            return True
        if key == Qt.Key.Key_Escape:
            if self.is_fullscreen:
                self.exit_fullscreen()
            return True
        # 全屏下按 M 弹出“更改全屏显示器”窗口
        if key == Qt.Key.Key_M and self.is_fullscreen:
            self.open_screen_chooser()
            return True
        # 全屏下按 L 显示/隐藏歌词
        if key == Qt.Key.Key_L and self.is_fullscreen:
            self.toggle_fullscreen_lyrics()
            return True

        if self.is_streaming:
            if key == Qt.Key.Key_Up:
                vol = self.media_player.audio_get_volume()
                self.media_player.audio_set_volume(min(vol + 5, 100))
                self.slider_vol.setValue(self.media_player.audio_get_volume())
            elif key == Qt.Key.Key_Down:
                vol = self.media_player.audio_get_volume()
                self.media_player.audio_set_volume(max(vol - 5, 0))
                self.slider_vol.setValue(self.media_player.audio_get_volume())
            else:
                handled = False
            return handled

        if key == Qt.Key.Key_Space:
            self.space_pressed = True
            self.is_space_long = False
            self.original_speed = self.cur_speed
            self.cur_speed = 2.0
            self.set_play_speed(str(self.cur_speed))
            self.space_timer.start()
        elif key == Qt.Key.Key_Left:
            current_ms = self.media_player.get_time()
            self.media_player.set_time(max(current_ms - 10000, 0))
        elif key == Qt.Key.Key_Right:
            current_ms = self.media_player.get_time()
            total_ms = self.media_player.get_length()
            self.media_player.set_time(min(current_ms + 10000, total_ms))
        elif key == Qt.Key.Key_Up:
            vol = self.media_player.audio_get_volume()
            self.media_player.audio_set_volume(min(vol + 5, 100))
            self.slider_vol.setValue(self.media_player.audio_get_volume())
        elif key == Qt.Key.Key_Down:
            vol = self.media_player.audio_get_volume()
            self.media_player.audio_set_volume(max(vol - 5, 0))
            self.slider_vol.setValue(self.media_player.audio_get_volume())
        elif key == Qt.Key.Key_Delete:
            self.delete_current_media()
        else:
            handled = False
        return handled

    def handle_global_key_release(self, event):
        key = event.key()
        if self.is_streaming:
            return False
        if key == Qt.Key.Key_Space and self.space_pressed:
            self.space_pressed = False
            self.space_timer.stop()
            if not self.is_space_long:
                self.play_pause()
            self.cur_speed = self.original_speed
            self.set_play_speed(str(self.cur_speed))
            return True
        return False

    def keyPressEvent(self, event):
        if not self.handle_global_key_press(event):
            super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if not self.handle_global_key_release(event):
            super().keyReleaseEvent(event)

    def init_ui(self):
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter = self.splitter
        self.setCentralWidget(splitter)
        splitter.setContentsMargins(20, 20, 20, 20)
        splitter.setHandleWidth(8)

        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setSpacing(15)

        self.video_label = QLabel("请打开媒体文件")
        self.video_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.video_label.setMinimumSize(320, 320)
        left_layout.addWidget(self.video_label)

        self.lrc_list = QListWidget()
        self.lrc_list.setFixedHeight(110)
        self.lrc_list.setVisible(False)
        left_layout.addWidget(self.lrc_list)

        control_layout = QVBoxLayout()
        control_layout.setSpacing(8)

        row1 = QHBoxLayout()
        row1.setSpacing(8)
        self.btn_open = QPushButton("打开音视频")
        self.btn_lyric = QPushButton("上传LRC歌词")
        self.cbx_lyrics_meta = QCheckBox("读取内嵌歌词")
        self.cbx_lyrics_meta.setToolTip("勾选后优先从音频文件元数据中读取内嵌歌词")
        self.cbx_lyrics_meta.setChecked(self.lyrics_from_metadata)
        self.btn_sub = QPushButton("加载SRT字幕")
        self.btn_play = QPushButton("播放/暂停")
        self.btn_stop = QPushButton("停止")
        self.btn_loop = QPushButton("单曲循环")
        self.btn_open_folder = QPushButton("打开文件夹")
        self.btn_set_cover = QPushButton("设置默认封面")

        self.cbx_speed = QComboBox()
        self.cbx_speed.addItems(["0.5x", "0.7x", "1.0x", "1.2x", "1.5x", "2.0x"])
        self.cbx_speed.setCurrentText("1.0x")

        row1.addWidget(self.btn_open)
        row1.addWidget(self.btn_lyric)
        row1.addWidget(self.cbx_lyrics_meta)
        row1.addWidget(self.btn_sub)
        row1.addWidget(self.btn_play)
        row1.addWidget(self.btn_stop)
        row1.addWidget(self.btn_loop)
        row1.addWidget(self.btn_open_folder)
        row1.addWidget(self.btn_set_cover)
        row1.addWidget(QLabel("倍速"))
        row1.addWidget(self.cbx_speed)
        row1.addStretch()

        row2 = QHBoxLayout()
        row2.setSpacing(10)
        row2.addWidget(QLabel("进度"))
        self.slider_pos = QSlider(Qt.Orientation.Horizontal)
        row2.addWidget(self.slider_pos, stretch=5)
        row2.addWidget(QLabel("音量"))
        self.slider_vol = QSlider(Qt.Orientation.Horizontal)
        self.slider_vol.setRange(0, 100)
        self.slider_vol.setValue(80)
        row2.addWidget(self.slider_vol, stretch=1)
        self.btn_fullscreen = QPushButton("全屏")
        self.btn_fullscreen.setToolTip("全屏播放视频/封面，按 F 或双击画面进入，按 ESC 退出")
        row2.addWidget(self.btn_fullscreen)

        self.btn_open.clicked.connect(self.open_media)
        self.btn_lyric.clicked.connect(lambda: self.load_lrc_file())
        self.cbx_lyrics_meta.toggled.connect(self.set_lyrics_meta_enabled)
        self.btn_sub.clicked.connect(self.load_subtitle)
        self.btn_play.clicked.connect(self.play_pause)
        self.btn_stop.clicked.connect(self.stop_play)
        self.btn_loop.clicked.connect(self.toggle_loop)
        self.btn_open_folder.clicked.connect(self.open_file_folder)
        self.btn_set_cover.clicked.connect(self.set_custom_default_cover)
        self.btn_fullscreen.clicked.connect(self.toggle_fullscreen)
        self.cbx_speed.currentTextChanged.connect(self.set_play_speed)
        self.slider_pos.sliderMoved.connect(self.seek_pos)
        self.slider_vol.valueChanged.connect(self.set_volume)

        control_layout.addLayout(row1)
        control_layout.addLayout(row2)
        left_layout.addLayout(control_layout)

        self.right_widget = QWidget()
        right_widget = self.right_widget
        right_layout = QVBoxLayout(right_widget)
        right_layout.setSpacing(8)

        # 均衡器：置于最上方，点开独立弹窗
        self.btn_equalizer = QPushButton("均衡器")
        self.btn_equalizer.setToolTip("点击打开均衡器窗口")
        self.btn_equalizer.clicked.connect(self.open_equalizer_dialog)
        right_layout.addWidget(self.btn_equalizer)

        # 右侧分类标签页：串流、音乐库、信息、标签
        # 外观保持 Qt 原生，不做自定义绘制
        self.right_tabs = QTabWidget()
        self.right_tabs.setDocumentMode(True)

        # ---- 标签页 1：串流 / 设置 ----
        tab_stream = QWidget()
        ts_layout = QVBoxLayout(tab_stream)
        ts_layout.setSpacing(8)

        stream_label = QLabel("网络串流/列表:")
        self.stream_url_input = QComboBox()
        self.stream_url_input.setEditable(True)
        self.stream_url_input.addItems(load_stream_history())
        self.stream_url_input.setCurrentIndex(-1)
        self.stream_url_input.setPlaceholderText(
            "串流/播放列表 URL，或本地 .m3u/.m3u8 路径（GitHub 链接自动转 raw）"
        )
        ts_layout.addWidget(stream_label)
        ts_layout.addWidget(self.stream_url_input)

        self.btn_stream = QPushButton("播放串流")
        self.btn_stream.clicked.connect(self.play_stream)
        ts_layout.addWidget(self.btn_stream)

        self.btn_m3u = QPushButton("上传M3U/M3U8文件")
        self.btn_m3u.setToolTip("选择本地M3U/M3U8播放列表，列出电视台点台播放")
        self.btn_m3u.clicked.connect(self.load_m3u_file)
        ts_layout.addWidget(self.btn_m3u)

        self.btn_tg = QPushButton("Telegram 音乐")
        self.btn_tg.setToolTip("登录 Telegram，抓取群组/频道音乐并生成播放列表（需 pip install telethon）")
        self.btn_tg.clicked.connect(self.open_telegram_music_dialog)
        ts_layout.addWidget(self.btn_tg)

        ts_layout.addStretch()

        # 设置项并入本标签页
        self.btn_scale = QPushButton("缩放")
        self.btn_scale.setToolTip("点击设置界面缩放比例")
        self.btn_scale.clicked.connect(self.open_scale_dialog)
        ts_layout.addWidget(self.btn_scale)

        stream_tab_index = self.right_tabs.addTab(tab_stream, "串流")
        self.right_tabs.setTabToolTip(
            stream_tab_index, "网络串流 / 设置（含 M3U、Telegram 音乐、缩放）"
        )

        # ---- 标签页 2：音乐库（指定文件夹扫描 + SHA256 去重） ----
        tab_library = QWidget()
        tl_layout = QVBoxLayout(tab_library)
        tl_layout.setSpacing(8)
        tl_layout.setContentsMargins(0, 6, 0, 0)

        folder_row = QHBoxLayout()
        folder_row.setSpacing(6)
        self.btn_library_add = QPushButton("添加文件夹")
        self.btn_library_add.setToolTip("选择要扫描的音乐文件夹（可添加多个）")
        self.btn_library_remove = QPushButton("移除文件夹")
        self.btn_library_remove.setToolTip("从待扫描列表移除当前文件夹（不会删除磁盘文件）")
        folder_row.addWidget(self.btn_library_add)
        folder_row.addWidget(self.btn_library_remove)
        tl_layout.addLayout(folder_row)

        self.library_folder_combo = QComboBox()
        self.library_folder_combo.setEditable(True)
        self.library_folder_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.library_folder_combo.setToolTip("待扫描的文件夹；扫描时全部合并，并按文件内容去重")
        tl_layout.addWidget(self.library_folder_combo)

        scan_row = QHBoxLayout()
        scan_row.setSpacing(6)
        self.btn_library_scan = QPushButton("扫描")
        self.btn_library_scan.setToolTip("递归扫描以上文件夹，按 SHA256 校验内容去重后加入列表")
        self.btn_library_stop = QPushButton("停止")
        self.btn_library_stop.setEnabled(False)
        scan_row.addWidget(self.btn_library_scan)
        scan_row.addWidget(self.btn_library_stop)
        scan_row.addStretch()
        tl_layout.addLayout(scan_row)

        option_row = QHBoxLayout()
        option_row.setSpacing(6)
        self.cbx_library_dedup = QCheckBox("SHA256去重")
        self.cbx_library_dedup.setChecked(True)
        self.cbx_library_dedup.setToolTip("勾选：内容相同的文件只保留一首；取消：仅按文件路径去重")
        self.cbx_library_min_duration = QComboBox()
        for label, seconds in MIN_DURATION_CHOICES:
            self.cbx_library_min_duration.addItem(label, seconds)
        self.cbx_library_min_duration.setToolTip(
            "短于该时长的文件不入库（多是系统音效/提示音）；\n"
            "无法读取时长的文件一律保留。\n"
            "只是不加入列表，不会删除磁盘文件。"
        )
        saved_min = load_min_duration()
        min_index = self.cbx_library_min_duration.findData(saved_min)
        if min_index < 0:
            min_index = self.cbx_library_min_duration.findData(DEFAULT_MIN_DURATION)
        self.cbx_library_min_duration.setCurrentIndex(max(0, min_index))
        option_row.addWidget(self.cbx_library_dedup)
        option_row.addWidget(QLabel("短音频"))
        option_row.addWidget(self.cbx_library_min_duration)
        option_row.addStretch()
        tl_layout.addLayout(option_row)

        self.library_search = QLineEdit()
        self.library_search.setPlaceholderText("搜索标题/艺术家/路径…")
        tl_layout.addWidget(self.library_search)

        self.music_list = QListWidget()
        self.music_list.setToolTip("双击播放；右键可播放 / 打开所在文件夹 / 移除")
        self.music_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        tl_layout.addWidget(self.music_list, stretch=1)

        list_row = QHBoxLayout()
        list_row.setSpacing(6)
        self.btn_library_remove_item = QPushButton("移除选中")
        self.btn_library_clear = QPushButton("清空列表")
        self.btn_library_dups = QPushButton("跳过项")
        self.btn_library_dups.setToolTip("查看上次扫描中因内容重复或时长过短而未入库的文件")
        list_row.addWidget(self.btn_library_remove_item)
        list_row.addWidget(self.btn_library_clear)
        list_row.addWidget(self.btn_library_dups)
        list_row.addStretch()
        tl_layout.addLayout(list_row)

        self.library_status = QLabel("尚未扫描")
        self.library_status.setWordWrap(True)
        tl_layout.addWidget(self.library_status)

        self.btn_library_add.clicked.connect(self.choose_library_folder)
        self.btn_library_remove.clicked.connect(self.remove_library_folder)
        self.btn_library_scan.clicked.connect(self.start_music_scan)
        self.btn_library_stop.clicked.connect(self.stop_music_scan)
        self.library_search.textChanged.connect(self.filter_music_list)
        self.music_list.itemDoubleClicked.connect(self.play_selected_music)
        self.music_list.customContextMenuRequested.connect(self.on_music_list_menu)
        self.btn_library_remove_item.clicked.connect(self.remove_selected_music)
        self.btn_library_clear.clicked.connect(self.clear_music_library)
        self.btn_library_dups.clicked.connect(self.show_skipped_records)

        library_tab_index = self.right_tabs.addTab(tab_library, "音乐库")
        self.right_tabs.setTabToolTip(
            library_tab_index, "音乐库：文件夹扫描、SHA256 去重、搜索与播放"
        )

        # ---- 标签页 3：媒体信息 ----
        self.tab_info = QWidget()
        ti_layout = QVBoxLayout(self.tab_info)
        ti_layout.setContentsMargins(0, 6, 0, 0)
        self.info_panel = QListWidget()
        ti_layout.addWidget(self.info_panel)
        info_tab_index = self.right_tabs.addTab(self.tab_info, "信息")
        self.right_tabs.setTabToolTip(info_tab_index, "媒体信息：当前播放内容的详细参数")

        # ---- 标签页 4：标签编辑（仅正在播放的音乐文件） ----
        tag_tab_index = self.right_tabs.addTab(self.build_tag_editor_tab(), "标签")
        self.right_tabs.setTabToolTip(
            tag_tab_index, "标签编辑：修改正在播放音乐的元数据并写回文件"
        )

        right_layout.addWidget(self.right_tabs, stretch=1)

        # 右下角：工作室徽标 + GPLv3 徽标
        gpl_layout = QHBoxLayout()
        gpl_layout.addStretch()

        self.qrstudio_label = QLabel()
        qrstudio_icon_path = os.path.join(os.path.dirname(__file__), "qrstudio-icon.png")
        if os.path.exists(qrstudio_icon_path):
            pixmap = QPixmap(qrstudio_icon_path).scaled(
                136, 68,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self.qrstudio_label.setPixmap(pixmap)
            self.qrstudio_label.setCursor(Qt.CursorShape.PointingHandCursor)
            self.qrstudio_label.mousePressEvent = self.open_qrstudio_link
        gpl_layout.addWidget(self.qrstudio_label)

        self.gpl_label = QLabel()
        gpl_logo_path = os.path.join(os.path.dirname(__file__), "gplv3-with-text-136x68.png")
        if os.path.exists(gpl_logo_path):
            self.gpl_label.setPixmap(QPixmap(gpl_logo_path))
            self.gpl_label.setCursor(Qt.CursorShape.PointingHandCursor)
            self.gpl_label.mousePressEvent = self.open_gpl_link
        gpl_layout.addWidget(self.gpl_label)
        right_layout.addLayout(gpl_layout)

        splitter.addWidget(left_widget)
        splitter.addWidget(right_widget)

        # 恢复上次保存的扫描文件夹与音乐库列表
        self.populate_saved_music_library()

    def adapt_layout_to_screen(self):
        """根据屏幕分辨率自适应：窗口尺寸、封面大小、左右 7:3 比例、字号、缩放"""
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        screen_scale = min(geo.width() / 1920.0, geo.height() / 1080.0)
        ui_scale = self.ui_scale

        # 窗口默认尺寸：可用区域的 ~90%（若未最大化）
        w, h = int(geo.width() * 0.90), int(geo.height() * 0.90)
        self.resize(w, h)
        self.move(geo.center().x() - w // 2, geo.center().y() - h // 2)

        # 左侧封面区域随屏幕与缩放比例调整（最小 320）
        self.video_label.setMinimumSize(
            max(320, int(geo.width() * 0.34 * ui_scale)),
            max(320, int(geo.height() * 0.46 * ui_scale)),
        )
        # 歌词栏高度随屏幕与缩放比例调整
        self.lrc_list.setFixedHeight(max(90, int(geo.height() * 0.12 * ui_scale)))
        # 右侧面板最小宽度，避免过窄
        self.right_widget.setMinimumWidth(max(150, int(geo.width() * 0.14 * ui_scale)))

        # 左右比例：封面控制区 75% : 媒体信息区 25%（介于 7:3 ~ 8:2）
        self._apply_splitter_ratio()

        # 字号自适应（1920x1080 基准 13px，叠加屏幕缩放与用户缩放）；
        # 控件外观保持 Qt 原生，这里只调整字号
        font_size = max(10, min(28, round(13 * screen_scale * ui_scale)))
        if w * ui_scale < 1280:
            # 窄屏下压缩按钮内边距，避免左侧按钮行把比例撑出 8:2
            self.setStyleSheet(
                f"QWidget {{ font-size: {font_size}px; }} "
                f"QPushButton {{ padding: 2px 4px; }}"
            )
        else:
            self.setStyleSheet(f"QWidget {{ font-size: {font_size}px; }}")

    def _apply_splitter_ratio(self):
        """按当前窗口宽度应用左右 75:25 比例（减左右边距）"""
        total = self.width() - 40  # 左右各 20px 边距
        if total <= 0:
            total = self.width()
        if total > 0:
            self.splitter.setSizes([int(total * 0.75), int(total * 0.25)])

    def showEvent(self, event):
        super().showEvent(event)
        # 窗口显示后（含最大化）确保左右比例落在 7:3 ~ 8:2
        QTimer.singleShot(0, self._apply_splitter_ratio)

    def open_equalizer_dialog(self):
        """打开均衡器窗口（操作真实生效，非假窗口）"""
        if self.equalizer_dialog is None:
            self.equalizer_dialog = EqualizerDialog(self)
        self.equalizer_dialog.show()
        self.equalizer_dialog.raise_()
        self.equalizer_dialog.activateWindow()

    def open_scale_dialog(self):
        """打开界面缩放设置窗口"""
        if self.scale_dialog is None:
            self.scale_dialog = ScaleDialog(self)
        else:
            # 再次打开时同步当前缩放值
            self.scale_dialog.slider.setValue(int(round(self.ui_scale * 100)))
        self.scale_dialog.show()
        self.scale_dialog.raise_()
        self.scale_dialog.activateWindow()

    def apply_ui_scale(self):
        """应用当前缩放比例：字号与控件尺寸即时生效并保存"""
        save_ui_scale(self.ui_scale)
        self.adapt_layout_to_screen()

    def open_gpl_link(self, event):
        """打开 GPLv3 许可证页面"""
        QDesktopServices.openUrl(QUrl("https://www.gnu.org/licenses/gpl-3.0"))

    def open_qrstudio_link(self, event):
        """打开项目主页"""
        QDesktopServices.openUrl(QUrl("https://github.com/liqirui1145-create/RuiPlayer"))

    def toggle_equalizer(self, enabled):
        self.equalizer_enabled = enabled
        self.apply_equalizer_to_player()

    def apply_equalizer_to_player(self):
        if not hasattr(self, "media_player"):
            return
        if not self.equalizer_enabled:
            self.media_player.set_equalizer(None)
            return
        eq = vlc.AudioEqualizer()
        eq.set_preamp(self.equalizer_preamp)
        for idx, amp in enumerate(self.equalizer_bands):
            eq.set_amp_at_index(float(amp), idx)
        self.media_player.set_equalizer(eq)

    def load_default_cover_from_file(self):
        cover_path = os.path.join(os.path.dirname(__file__), self.DEFAULT_COVER_FILENAME)
        if os.path.exists(cover_path):
            try:
                self.custom_default_cover = QPixmap(cover_path)
            except Exception:
                self.custom_default_cover = None

    def show_default_cover(self):
        if self.custom_default_cover is not None:
            self.video_label.setPixmap(
                self.custom_default_cover.scaled(
                    self.video_label.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        else:
            size = 550
            pix = QPixmap(size, size)
            pix.fill(QColor("#2c3e50"))
            painter = QPainter(pix)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QColor("#ffffff"))
            painter.setFont(QFont("微软雅黑", 20, QFont.Weight.Bold))
            painter.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, "🎵 暂无封面\n请点击【设置默认封面】上传图片")
            painter.end()
            self.video_label.setPixmap(pix)

    def set_custom_default_cover(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "选择默认封面图片",
            "",
            "图片文件 (*.jpg *.jpeg *.png *.bmp *.gif)",
        )
        if not file_path:
            return
        try:
            self.custom_default_cover = QPixmap(file_path)
            QMessageBox.information(self, "设置成功", "默认封面已更换！")
            if self.cur_media_path and not self.is_video:
                self.show_default_cover()
        except Exception:
            QMessageBox.warning(self, "加载失败", "图片格式错误或文件损坏！")

    def load_audio_cover(self, file_path):
        try:
            if not MutagenFile:
                self.show_default_cover()
                return
            audio = MutagenFile(file_path)
            cover_data = None
            if isinstance(audio, MP3):
                for tag in audio.tags.values():
                    if getattr(tag, "FrameID", None) == "APIC":
                        cover_data = tag.data
                        break
            elif isinstance(audio, FLAC):
                for pic in audio.pictures:
                    cover_data = pic.data
            if cover_data:
                img = Image.open(BytesIO(cover_data)).convert("RGBA")
                qimg = QImage(img.tobytes(), img.width, img.height, QImage.Format.Format_RGBA8888)
                self.video_label.setPixmap(
                    QPixmap.fromImage(qimg).scaled(
                        self.video_label.size(),
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
            else:
                self.show_default_cover()
        except Exception:
            self.show_default_cover()

    def open_file_folder(self):
        if self.cur_media_path and os.path.exists(self.cur_media_path):
            os.startfile(os.path.dirname(self.cur_media_path))

    def delete_current_media(self):
        self.close_fullscreen_on_media_change()
        if not self.cur_media_path or not os.path.exists(self.cur_media_path):
            return
        reply = QMessageBox.question(
            self,
            "删除确认",
            f"确定永久删除该文件？\n{os.path.basename(self.cur_media_path)}\n操作无法撤销！",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            try:
                self.media_player.stop()
                os.remove(self.cur_media_path)
                self.reset_player_state()
            except Exception:
                QMessageBox.warning(self, "删除失败", "文件被占用、权限不足或已删除！")

    def reset_player_state(self):
        self.cur_media_path = ""
        self.video_label.clear()
        self.video_label.setText("请打开媒体文件")
        self.lrc_list.clear()
        self.lrc_list.hide()
        self.info_panel.clear()
        self.refresh_tag_editor("")
        self.sync_mpris_state()

    def toggle_loop(self):
        self.loop_single = not self.loop_single
        self.btn_loop.setText("循环(开启)" if self.loop_single else "单曲循环")

    def _set_video_window(self, widget):
        """把 VLC 视频画面嵌入指定 widget，需按平台传入正确的窗口句柄。
        Windows → set_hwnd（HWND）；macOS → set_nsobject；
        Linux/X11(XWayland) → set_xwindow。set_hwnd 在 Linux 上无效，
        会导致“有声音无画面”，因此不能无条件调用。
        """
        win_id = int(widget.winId())
        if sys.platform.startswith("win"):
            self.media_player.set_hwnd(win_id)
        elif sys.platform == "darwin":
            self.media_player.set_nsobject(win_id)
        else:
            # Linux：仅 X11/XWayland 下可嵌入；Wayland 会话无法直接嵌入
            self.media_player.set_xwindow(win_id)

    def bind_video_window(self):
        """正常模式下把视频画面绑定到左侧 video_label"""
        self._set_video_window(self.video_label)

    # ---------------------- 全屏播放 ----------------------
    def toggle_fullscreen(self):
        """在普通/全屏之间切换（视频画面或音频封面铺满屏幕）"""
        if self.is_fullscreen:
            self.exit_fullscreen()
        else:
            self.enter_fullscreen()

    def _screen_display_name(self, screen):
        """显示器在下拉框里的显示文案"""
        name = screen.name() or "显示器"
        geo = screen.geometry()
        suffix = "（主屏）" if screen is QApplication.primaryScreen() else ""
        return f"{name}  {geo.width()}×{geo.height()}{suffix}"

    def choose_fullscreen_screen(self):
        """确定全屏使用哪块屏幕：
        - 单显示器：直接使用，不打扰；
        - 多显示器且已有保存的偏好：沿用（偏好屏幕不存在时重新询问）；
        - 多显示器首次：弹窗让用户选择并记住，之后自动沿用。
        """
        screens = QApplication.screens()
        if not screens:
            return None
        if len(screens) == 1:
            return screens[0]

        saved = load_fullscreen_screen()
        if saved:
            for s in screens:
                if s.name() == saved:
                    return s

        names = [self._screen_display_name(s) for s in screens]
        current = self.screen() or QApplication.primaryScreen()
        default_idx = screens.index(current) if current in screens else 0
        choice, ok = QInputDialog.getItem(
            self,
            "选择全屏显示器",
            "检测到多个显示器，请选择全屏显示在哪块屏幕\n（选择会被记住，下次自动沿用）：",
            names,
            default_idx,
            False,
        )
        chosen = screens[names.index(choice)] if (ok and choice in names) else (current or screens[0])
        if chosen is not None:
            save_fullscreen_screen(chosen.name())
        return chosen

    def _place_fullscreen_on_screen(self, screen):
        """把全屏窗口定位到指定显示器（只定位，不负责显示）。

        只把窗口几何设为目标屏幕的完整区域；不再调用 windowHandle().setScreen()，
        以避免对已全屏的原生窗口设置屏幕时在某些平台触发崩溃（SIGABRT）。
        """
        fs = self.fullscreen_window
        if screen is None or fs is None:
            return
        try:
            fs.setGeometry(screen.geometry())
        except Exception:
            pass

    def _open_fullscreen_window(self, screen):
        """在指定屏幕创建/显示全屏窗口，并绑定画面、显示控制栏"""
        if self.fullscreen_window is None:
            self.fullscreen_window = FullscreenWindow(self)
        fs = self.fullscreen_window
        if self.lrc_lines:
            fs.set_lyrics(self.lrc_lines)
        self._place_fullscreen_on_screen(screen)
        # 先显示再绑定，确保全屏窗口的原生句柄有效
        fs.showFullScreen()
        if self.is_video:
            # 视频：VLC 重绑到全屏窗口，实现画面铺满
            fs.label.show()
            self._set_video_window(fs.label)
        else:
            # 音频：把封面放大铺到全屏窗口
            self.update_fullscreen_art()
        fs.raise_()
        fs.activateWindow()
        fs.setFocus()
        fs.on_enter_fullscreen()

    def move_fullscreen_to(self, screen):
        """切换到另一块显示器：在新屏幕上重建全屏窗口（保持播放）"""
        if not self.is_fullscreen or screen is None:
            return
        old = self.fullscreen_window
        if old is not None:
            old.on_exit_fullscreen()
            old.hide()
            old.deleteLater()
            self.fullscreen_window = None
        self._open_fullscreen_window(screen)

    def open_screen_chooser(self):
        """弹出“更改全屏显示器”窗口，选择后立即应用并记住。"""
        screens = QApplication.screens()
        if not screens:
            return
        names = [self._screen_display_name(s) for s in screens]

        # 默认项：优先当前全屏所在屏幕，其次已保存偏好，再次主窗口所在屏幕
        current = None
        if self.is_fullscreen and self.fullscreen_window is not None:
            current = self.fullscreen_window.screen()
        saved = load_fullscreen_screen()
        default_idx = 0
        for i, s in enumerate(screens):
            if (current is not None and s is current) or (saved and s.name() == saved):
                default_idx = i
                break

        # 全屏时以全屏窗口为父对象，保证弹窗处在最上层、不被全屏画面遮住
        parent = (
            self.fullscreen_window
            if self.is_fullscreen and self.fullscreen_window is not None
            else self
        )
        prompt = (
            "检测到多个显示器，请选择全屏显示在哪块屏幕："
            if len(screens) > 1
            else "当前仅检测到一个显示器："
        )
        choice, ok = QInputDialog.getItem(
            parent, "更改全屏显示器", prompt, names, default_idx, False
        )
        if not ok or choice not in names:
            return

        target = screens[names.index(choice)]
        save_fullscreen_screen(target.name())
        if self.is_fullscreen and self.fullscreen_window is not None:
            self.move_fullscreen_to(target)
        if self.fullscreen_window is not None:
            self.fullscreen_window.controls.refresh_monitor_button()

    def enter_fullscreen(self):
        if self.is_fullscreen:
            return
        self.is_fullscreen = True

        # 多显示器：先确定目标屏幕（首次询问并记住）
        screen = self.choose_fullscreen_screen()
        self._open_fullscreen_window(screen)

    def update_fullscreen_art(self):
        """全屏（音频）时按当前封面刷新画面，避免变形并随窗口尺寸自适应"""
        if not self.is_fullscreen or self.fullscreen_window is None:
            return
        fs = self.fullscreen_window
        fs.label.setText("")
        pix = self.video_label.pixmap()
        if pix is not None and not pix.isNull():
            fs.label.setPixmap(
                pix.scaled(
                    fs.label.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        else:
            fs.label.setPixmap(QPixmap())
            fs.label.setText("请打开媒体文件")

    def exit_fullscreen(self):
        if not self.is_fullscreen:
            return
        self.is_fullscreen = False
        fs = self.fullscreen_window
        if fs is not None:
            fs.on_exit_fullscreen()
            fs.hide()
        if self.is_video:
            # 恢复绑定回普通窗口，播放不中断
            self._set_video_window(self.video_label)
        self.activateWindow()
        self.setFocus()

    def close_fullscreen_on_media_change(self):
        """切换媒体/停止播放时若处于全屏则退出，避免状态错乱"""
        if self.is_fullscreen:
            self.exit_fullscreen()

    def set_play_speed(self, text):
        self.cur_speed = float(text.replace("x", ""))
        self.media_player.set_rate(self.cur_speed)

    def load_subtitle(self):
        path, _ = QFileDialog.getOpenFileName(self, "加载SRT字幕", "", "*.srt")
        if path:
            self.media_player.subtitle_set_file(path)

    def parse_lrc(self, lrc_text):
        reg = re.compile(r"\[(\d+):(\d+)\.(\d+)\]")
        times, lyrics = [], []
        for line in lrc_text.splitlines():
            line = line.strip()
            matches = reg.findall(line)
            lyric = reg.sub("", line).strip()
            if matches and lyric:
                for m, s, ms in matches:
                    times.append(int(m) * 60000 + int(s) * 1000 + int(ms))
                    lyrics.append(lyric)
        combined = sorted(zip(times, lyrics))
        self.lrc_time_list, self.lrc_lines = zip(*combined) if combined else ([], [])

    def set_lyrics(self, text):
        """解析并应用歌词文本（列表 + 全屏覆盖层），成功返回 True"""
        text = (text or "").strip()
        if not text:
            return False
        self.parse_lrc(text)
        if not self.lrc_lines:
            return False
        self.cur_lrc_idx = -1
        self.lrc_list.clear()
        self.lrc_list.addItems(self.lrc_lines)
        self.lrc_list.setVisible(True)
        if self.fullscreen_window is not None:
            self.fullscreen_window.set_lyrics(self.lrc_lines)
        return True

    def clear_lyrics(self):
        """清空当前歌词（列表与全屏）"""
        self.lrc_time_list, self.lrc_lines = [], []
        self.cur_lrc_idx = -1
        self.lrc_list.clear()
        self.lrc_list.setVisible(False)
        if self.fullscreen_window is not None:
            self.fullscreen_window.clear_lyrics()

    def _read_text_file(self, path):
        """尝试多种编码读取文本文件，失败返回 None"""
        for enc in ("utf-8-sig", "utf-8", "gbk", "latin-1"):
            try:
                with open(path, "r", encoding=enc) as f:
                    return f.read()
            except Exception:
                continue
        return None

    def try_load_lyrics_for(self, path):
        """打开媒体时按用户偏好加载歌词：
        勾选“读取内嵌歌词” → 优先读文件元数据，其次同目录同名 .lrc；
        未勾选 → 只读同目录同名 .lrc。
        """
        self.clear_lyrics()
        if self.is_video:
            return
        if self.lyrics_from_metadata:
            try:
                text = get_embedded_lyrics(path)
            except Exception:
                text = None
            if text and self.set_lyrics(text):
                return
        media_dir = os.path.dirname(path)
        media_name = os.path.splitext(os.path.basename(path))[0]
        lrc_path = os.path.join(media_dir, f"{media_name}.lrc")
        if os.path.exists(lrc_path):
            text = self._read_text_file(lrc_path)
            if text:
                self.set_lyrics(text)

    def load_lrc_file(self, parent=None):
        """上传 .lrc 歌词文件（parent 可为全屏窗口，保证对话框在最上层）"""
        path, _ = QFileDialog.getOpenFileName(
            parent or self, "选择LRC歌词", "", "歌词文件 (*.lrc *.LRC)"
        )
        if not path:
            return
        text = self._read_text_file(path)
        if not text:
            QMessageBox.warning(self, "读取失败", "无法读取歌词文件（编码不支持或文件损坏）")
            return
        if not self.set_lyrics(text):
            QMessageBox.information(self, "提示", "未在该文件中解析到歌词内容")

    def toggle_fullscreen_lyrics(self):
        """显示/隐藏全屏歌词"""
        if self.fullscreen_window is not None:
            self.fullscreen_window.toggle_lyrics()

    def set_lyrics_meta_enabled(self, enabled):
        """勾选/取消“从元数据读取内嵌歌词”，并即时生效"""
        self.lyrics_from_metadata = bool(enabled)
        save_lyrics_meta_pref(self.lyrics_from_metadata)
        if hasattr(self, "cbx_lyrics_meta") and self.cbx_lyrics_meta.isChecked() != bool(enabled):
            self.cbx_lyrics_meta.blockSignals(True)
            self.cbx_lyrics_meta.setChecked(bool(enabled))
            self.cbx_lyrics_meta.blockSignals(False)
        if self.fullscreen_window is not None:
            cbx = self.fullscreen_window.controls.cbx_lyrics
            cbx.blockSignals(True)
            cbx.setChecked(bool(enabled))
            cbx.blockSignals(False)
        # 勾选后立即为当前文件尝试读取内嵌歌词
        if enabled and self.cur_media_path and not self.is_video:
            try:
                text = get_embedded_lyrics(self.cur_media_path)
            except Exception:
                text = None
            if text:
                self.set_lyrics(text)

    def read_audio_metadata(self, file_path):
        self.audio_metadata = {k: "--" for k in self.audio_metadata}
        if not MutagenFile or self.is_video:
            return
        try:
            audio = MutagenFile(file_path)
            if audio:
                if hasattr(audio.info, "sample_rate"):
                    self.audio_metadata["sample_rate"] = f"{audio.info.sample_rate} Hz"
                if hasattr(audio.info, "channels"):
                    self.audio_metadata["channels"] = f"{audio.info.channels} 声道"
                if hasattr(audio.info, "bitrate"):
                    self.audio_metadata["bitrate"] = f"{audio.info.bitrate // 1000} kbps"
                tags = audio.tags
                if tags:
                    self.audio_metadata["title"] = get_tag_text(tags, "title") or "--"
                    self.audio_metadata["artist"] = get_tag_text(tags, "artist") or "--"
                    self.audio_metadata["album"] = get_tag_text(tags, "album") or "--"
        except Exception:
            pass

    def show_media_info(self, path, display_name=None):
        self.info_panel.clear()
        try:
            duration = self.media_player.get_length()
        except Exception:
            duration = -1
        dur = f"{duration // 60000}:{duration % 60000 // 1000:02d}" if duration > 0 else "未知"
        try:
            vw = self.media_player.video_get_width()
            vh = self.media_player.video_get_height()
        except Exception:
            vw = vh = 0
        res = f"{vw}×{vh}" if self.is_video and vw > 0 else "纯音频"

        if path.startswith(("http://", "https://", "rtsp://", "rtmp://", "udp://", "tcp://")):
            items = [
                f"1. 类型：网络串流",
                f"2. 地址：{path}",
                f"3. 时长：{dur}",
                f"4. 分辨率：{res}",
                f"5. 倍速：{self.cur_speed}x",
            ]
            if display_name:
                items.insert(1, f"2. 频道：{display_name}")
        else:
            try:
                stat = os.stat(path)
                items = [
                    f"1. 文件名：{os.path.basename(path)[:15]}",
                    f"2. 标题：{self.audio_metadata['title'][:15]}",
                    f"3. 格式：{os.path.splitext(path)[1][1:].upper()}",
                    f"4. 大小：{stat.st_size / 1024 / 1024:.2f} MB",
                    f"5. 修改时间：{datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M')}",
                    f"6. 时长：{dur}",
                    f"7. 分辨率：{res}",
                    f"8. 音频码率：{self.audio_metadata['bitrate']}",
                    f"9. 声道：{self.audio_metadata['channels']}",
                    f"10. 采样率：{self.audio_metadata['sample_rate']}",
                    f"11. 艺术家：{self.audio_metadata['artist'][:15]}",
                    f"12. 专辑：{self.audio_metadata['album'][:15]}",
                    f"13. 倍速：{self.cur_speed}x",
                ]
            except Exception:
                items = [f"1. 文件：{os.path.basename(path)[:15]}", f"2. 时长：{dur}", f"3. 倍速：{self.cur_speed}x"]
        self.info_panel.addItems(items)

    def play_stream(self):
        raw = (self.stream_url_input.currentText() or "").strip()
        if not raw:
            QMessageBox.warning(self, "输入错误", "请输入网络串流地址或播放列表路径！")
            return

        target = normalize_stream_url(raw)

        # 1) 本地文件：.m3u/.m3u8 走播放列表，其它媒体直接打开
        local_path = self._local_path_from_url(target)
        if local_path and os.path.exists(local_path):
            self._remember_stream_url(raw)
            self._open_local_playlist_or_media(local_path)
            return

        # 2) 网络地址
        if not re.match(r"^(https?|rtsp|rtmp|udp|tcp)://", target, re.IGNORECASE):
            QMessageBox.warning(
                self, "格式错误",
                "请输入有效的地址（如 http://、https://、rtsp://）"
                "或本地 .m3u/.m3u8 文件路径。",
            )
            return

        self._remember_stream_url(raw)
        if target != raw:
            self.stream_url_input.setEditText(target)

        # m3u/m3u8 链接：先下载解析（HLS 会直接播放）
        if self.is_m3u_url(target):
            self.load_m3u_playlist(target)
            return
        self._play_stream_url(target)

    def _remember_stream_url(self, url):
        """记住最近使用的串流地址（供下拉框快速选择）"""
        history = [h for h in load_stream_history() if h != url]
        history.insert(0, url)
        save_stream_history(history[:10])

    @staticmethod
    def _local_path_from_url(url):
        """把 file:// URL 或本地路径转成本地文件路径；网络地址返回 None"""
        if not url:
            return None
        if url.startswith("file://"):
            return QUrl(url).toLocalFile() or None
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", url):
            return None
        return url

    def _open_local_playlist_or_media(self, path):
        """本地文件：m3u/m3u8 解析为频道列表；HLS 或其它媒体直接播放"""
        ext = os.path.splitext(path)[1].lower()
        if ext in (".m3u", ".m3u8"):
            text = self._read_text_file(path)
            if text is None:
                QMessageBox.warning(self, "读取失败", "无法读取该播放列表文件。")
                return
            if looks_like_hls(text):
                # HLS 本地文件：交给 VLC 直接播放
                self._play_stream_url(Path(path).as_uri())
                return
            channels = self.parse_m3u_content(text, Path(path).as_uri())
            if channels:
                self.open_channel_dialog(channels)
            else:
                QMessageBox.warning(
                    self, "提示",
                    "未在该文件中解析到频道；若为 HLS 单片流请直接填播放地址。",
                )
            return
        self.open_media_from_path(path)

    # ====================== M3U / M3U8 电视台列表 ======================
    def is_m3u_url(self, url):
        """判断是否为 M3U/M3U8 播放列表链接"""
        lower = url.lower().split("?")[0].split("#")[0]
        return lower.endswith((".m3u", ".m3u8")) or ".m3u8" in lower or ".m3u?" in url.lower()

    def load_m3u_playlist(self, url):
        """异步下载并解析 M3U/M3U8 播放列表"""
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        request = QNetworkRequest(QUrl(url))
        request.setTransferTimeout(15000)
        reply = self.network_manager.get(request)
        reply.finished.connect(lambda: self.on_m3u_downloaded(reply))

    def on_m3u_downloaded(self, reply):
        QApplication.restoreOverrideCursor()
        try:
            if reply.error() != QNetworkReply.NetworkError.NoError:
                QMessageBox.warning(self, "加载失败", f"无法获取M3U列表：{reply.errorString()}")
                reply.deleteLater()
                return

            raw = bytes(reply.readAll())
            final_url = reply.url().toString()
            try:
                content_type = (
                    reply.header(QNetworkRequest.KnownHeaders.ContentTypeHeader) or ""
                ).lower()
            except Exception:
                content_type = ""
            reply.deleteLater()

            text = None
            for enc in ("utf-8-sig", "utf-8", "gbk", "latin-1"):
                try:
                    text = raw.decode(enc)
                    break
                except Exception:
                    continue
            if text is None:
                text = raw.decode("utf-8", errors="ignore")

            head = text.lstrip()[:64].lower()
            # 返回的是网页而不是原始播放列表（常见于 GitHub blob 链接）
            if "text/html" in content_type or head.startswith("<!doctype") or head.startswith("<html"):
                QMessageBox.warning(
                    self, "不是播放列表",
                    "该链接返回的是网页而不是原始播放列表。\n"
                    "如果来自 GitHub，请改用 raw 直链"
                    "（粘贴 blob 链接时本程序会自动转换）。",
                )
                return

            # HLS(.m3u8)：直接交给 VLC 播放，不当作频道列表
            if looks_like_hls(text):
                self._play_stream_url(final_url)
                return

            channels = self.parse_m3u_content(text, final_url)
            if channels:
                self.open_channel_dialog(channels)
            else:
                QMessageBox.warning(
                    self, "解析失败",
                    "未能从该链接解析出频道列表（可能不是 M3U 播放列表）。",
                )
        except Exception as e:
            QMessageBox.warning(self, "解析失败", str(e))

    def parse_m3u_content(self, text, base_url):
        """解析 M3U 文本，返回 [(频道名, 流地址), ...]"""
        channels = []
        lines = text.splitlines()
        i, n = 0, len(lines)
        while i < n:
            line = lines[i].strip()
            if line.startswith("#EXTINF"):
                name = self._extract_extinf_name(line)
                j = i + 1
                url_line = None
                while j < n:
                    cand = lines[j].strip()
                    if cand and not cand.startswith("#"):
                        url_line = cand
                        break
                    if cand.startswith("#EXTINF"):
                        break
                    j += 1
                if url_line:
                    channels.append((name, urljoin(base_url, url_line)))
                i = j if j < n else n
            else:
                i += 1
        return channels

    def _extract_extinf_name(self, line):
        """从 #EXTINF 行提取频道名，优先 tvg-name 属性"""
        name = ""
        m = re.search(r'tvg-name="([^"]*)"', line)
        if m and m.group(1).strip():
            name = m.group(1).strip()
        if not name and "," in line:
            name = line.split(",", 1)[1].strip()
        return name or "未知频道"

    def open_channel_dialog(self, channels):
        """打开电视台列表窗口"""
        if self.channel_dialog is None:
            self.channel_dialog = ChannelListDialog(self)
        self.channel_dialog.set_channels(channels)
        self.channel_dialog.show()
        self.channel_dialog.raise_()
        self.channel_dialog.activateWindow()

    def open_telegram_music_dialog(self):
        """打开 Telegram 群组/频道音乐抓取窗口"""
        if TelegramMusicDialog is None:
            QMessageBox.warning(
                self, "缺少依赖",
                "未安装 telethon。请先执行：pip install telethon",
            )
            return
        if self.tg_dialog is None:
            self.tg_dialog = TelegramMusicDialog(self)
        self.tg_dialog.show()
        self.tg_dialog.raise_()
        self.tg_dialog.activateWindow()

    def parse_m3u_file(self, path):
        """读取本地 M3U/M3U8 文件并解析频道列表（相对路径基于文件所在目录）"""
        with open(path, "rb") as f:
            raw = f.read()
        text = None
        for enc in ("utf-8", "gbk", "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except Exception:
                continue
        if text is None:
            text = raw.decode("utf-8", errors="ignore")
        base_url = Path(path).as_uri()  # file:///... 相对路径经 urljoin 拼为本地绝对路径
        return self.parse_m3u_content(text, base_url)

    def load_m3u_file(self):
        """上传本地 M3U/M3U8 文件，列出台目列表点台播放"""
        path, _ = QFileDialog.getOpenFileName(
            self, "选择M3U/M3U8播放列表", "",
            "M3U播放列表 (*.m3u *.m3u8)"
        )
        if not path:
            return
        try:
            channels = self.parse_m3u_file(path)
        except Exception as e:
            QMessageBox.warning(self, "读取失败", f"无法读取文件：{e}")
            return
        if not channels:
            QMessageBox.information(self, "提示", "未在文件中解析到频道")
            return
        self.open_channel_dialog(channels)

    def play_channel(self, name, url):
        """播放选中的频道/曲目：本地文件走本地播放，网络流走串流"""
        local_path = self._local_path_from_url(url)
        if local_path and os.path.exists(local_path):
            ext = os.path.splitext(local_path)[1].lower()
            if ext in (".m3u", ".m3u8"):
                self._open_local_playlist_or_media(local_path)
            else:
                self.open_media_from_path(local_path)
        else:
            self._play_stream_url(url, display_name=name)

    def _play_stream_url(self, url, display_name=None):
        """按地址播放单个网络串流（本地链接或频道流共用）"""
        self.close_fullscreen_on_media_change()
        if self.media_player.is_playing():
            self.media_player.stop()

        self.cur_media_path = url
        self.is_streaming = True
        self._set_stream_mode(True)
        self.clear_lyrics()
        self._stream_error_shown = False
        self._last_stream_state = None

        self.is_video = url.lower().endswith(STREAM_VIDEO_EXTS) or url.lower().startswith(("rtsp://", "rtmp://"))

        if self.is_video:
            self.bind_video_window()
            self.video_label.clear()
        else:
            self.show_default_cover()

        media = self.vlc_instance.media_new(url)
        self.media_player.set_media(media)
        result = self.media_player.play()
        self.media_player.set_rate(self.cur_speed)
        self.apply_equalizer_to_player()
        if result == -1:
            QMessageBox.critical(self, "播放失败", "无法播放该网络串流，请检查URL是否正确或网络是否正常")
            return
        self.show_media_info(url, display_name)
        self.update_mpris_track(url)
        self.sync_mpris_state()

    def open_media(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "打开媒体文件",
            "",
            "媒体文件(*.mp3 *.flac *.wav *.mp4 *.mkv *.avi *.mov *.mpeg *.mpg *.flv *.webm *.m4a *.aac *.ogg *.opus *.wma *.alac *.aiff *.ape)",
        )
        if not path:
            return

        self.close_fullscreen_on_media_change()
        self.cur_media_path = path
        self.is_streaming = False
        self._set_stream_mode(False)
        self.is_video = path.lower().endswith(LOCAL_VIDEO_EXTS)
        self.clear_lyrics()

        self.read_audio_metadata(path)
        if self.is_video:
            self.bind_video_window()
            self.video_label.clear()
        else:
            self.load_audio_cover(path)

        media = self.vlc_instance.media_new(path)
        self.media_player.set_media(media)
        self.media_player.play()
        self.media_player.set_rate(self.cur_speed)
        self.apply_equalizer_to_player()
        self.show_media_info(path)

        if not self.is_video:
            self.try_load_lyrics_for(path)
        self.refresh_tag_editor(path)
        self.update_mpris_track(path)

    def play_pause(self):
        if self.media_player.is_playing():
            self.media_player.pause()
        else:
            self.media_player.play()
            self.media_player.set_rate(self.cur_speed)
            self.apply_equalizer_to_player()
        self.sync_mpris_state()

    def stop_play(self):
        self.close_fullscreen_on_media_change()
        self.media_player.stop()
        self.slider_pos.setValue(0)
        self.sync_mpris_state()

    def seek_pos(self, val):
        self.media_player.set_time(val)
        if self.mpris is not None:
            self.mpris.notify_seeked(int(val) * 1000)

    def set_volume(self, vol):
        self.media_player.audio_set_volume(vol)
        if self.mpris is not None:
            self.mpris.update_volume(vol / 100.0)

    def update_progress_and_lrc(self):
        # 网络串流走低频 update_stream_status，避免高频调用 libvlc 导致 UI 卡顿
        if not self.cur_media_path or self.is_streaming:
            return
        cur_ms = self.media_player.get_time()
        total_ms = self.media_player.get_length()

        if total_ms > 0:
            self.slider_pos.setRange(0, total_ms)
            self.slider_pos.setValue(cur_ms)

        if self.lrc_time_list:
            target = -1
            for i, t in enumerate(self.lrc_time_list):
                if cur_ms >= t:
                    target = i
            if target != self.cur_lrc_idx:
                self.cur_lrc_idx = target
                if self.lrc_list.isVisible():
                    self.lrc_list.setCurrentRow(target)
                if self.fullscreen_window is not None:
                    self.fullscreen_window.update_lyrics(target)

        if self.loop_single and total_ms > 0 and cur_ms >= total_ms - 100:
            self.media_player.set_time(0)
            self.media_player.play()

        if self.is_fullscreen and self.fullscreen_window is not None:
            self.fullscreen_window.sync_from_player()

    def update_stream_status(self):
        """网络串流低频轮询：轻量检查状态，失败时给出一次提示"""
        if not self.cur_media_path or not self.is_streaming:
            return
        try:
            state = self.media_player.get_state()
        except Exception:
            state = None
        if state != self._last_stream_state:
            self._last_stream_state = state
            if state == vlc.State.Error and not self._stream_error_shown:
                self._stream_error_shown = True
                QMessageBox.warning(
                    self, "串流错误",
                    "无法播放该网络串流（链接可能已失效、需要特定网络，"
                    "或返回的是网页而非原始播放列表）。",
                )
        if self.is_fullscreen and self.fullscreen_window is not None:
            self.fullscreen_window.sync_from_player()

    def open_media_from_path(self, path):
        self.close_fullscreen_on_media_change()
        self.cur_media_path = path
        self.is_streaming = False
        self._set_stream_mode(False)
        self.is_video = path.lower().endswith(LOCAL_VIDEO_EXTS)
        self.clear_lyrics()
        self.read_audio_metadata(path)
        if self.is_video:
            self.bind_video_window()
            self.video_label.clear()
        else:
            self.load_audio_cover(path)

        media = self.vlc_instance.media_new(path)
        self.media_player.set_media(media)
        self.media_player.play()
        self.media_player.set_rate(self.cur_speed)
        self.apply_equalizer_to_player()
        self.show_media_info(path)
        if not self.is_video:
            self.try_load_lyrics_for(path)
        self.refresh_tag_editor(path)
        self.update_mpris_track(path)

    # ---------------- 音乐库：文件夹扫描 + SHA256 去重 ----------------

    def populate_saved_music_library(self):
        """把上次保存的扫描文件夹与音乐条目恢复到界面"""
        for folder in self.library_folder_history:
            if self.library_folder_combo.findText(folder) < 0:
                self.library_folder_combo.addItem(folder)
        if self.library_folder_combo.count() > 0:
            self.library_folder_combo.setCurrentIndex(0)
        for entry in list(self.library_entries.values()):
            self.add_music_item(entry)
        if self.music_list.count() > 1:
            self.music_list.sortItems()
        self.update_library_status()

    def library_folders(self):
        """当前待扫描文件夹列表（去重、保序，含手动输入的路径）"""
        folders = []
        for i in range(self.library_folder_combo.count()):
            folder = self.library_folder_combo.itemText(i).strip()
            if folder and folder not in folders:
                folders.append(folder)
        typed = self.library_folder_combo.currentText().strip()
        if typed and typed not in folders:
            folders.append(typed)
        return folders

    def choose_library_folder(self):
        """选择要扫描的音乐文件夹并加入下拉框"""
        start = self.library_folder_combo.currentText().strip()
        if not os.path.isdir(start):
            start = os.path.expanduser("~")
        folder = QFileDialog.getExistingDirectory(self, "选择音乐文件夹", start)
        if not folder:
            return
        folder = os.path.abspath(folder)
        if self.library_folder_combo.findText(folder) < 0:
            self.library_folder_combo.addItem(folder)
        self.library_folder_combo.setCurrentText(folder)
        save_music_folders(self.library_folders())
        self.update_library_status(f"已添加文件夹：{folder}")

    def remove_library_folder(self):
        """从下拉框移除当前文件夹（不删除磁盘文件）"""
        index = self.library_folder_combo.currentIndex()
        if index < 0:
            QMessageBox.information(self, "提示", "下拉框中没有可移除的文件夹。")
            return
        removed = self.library_folder_combo.itemText(index)
        self.library_folder_combo.removeItem(index)
        save_music_folders(self.library_folders())
        self.update_library_status(f"已移除文件夹：{removed}")

    def start_music_scan(self):
        """启动后台扫描：递归收集音频 → 计算 SHA256 → 内容去重入库"""
        if self.scan_worker is not None and self.scan_worker.isRunning():
            QMessageBox.information(self, "提示", "扫描正在进行，可点击“停止”中断。")
            return

        folders = self.library_folders()
        if not folders:
            QMessageBox.information(
                self, "提示", "请先点击“添加文件夹”选择要扫描的音乐文件夹。"
            )
            return

        valid = [f for f in folders if os.path.isdir(f)]
        missing = [f for f in folders if not os.path.isdir(f)]
        if missing:
            QMessageBox.warning(
                self, "文件夹不可用",
                "以下文件夹不存在或无法访问，将被跳过：\n" + "\n".join(missing[:5]),
            )
        if not valid:
            return

        save_music_folders(valid)
        self.duplicate_records = []
        self.short_records = []
        self._scan_stats = None

        # “短音频”阈值：时长短于此值的文件（多是系统音效）不入库；0 表示不过滤
        try:
            min_duration = int(self.cbx_library_min_duration.currentData() or 0)
        except (TypeError, ValueError):
            min_duration = DEFAULT_MIN_DURATION
        self._last_min_duration = min_duration
        save_min_duration(min_duration)

        self.scan_worker = MusicScanWorker(
            valid,
            known_entries=self.library_entries,  # 未变化文件复用历史 SHA256
            known_hashes=self.library_hashes,
            dedup=self.cbx_library_dedup.isChecked(),
            min_duration=min_duration,
            parent=self,
        )
        self.scan_worker.file_found.connect(self.add_music_item)
        self.scan_worker.duplicate_found.connect(self.on_music_duplicate_found)
        self.scan_worker.short_found.connect(self.on_music_short_found)
        self.scan_worker.progress.connect(self.on_music_scan_progress)
        self.scan_worker.finished_scan.connect(self.on_music_scan_finished)
        self.scan_worker.finished.connect(self.on_music_scan_thread_done)

        self.btn_library_scan.setEnabled(False)
        self.btn_library_stop.setEnabled(True)
        self.update_library_status("扫描中…")
        self.scan_worker.start()

    def stop_music_scan(self):
        """请求停止扫描（线程会在当前文件处理完后退出）"""
        if self.scan_worker is not None and self.scan_worker.isRunning():
            self.scan_worker.requestInterruption()
            self.btn_library_stop.setEnabled(False)
            self.update_library_status("正在停止…")

    def on_music_scan_progress(self, done, total):
        """扫描进度回报"""
        self.update_library_status(f"扫描中… {done}/{total}" if total else "扫描中…")

    def add_music_item(self, entry):
        """把扫描结果写入列表：新路径新增条目，已有路径更新显示"""
        path = entry.get("path")
        if not path:
            return
        item = self.library_items.get(path)
        if item is None:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, path)
            self.music_list.addItem(item)
            self.library_items[path] = item
        self.library_entries[path] = entry
        sha = entry.get("sha256")
        if sha:
            self.library_hashes[sha] = path
        item.setText(track_display_text(entry))
        item.setToolTip(track_tooltip(entry))
        self.apply_music_filter_to(item)

    def on_music_duplicate_found(self, record):
        """记录一个因内容重复被跳过的文件"""
        self.duplicate_records.append(record)

    def on_music_short_found(self, record):
        """记录一个因时长过短（系统音效等）被跳过的文件"""
        self.short_records.append(record)

    def on_music_scan_finished(self, stats):
        """扫描结束：排序、落盘、刷新状态"""
        self._scan_stats = stats
        self.btn_library_scan.setEnabled(True)
        self.btn_library_stop.setEnabled(False)
        if self.music_list.count() > 1:
            self.music_list.sortItems()
        self.persist_music_library()

        summary = (
            f"新增 {stats.get('added', 0)}，已在库 {stats.get('existing', 0)}，"
            f"重复跳过 {stats.get('duplicate', 0)}"
        )
        if stats.get("too_short"):
            summary += f"，短音频跳过 {stats.get('too_short', 0)}"
        if stats.get("failed"):
            summary += f"，读取失败 {stats.get('failed', 0)}"
        summary = ("扫描已停止：" if stats.get("cancelled") else "扫描完成：") + summary
        self.update_library_status(summary)

    def on_music_scan_thread_done(self):
        """线程真正结束后释放对象，避免下次扫描与残留线程冲突"""
        worker = self.scan_worker
        self.scan_worker = None
        if worker is not None:
            worker.deleteLater()

    def persist_music_library(self):
        """把音乐库与 SHA256 索引写入 ui_settings.json"""
        persist_music_library(self.library_entries)

    def update_library_status(self, prefix=None):
        """刷新底部状态标签：库容量 + 最近一次扫描结果"""
        total = len(self.library_entries)
        size = sum((e.get("size") or 0) for e in self.library_entries.values())
        text = f"共 {total} 首 · {format_size(size)}"
        self.library_status.setText(f"{prefix}\n{text}" if prefix else text)

        details = []
        stats = self._scan_stats
        if stats:
            details.append(
                f"上次扫描：{stats.get('total', 0)} 个文件，新增 {stats.get('added', 0)}，"
                f"已存在 {stats.get('existing', 0)}，重复跳过 {stats.get('duplicate', 0)}，"
                f"短音频跳过 {stats.get('too_short', 0)}，失败 {stats.get('failed', 0)}"
            )
        if self.duplicate_records or self.short_records:
            details.append(
                f"跳过 {len(self.duplicate_records)} 个重复 + "
                f"{len(self.short_records)} 个短音频（点击“跳过项”查看详情）"
            )
        self.library_status.setToolTip("\n".join(details))

    def apply_music_filter_to(self, item):
        """对单个列表项应用当前搜索关键字"""
        keyword = self.library_search.text().strip().lower()
        if not keyword:
            item.setHidden(False)
            return
        haystack = f"{item.text()}\n{item.toolTip()}".lower()
        item.setHidden(keyword not in haystack)

    def filter_music_list(self, _text=""):
        """按关键字过滤音乐列表（匹配标题、艺术家与完整路径）"""
        for i in range(self.music_list.count()):
            self.apply_music_filter_to(self.music_list.item(i))

    def play_selected_music(self, item=None):
        """播放列表中选中的音乐"""
        if item is None:
            item = self.music_list.currentItem()
        if item is None:
            QMessageBox.information(self, "提示", "请先在列表中选择要播放的音乐。")
            return
        path = item.data(Qt.ItemDataRole.UserRole)
        if not path or not os.path.exists(path):
            QMessageBox.warning(
                self, "文件不存在", f"该文件已被移动或删除：\n{path}"
            )
            return
        self.open_media_from_path(path)

    def reveal_music_file(self):
        """在系统文件管理器中定位当前选中的音乐"""
        item = self.music_list.currentItem()
        if item is None:
            return
        path = item.data(Qt.ItemDataRole.UserRole)
        if not path or not os.path.exists(path):
            QMessageBox.warning(self, "文件不存在", f"找不到该文件：\n{path}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(path)))

    def remove_selected_music(self, *_):
        """仅从列表移除选中条目（不删除磁盘文件）"""
        items = self.music_list.selectedItems()
        if not items:
            QMessageBox.information(self, "提示", "请先在列表中选择要移除的条目。")
            return
        for item in items:
            path = item.data(Qt.ItemDataRole.UserRole)
            row = self.music_list.row(item)
            if row >= 0:
                self.music_list.takeItem(row)
            self.library_items.pop(path, None)
            self.library_entries.pop(path, None)
        self.rebuild_library_hashes()
        self.persist_music_library()
        self.update_library_status(f"已移除 {len(items)} 首")

    def clear_music_library(self):
        """清空音乐库列表（不删除磁盘文件）"""
        if not self.library_entries:
            return
        reply = QMessageBox.question(
            self, "清空列表",
            f"确定从列表移除全部 {len(self.library_entries)} 首音乐？\n"
            "（不会删除磁盘上的文件）",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self.music_list.clear()
        self.library_entries.clear()
        self.library_items.clear()
        self.library_hashes.clear()
        self.duplicate_records = []
        self.short_records = []
        self._scan_stats = None
        self.persist_music_library()
        self.update_library_status("列表已清空")

    def rebuild_library_hashes(self):
        """按当前条目重建 SHA256 索引（移除条目后调用）"""
        hashes = {}
        for path, entry in self.library_entries.items():
            sha = entry.get("sha256")
            if sha:
                hashes.setdefault(sha, path)
        self.library_hashes = hashes

    def show_skipped_records(self):
        """查看上次扫描中被跳过的文件：内容重复 / 时长过短"""
        dups, shorts = self.duplicate_records, self.short_records
        if not dups and not shorts:
            QMessageBox.information(
                self, "跳过项", "上次扫描没有跳过任何文件。"
            )
            return

        sections = []
        if dups:
            lines = [f"内容重复（SHA256 相同）{len(dups)} 个，仅保留首个："]
            for r in dups[:20]:
                lines.append(
                    f"{os.path.basename(r['path'])}\n"
                    f"  跳过：{r['path']}\n  保留：{r['original']}"
                )
            if len(dups) > 20:
                lines.append(f"…（共 {len(dups)} 条，仅显示前 20 条）")
            sections.append("\n".join(lines))
        if shorts:
            label = (f"短于 {self._last_min_duration} 秒" if self._last_min_duration
                     else "时长过短")
            lines = [f"时长过短（{label}）{len(shorts)} 个，未入库："]
            for r in shorts[:20]:
                lines.append(
                    f"{format_duration(r.get('duration')) or '?'}  {r['path']}"
                )
            if len(shorts) > 20:
                lines.append(f"…（共 {len(shorts)} 条，仅显示前 20 条）")
            lines.append("如需收录，把“短音频”改为“不过滤”后重新扫描即可")
            sections.append("\n".join(lines))

        box = QMessageBox(self)
        box.setWindowTitle("跳过项")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(f"重复 {len(dups)} 个 · 短音频 {len(shorts)} 个")
        box.setInformativeText("这些文件都未加入列表，磁盘上的文件未做任何改动。")
        box.setDetailedText("\n\n".join(sections))
        box.exec()

    def on_music_list_menu(self, pos):
        """音乐列表右键菜单"""
        item = self.music_list.itemAt(pos)
        if item is None:
            return
        self.music_list.setCurrentItem(item)
        menu = QMenu(self.music_list)
        act_play = menu.addAction("播放")
        act_reveal = menu.addAction("打开所在文件夹")
        menu.addSeparator()
        act_remove = menu.addAction("从列表移除")
        chosen = menu.exec(self.music_list.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if chosen == act_play:
            self.play_selected_music(item)
        elif chosen == act_reveal:
            self.reveal_music_file()
        elif chosen == act_remove:
            self.remove_selected_music()

    # ---------------- 标签编辑：仅对正在播放的音乐 ----------------

    def build_tag_editor_tab(self):
        """构建“标签编辑”标签页：字段定义来自 tag_editor.TAG_FIELDS"""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(8)
        layout.setContentsMargins(0, 6, 0, 0)

        self.tag_file_label = QLabel("未播放音乐")
        self.tag_file_label.setWordWrap(True)
        layout.addWidget(self.tag_file_label)

        button_row = QHBoxLayout()
        button_row.setSpacing(6)
        self.btn_tag_reload = QPushButton("重新读取")
        self.btn_tag_reload.setToolTip("丢弃未保存的改动，从文件重新读取标签")
        self.btn_tag_save = QPushButton("保存标签")
        self.btn_tag_save.setToolTip("把当前内容直接写入音频文件（覆盖原标签，不可撤销）")
        button_row.addWidget(self.btn_tag_reload)
        button_row.addWidget(self.btn_tag_save)
        button_row.addStretch()
        layout.addLayout(button_row)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        form = QFormLayout(inner)
        form.setSpacing(6)
        form.setContentsMargins(2, 2, 2, 2)
        form.setLabelAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self.tag_edits = {}
        for field in TAG_FIELDS:
            if field.multiline:
                editor = QPlainTextEdit()
                editor.setFixedHeight(96)
            else:
                editor = QLineEdit()
            # 左侧已有字段名，输入框内不再重复显示占位文字，避免视觉噪音
            editor.setToolTip(f"{field.label}（留空表示删除该标签项）")
            self.tag_edits[field.key] = editor
            form.addRow(field.label, editor)
        scroll.setWidget(inner)
        layout.addWidget(scroll, stretch=1)

        self.tag_status = QLabel("标签编辑仅对本地音乐文件可用")
        self.tag_status.setWordWrap(True)
        layout.addWidget(self.tag_status)

        self.btn_tag_reload.clicked.connect(self.reload_tag_editor)
        self.btn_tag_save.clicked.connect(self.save_tag_editor)
        self.set_tag_editors_enabled(False)
        return tab

    def set_tag_editors_enabled(self, enabled):
        """统一切换标签编辑区可用状态"""
        for editor in self.tag_edits.values():
            editor.setEnabled(enabled)
        self.btn_tag_save.setEnabled(enabled)
        self.btn_tag_reload.setEnabled(enabled)

    @staticmethod
    def get_tag_edit_text(editor):
        if isinstance(editor, QPlainTextEdit):
            return editor.toPlainText()
        return editor.text()

    @staticmethod
    def put_tag_edit_text(editor, text):
        if isinstance(editor, QPlainTextEdit):
            editor.setPlainText(text)
        else:
            editor.setText(text)

    def current_tag_values(self):
        """读取编辑区当前内容，返回 {key: 文本}"""
        return {key: self.get_tag_edit_text(editor)
                for key, editor in self.tag_edits.items()}

    def apply_tag_values(self, values):
        """把 {key: 文本} 填进编辑区"""
        for key, editor in self.tag_edits.items():
            editor.blockSignals(True)
            self.put_tag_edit_text(editor, values.get(key, ""))
            editor.blockSignals(False)

    def has_unsaved_tag_changes(self):
        """编辑区是否有未保存的改动"""
        if not self.tag_current_path or not self.tag_original:
            return False
        return self.current_tag_values() != self.tag_original

    def refresh_tag_editor(self, path=None):
        """按当前播放的文件刷新标签编辑区；非音乐/串流时置为不可编辑"""
        if not getattr(self, "tag_edits", None):
            return
        if path is None:
            path = self.cur_media_path
        path = path or ""

        # 切换到另一个文件时，未保存的改动会被丢弃
        dropped = ""
        if (self.tag_current_path and path
                and os.path.abspath(path) != os.path.abspath(self.tag_current_path)
                and self.has_unsaved_tag_changes()):
            dropped = "（已丢弃上一个文件未保存的改动）"

        note = None
        kind = "unknown"
        if not path:
            note = "标签编辑仅对本地音乐文件可用（当前未播放音乐）"
        elif self.is_streaming or path.startswith(
            ("http://", "https://", "rtsp://", "rtmp://", "udp://", "tcp://")
        ):
            note = "网络串流不支持编辑标签"
        elif self.is_video or path.lower().endswith(LOCAL_VIDEO_EXTS):
            note = "视频文件不支持编辑标签"
        elif not os.path.isfile(path):
            note = "文件不存在或已被移动"
        else:
            kind = probe_container(path)
            if kind == "unknown":
                note = f"该格式（{os.path.splitext(path)[1] or '未知'}）暂不支持写入标签"

        if note is not None:
            self.tag_current_path = ""
            self.tag_original = {}
            self.apply_tag_values({})
            self.set_tag_editors_enabled(False)
            self.tag_file_label.setText(
                "未播放音乐" if not path else os.path.basename(path)
            )
            self.tag_file_label.setToolTip(path)
            self.tag_status.setText(note + dropped)
            return

        values = read_tags(path)
        self.apply_tag_values(values)
        self.tag_original = dict(values)
        self.tag_current_path = path
        self.set_tag_editors_enabled(True)
        self.tag_file_label.setText(os.path.basename(path))
        self.tag_file_label.setToolTip(path)
        self.tag_status.setText(
            f"标签格式：{CONTAINER_LABELS.get(kind, kind)}"
            "（修改后点“保存标签”写入文件）" + dropped
        )

    def reload_tag_editor(self):
        """从文件重新读取标签（丢弃未保存的改动）"""
        path = self.tag_current_path or self.cur_media_path
        if not path or not os.path.isfile(path):
            QMessageBox.information(self, "提示", "当前没有可读取标签的音乐文件。")
            return
        self.refresh_tag_editor(path)

    def save_tag_editor(self):
        """把编辑区内容写入音频文件，并同步刷新列表与媒体信息"""
        path = self.tag_current_path
        if not path or not os.path.isfile(path):
            QMessageBox.information(self, "提示", "当前没有可编辑标签的音乐文件。")
            return

        values = self.current_tag_values()
        changed = [
            FIELD_LABELS.get(key, key)
            for key, text in values.items()
            if text.strip() != (self.tag_original.get(key) or "").strip()
        ]
        if not changed:
            self.tag_status.setText("没有改动需要保存")
            return

        ok, message = save_tags(path, values)
        if not ok:
            self.tag_status.setText(message)
            QMessageBox.warning(self, "保存失败", message)
            return

        self.tag_original = dict(values)
        summary = "、".join(changed[:4])
        if len(changed) > 4:
            summary += f" 等 {len(changed)} 项"
        self.tag_status.setText(f"已更新：{summary}")

        # 标题/艺术家等变化后，同步音乐库列表与媒体信息面板
        self.refresh_library_entry_for(path)
        if not self.is_video:
            self.read_audio_metadata(path)
            self.show_media_info(path)

    def refresh_library_entry_for(self, path):
        """标签改动后刷新音乐库中该文件的显示文本"""
        entry = self.library_entries.get(path)
        if entry is None:
            return
        for key, value in read_track_meta(path).items():
            if value is not None:
                entry[key] = value
        self.add_music_item(entry)

    # ---------------- 系统媒体控制：MPRIS / 系统托盘 / 媒体键 ----------------

    def setup_system_media_control(self):
        """初始化系统集成：MPRIS 服务 + 系统托盘 + 键盘媒体键"""
        self.setup_mpris()
        self.setup_tray_icon()
        self.install_media_key_shortcuts()

    # ---- MPRIS：让桌面媒体控件 / 耳机按键 / playerctl 能控制播放 ----
    def setup_mpris(self):
        """启动 MPRIS 服务（缺少 PyGObject 时静默跳过，不影响播放）"""
        if not mpris_player.is_available():
            return
        self.mpris = mpris_player.MprisController(parent=self)
        self.mpris.play_pause_requested.connect(self.play_pause)
        self.mpris.play_requested.connect(self.mpris_play)
        self.mpris.pause_requested.connect(self.mpris_pause)
        self.mpris.stop_requested.connect(self.stop_play)
        self.mpris.next_requested.connect(self.play_next_music)
        self.mpris.previous_requested.connect(self.play_previous_music)
        self.mpris.seek_requested.connect(self.mpris_seek_relative)
        self.mpris.set_position_requested.connect(self.mpris_seek_absolute)
        self.mpris.volume_requested.connect(self.mpris_set_volume)
        self.mpris.loop_status_requested.connect(self.mpris_set_loop_status)
        self.mpris.raise_requested.connect(self.mpris_raise)
        self.mpris.quit_requested.connect(self.close)
        if not self.mpris.start():
            self.mpris = None

    def mpris_play(self):
        """桌面请求播放"""
        if not self.media_player.is_playing():
            self.play_pause()

    def mpris_pause(self):
        """桌面请求暂停"""
        if self.media_player.is_playing():
            self.media_player.pause()
        self.sync_mpris_state()

    def mpris_seek_relative(self, offset_us):
        """桌面请求相对跳转（微秒，来自 Seek）"""
        if not self.cur_media_path or self.is_streaming:
            return
        try:
            target = self.media_player.get_time() + int(offset_us) // 1000
            total = self.media_player.get_length()
            if total and total > 0:
                target = min(target, total)
            target = max(0, target)
            self.media_player.set_time(target)
            if self.mpris is not None:
                self.mpris.notify_seeked(target * 1000)
        except Exception:
            pass

    def mpris_seek_absolute(self, position_us):
        """桌面请求绝对跳转（微秒，来自 SetPosition）"""
        if not self.cur_media_path or self.is_streaming:
            return
        try:
            position_us = max(0, int(position_us))
            self.media_player.set_time(position_us // 1000)
            if self.mpris is not None:
                self.mpris.notify_seeked(position_us)
        except Exception:
            pass

    def mpris_set_volume(self, volume):
        """桌面请求设置音量（0.0 ~ 1.0）"""
        level = int(round(max(0.0, min(1.0, float(volume))) * 100))
        self.media_player.audio_set_volume(level)
        self.slider_vol.blockSignals(True)
        self.slider_vol.setValue(level)
        self.slider_vol.blockSignals(False)
        if self.mpris is not None:
            self.mpris.update_volume(level / 100.0)

    def mpris_set_loop_status(self, status):
        """桌面请求切换循环模式（None / Track / Playlist）"""
        self.loop_single = status in ("Track", "Playlist")
        self.btn_loop.setText("循环(开启)" if self.loop_single else "单曲循环")
        if self.mpris is not None:
            self.mpris.update_loop_status("Track" if self.loop_single else "None")

    def mpris_raise(self):
        """桌面请求把主窗口带到前台"""
        self.tray_show_window()

    def mpris_art_url(self, path):
        """给桌面提供封面文件 URL（MPRIS 只接受 URL）

        内嵌封面写到临时缓存文件；没有内嵌封面时回退到默认封面图片。
        """
        if not path or not os.path.isfile(path):
            return None
        if path in self._mpris_art_urls:
            return self._mpris_art_urls[path]

        url = None
        try:
            image = MetadataReader.extract_cover(path)
            if image is not None:
                cache_dir = os.path.join(tempfile.gettempdir(), "ruiplayer-mpris")
                os.makedirs(cache_dir, exist_ok=True)
                stamp = f"{path}:{os.path.getmtime(path)}"
                digest = hashlib.sha1(stamp.encode("utf-8")).hexdigest()[:16]
                cover_path = os.path.join(cache_dir, f"cover-{digest}.jpg")
                if not os.path.exists(cover_path):
                    image.convert("RGB").save(cover_path, "JPEG", quality=90)
                url = QUrl.fromLocalFile(cover_path).toString()
        except Exception:
            url = None

        if url is None:
            default_path = os.path.join(
                os.path.dirname(__file__), self.DEFAULT_COVER_FILENAME
            )
            if os.path.exists(default_path):
                url = QUrl.fromLocalFile(default_path).toString()

        self._mpris_art_urls[path] = url
        return url

    def track_length_us(self, meta=None):
        """当前曲目时长（微秒）：优先播放器解析结果，其次文件标签"""
        try:
            length_ms = self.media_player.get_length()
        except Exception:
            length_ms = 0
        if length_ms and length_ms > 0:
            return int(length_ms) * 1000
        if meta and meta.get("duration"):
            try:
                return int(meta["duration"]) * 1_000_000
            except (TypeError, ValueError):
                return 0
        return 0

    def update_mpris_track(self, path=None):
        """把当前曲目信息（标题/艺术家/专辑/时长/封面）回传给桌面"""
        if self.mpris is None:
            return
        path = path or self.cur_media_path
        if not path:
            return

        # 网络串流没有本地标签，用地址当标题
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", path):
            title = (self.stream_url_input.currentText() or path).strip()
            self.mpris.update_track(title=title, track_seed=path)
            self._mpris_length_us = 0
            return

        try:
            meta = read_track_meta(path) or {}
        except Exception:
            meta = {}
        title = meta.get("title") or os.path.splitext(os.path.basename(path))[0]
        length_us = self.track_length_us(meta)
        self.mpris.update_track(
            title=title,
            artist=meta.get("artist"),
            album=meta.get("album"),
            length_us=length_us,
            art_url=self.mpris_art_url(path),
            track_seed=path,
        )
        self._mpris_length_us = length_us
        self.update_tray_tooltip()

    def sync_mpris_state(self):
        """每秒把播放状态 / 进度 / 音量同步给桌面"""
        if self.mpris is None:
            return

        try:
            playing = bool(self.media_player.is_playing())
        except Exception:
            playing = False
        if playing:
            status = "Playing"
        elif self.cur_media_path:
            status = "Paused"
        else:
            status = "Stopped"

        queue = self.music_queue_paths()
        has_queue = len(queue) > 1 and self.cur_media_path in queue
        self.mpris.update_playback(
            status,
            next=has_queue,
            previous=has_queue,
            seek=bool(self.cur_media_path) and not self.is_streaming,
            play=bool(self.cur_media_path),
            pause=bool(self.cur_media_path),
        )

        try:
            position_ms = self.media_player.get_time()
        except Exception:
            position_ms = 0
        if position_ms and position_ms > 0:
            self.mpris.update_position(int(position_ms) * 1000)

        try:
            volume = self.media_player.audio_get_volume()
        except Exception:
            volume = None
        if volume is not None and volume >= 0:
            self.mpris.update_volume(volume / 100.0)

        # 曲目总时长往往解析得比较晚，发现明显变化就重新回传一次
        if self.cur_media_path and not self.is_streaming:
            length_us = self.track_length_us()
            if length_us and abs(length_us - self._mpris_length_us) > 1_000_000:
                self.update_mpris_track(self.cur_media_path)

        self.update_tray_tooltip()

    # ---- 上下曲：按音乐库列表顺序 ----
    def music_queue_paths(self):
        """音乐库列表中当前可见（未被搜索过滤掉）的曲目，按列表顺序"""
        paths = []
        for i in range(self.music_list.count()):
            item = self.music_list.item(i)
            if item is None or item.isHidden():
                continue
            path = item.data(Qt.ItemDataRole.UserRole)
            if path:
                paths.append(path)
        return paths

    def music_queue_position(self):
        """当前曲目在播放队列中的下标；不在队列里返回 None"""
        queue = self.music_queue_paths()
        return queue.index(self.cur_media_path) if self.cur_media_path in queue else None

    def play_queue_offset(self, step):
        """按音乐库列表顺序切歌（step=+1 下一首，-1 上一首），首尾循环"""
        queue = self.music_queue_paths()
        if not queue:
            QMessageBox.information(
                self, "音乐库为空",
                "上一曲/下一曲按音乐库列表顺序切换，请先在“音乐库”标签页扫描音乐文件夹。",
            )
            return
        index = self.music_queue_position()
        if index is None:
            target = queue[0] if step > 0 else queue[-1]
        else:
            target = queue[(index + step) % len(queue)]
        if not os.path.exists(target):
            QMessageBox.warning(self, "文件不存在", f"该文件已被移动或删除：\n{target}")
            return
        self.open_media_from_path(target)

    def play_next_music(self):
        """下一曲（媒体键 / 桌面媒体控件 / 托盘菜单共用）"""
        self.play_queue_offset(1)

    def play_previous_music(self):
        """上一曲"""
        self.play_queue_offset(-1)

    # ---- 系统托盘 ----
    def setup_tray_icon(self):
        """系统托盘图标 + 右键菜单"""
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        icon = QIcon()
        icon_path = os.path.join(os.path.dirname(__file__), "qrstudio-icon.png")
        if os.path.exists(icon_path):
            icon = QIcon(icon_path)
        elif self.custom_default_cover is not None:
            icon = QIcon(self.custom_default_cover)
        self.tray_icon = QSystemTrayIcon(icon, self)
        self.tray_icon.setToolTip("RuiPlayer 多媒体播放器")

        menu = QMenu(self)
        act_show = menu.addAction("显示主窗口")
        act_show.triggered.connect(self.tray_show_window)
        menu.addSeparator()
        act_play = menu.addAction("播放/暂停")
        act_play.triggered.connect(self.play_pause)
        act_prev = menu.addAction("上一曲")
        act_prev.triggered.connect(self.play_previous_music)
        act_next = menu.addAction("下一曲")
        act_next.triggered.connect(self.play_next_music)
        act_stop = menu.addAction("停止")
        act_stop.triggered.connect(self.stop_play)
        menu.addSeparator()
        act_quit = menu.addAction("退出")
        act_quit.triggered.connect(self.close)

        self.tray_menu = menu
        self.tray_icon.setContextMenu(menu)
        self.tray_icon.activated.connect(self.on_tray_activated)
        self.tray_icon.show()
        self.update_tray_tooltip()

    def on_tray_activated(self, reason):
        """双击托盘图标显示主窗口"""
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.tray_show_window()

    def tray_show_window(self):
        """把主窗口带到前台（同时供 MPRIS Raise 使用）"""
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def update_tray_tooltip(self):
        """托盘提示显示当前曲目与播放状态"""
        if self.tray_icon is None:
            return
        if not self.cur_media_path:
            self.tray_icon.setToolTip("RuiPlayer 多媒体播放器")
            return
        try:
            playing = bool(self.media_player.is_playing())
        except Exception:
            playing = False
        name = (self.cur_media_path if self.is_streaming
                else os.path.basename(self.cur_media_path))
        self.tray_icon.setToolTip(
            f"RuiPlayer · {'播放中' if playing else '已暂停'}\n{name}"
        )

    # ---- 键盘媒体键 ----
    def install_media_key_shortcuts(self):
        """注册键盘媒体键（播放/暂停、上一曲、下一曲、停止）

        桌面（KDE/GNOME）一般会把媒体键转发给 MPRIS 播放器，按键不会到达窗口；
        这里额外保留一份应用内快捷键，覆盖未配置全局转发的情况。
        """
        bindings = (
            (Qt.Key.Key_MediaTogglePlayPause, self.play_pause),
            (Qt.Key.Key_MediaPlay, self.play_pause),
            (Qt.Key.Key_MediaPause, self.play_pause),
            (Qt.Key.Key_MediaStop, self.stop_play),
            (Qt.Key.Key_MediaNext, self.play_next_music),
            (Qt.Key.Key_MediaPrevious, self.play_previous_music),
        )
        self.media_shortcuts = []
        for key, handler in bindings:
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
            shortcut.activated.connect(lambda h=handler: self.handle_media_key(h))
            self.media_shortcuts.append(shortcut)

    def handle_media_key(self, handler):
        """媒体键去重：同一按键的重复事件短时间内只执行一次"""
        now = time.monotonic()
        if now - self._last_media_key < 0.3:
            return
        self._last_media_key = now
        handler()

    def closeEvent(self, event):
        """退出前停止后台扫描线程与 MPRIS 服务，避免线程仍在工作时被销毁"""
        if self.scan_worker is not None and self.scan_worker.isRunning():
            self.scan_worker.requestInterruption()
            self.scan_worker.wait(3000)
        if self.mpris is not None:
            self.mpris.stop()
            self.mpris = None
        if self.tray_icon is not None:
            self.tray_icon.hide()
        self.persist_music_library()
        super().closeEvent(event)


class EqualizerDialog(QDialog):
    """均衡器窗口：所有调节实时生效，非假窗口"""

    def __init__(self, player, parent=None):
        super().__init__(parent)
        self.player = player
        self.setWindowTitle("均衡器")
        self.setMinimumWidth(660)
        self.equalizer_sliders = []
        self.equalizer_value_labels = []

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        # 顶部：预设 + 启用 + 重置
        top_row = QHBoxLayout()
        top_row.setSpacing(6)
        self.eq_preset_combo = QComboBox()
        self.eq_preset_combo.addItems([
            "平直", "古典", "俱乐部", "舞曲", "低音增强",
            "低音+高音增强", "高音增强", "耳机", "大厅", "现场",
            "派对", "流行", "雷鬼", "摇滚", "斯卡", "柔和", "柔摇滚", "电子",
        ])
        self.eq_preset_combo.setCurrentText("平直")
        self.eq_preset_combo.currentTextChanged.connect(self.apply_equalizer_preset)

        self.eq_enable_checkbox = QCheckBox("启用均衡器")
        self.eq_enable_checkbox.setChecked(self.player.equalizer_enabled)
        self.eq_enable_checkbox.toggled.connect(self.toggle_equalizer)

        self.eq_reset_btn = QPushButton("重置")
        self.eq_reset_btn.clicked.connect(self.reset_equalizer)

        top_row.addWidget(QLabel("预设："))
        top_row.addWidget(self.eq_preset_combo)
        top_row.addWidget(self.eq_enable_checkbox)
        top_row.addWidget(self.eq_reset_btn)
        top_row.addStretch()
        layout.addLayout(top_row)

        # 10 段频段滑块
        band_row = QHBoxLayout()
        band_row.setSpacing(8)
        for freq in [31.25, 62.5, 125.0, 250.0, 500.0, 1000.0, 2000.0, 4000.0, 8000.0, 16000.0]:
            band_widget = QWidget()
            band_layout = QVBoxLayout(band_widget)
            band_layout.setSpacing(4)
            freq_label = QLabel(f"{freq:.0f}")
            freq_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            value_label = QLabel("0dB")
            value_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            slider = QSlider(Qt.Orientation.Vertical)
            slider.setRange(-12, 12)
            slider.setValue(0)
            slider.setSingleStep(1)
            slider.setTickPosition(QSlider.TickPosition.TicksBothSides)
            slider.setTickInterval(2)
            slider.valueChanged.connect(self.on_equalizer_slider_changed)
            band_layout.addWidget(freq_label)
            band_layout.addWidget(slider, stretch=1)
            band_layout.addWidget(value_label)
            band_row.addWidget(band_widget)
            self.equalizer_sliders.append(slider)
            self.equalizer_value_labels.append(value_label)
        layout.addLayout(band_row)

        self.sync_sliders_from_player()

    def sync_sliders_from_player(self):
        """打开时用播放器当前均衡器状态初始化滑块与预设显示"""
        for slider, value in zip(self.equalizer_sliders, self.player.equalizer_bands):
            slider.blockSignals(True)
            slider.setValue(int(round(value)))
            slider.blockSignals(False)
        self.update_equalizer_labels()
        for i in range(self.eq_preset_combo.count()):
            name = self.eq_preset_combo.itemText(i)
            if get_equalizer_preset_values(name) == self.player.equalizer_bands:
                self.eq_preset_combo.blockSignals(True)
                self.eq_preset_combo.setCurrentText(name)
                self.eq_preset_combo.blockSignals(False)
                break

    def toggle_equalizer(self, enabled):
        self.player.toggle_equalizer(enabled)

    def reset_equalizer(self):
        self.player.equalizer_bands = [0.0] * 10
        self.player.equalizer_preamp = 0.0
        self.eq_preset_combo.blockSignals(True)
        self.eq_preset_combo.setCurrentText("平直")
        self.eq_preset_combo.blockSignals(False)
        for slider in self.equalizer_sliders:
            slider.setValue(0)
        self.update_equalizer_labels()
        self.player.apply_equalizer_to_player()

    def apply_equalizer_preset(self, preset_name):
        values = get_equalizer_preset_values(preset_name)
        self.player.equalizer_bands = list(values)
        for slider, value in zip(self.equalizer_sliders, values):
            slider.blockSignals(True)
            slider.setValue(int(round(value)))
            slider.blockSignals(False)
        self.update_equalizer_labels()
        self.player.apply_equalizer_to_player()

    def on_equalizer_slider_changed(self, value):
        slider = self.sender()
        if slider is None:
            return
        idx = self.equalizer_sliders.index(slider)
        self.player.equalizer_bands[idx] = float(value)
        # 手动调节后切回“平直(自定义)”，但不触发预设应用，避免重置滑块
        self.eq_preset_combo.blockSignals(True)
        self.eq_preset_combo.setCurrentText("平直")
        self.eq_preset_combo.blockSignals(False)
        self.update_equalizer_labels()
        self.player.apply_equalizer_to_player()

    def update_equalizer_labels(self):
        for slider, label in zip(self.equalizer_sliders, self.equalizer_value_labels):
            label.setText(f"{slider.value()}dB")


class ScaleDialog(QDialog):
    """界面缩放设置窗口：调整比例即时生效并保存"""

    def __init__(self, player, parent=None):
        super().__init__(parent)
        self.player = player
        self.setWindowTitle("界面缩放")
        self.setMinimumWidth(380)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        tip = QLabel("调整界面缩放比例。高清屏（4K/高分屏）建议 125%～200%，"
                     "放大后文字更清晰。")
        tip.setWordWrap(True)
        layout.addWidget(tip)

        # 滑块 + 百分比
        row = QHBoxLayout()
        row.setSpacing(8)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(50, 200)
        self.slider.setValue(int(round(self.player.ui_scale * 100)))
        self.slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.slider.setTickInterval(25)
        self.percent_label = QLabel(f"{self.slider.value()}%")
        self.percent_label.setMinimumWidth(52)
        self.percent_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.slider.valueChanged.connect(lambda v: self.percent_label.setText(f"{v}%"))
        row.addWidget(self.slider)
        row.addWidget(self.percent_label)
        layout.addLayout(row)

        # 快捷预设
        preset_row = QHBoxLayout()
        preset_row.setSpacing(6)
        for text, val in [("100%", 100), ("125%", 125), ("150%", 150), ("200%", 200)]:
            btn = QPushButton(text)
            btn.setProperty("scale_val", val)
            btn.clicked.connect(lambda _=False, v=val: self.slider.setValue(v))
            preset_row.addWidget(btn)
        preset_row.addStretch()
        layout.addLayout(preset_row)

        # 操作按钮
        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)
        self.apply_btn = QPushButton("应用")
        self.apply_btn.clicked.connect(self.apply_scale)
        self.reset_btn = QPushButton("恢复默认")
        self.reset_btn.clicked.connect(lambda: self.slider.setValue(100))
        self.close_btn = QPushButton("关闭")
        self.close_btn.clicked.connect(self.close)
        btn_row.addWidget(self.apply_btn)
        btn_row.addWidget(self.reset_btn)
        btn_row.addWidget(self.close_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

    def apply_scale(self):
        """应用缩放比例：写入配置并即时刷新界面"""
        scale = self.slider.value() / 100.0
        self.player.ui_scale = scale
        self.player.apply_ui_scale()


class ChannelListDialog(QDialog):
    """电视台列表窗口：列出 M3U/M3U8 中的频道，点台播放"""

    def __init__(self, player, parent=None):
        super().__init__(parent)
        self.player = player
        self.channels = []
        self.setWindowTitle("电视台列表")
        self.resize(380, 520)

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索频道...")
        self.search_edit.textChanged.connect(self.filter_channels)
        layout.addWidget(self.search_edit)

        self.channel_list = QListWidget()
        self.channel_list.itemDoubleClicked.connect(self.play_selected)
        layout.addWidget(self.channel_list, stretch=1)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.play_btn = QPushButton("播放选中")
        self.play_btn.clicked.connect(self.play_selected)
        self.count_label = QLabel("共 0 个频道")
        self.close_btn = QPushButton("关闭")
        self.close_btn.clicked.connect(self.close)
        row.addWidget(self.play_btn)
        row.addWidget(self.count_label)
        row.addStretch()
        row.addWidget(self.close_btn)
        layout.addLayout(row)

    def set_channels(self, channels):
        """填充频道列表"""
        self.channels = channels
        self.channel_list.clear()
        for name, url in channels:
            item = QListWidgetItem(name)
            item.setData(Qt.ItemDataRole.UserRole, url)
            self.channel_list.addItem(item)
        if self.channel_list.count() > 0:
            self.channel_list.setCurrentRow(0)
        self.count_label.setText(f"共 {len(channels)} 个频道")
        self.search_edit.clear()

    def filter_channels(self, text):
        """按关键字过滤频道"""
        keyword = text.strip().lower()
        for i in range(self.channel_list.count()):
            item = self.channel_list.item(i)
            item.setHidden(bool(keyword) and keyword not in item.text().lower())

    def current_channel(self):
        """返回当前选中的 (频道名, 流地址)"""
        item = self.channel_list.currentItem()
        if item is None:
            return None
        return item.text(), item.data(Qt.ItemDataRole.UserRole)

    def play_selected(self, *_):
        """播放当前选中的频道"""
        ch = self.current_channel()
        if ch is None:
            QMessageBox.information(self, "提示", "请先选择一个频道")
            return
        name, url = ch
        self.player.play_channel(name, url)


def get_equalizer_preset_values(preset_name):
    presets = {
        "平直": [0.0] * 10,
        "古典": [-1.0, -0.5, 0.0, 0.5, 0.8, 1.0, 0.8, 0.5, 0.0, -0.5],
        "俱乐部": [0.7, 0.6, 0.3, -0.2, -0.4, -0.2, 0.2, 0.5, 0.7, 0.8],
        "舞曲": [1.0, 0.8, 0.4, -0.1, -0.3, -0.1, 0.3, 0.7, 0.9, 1.1],
        "低音增强": [1.5, 1.2, 0.8, 0.3, -0.1, -0.4, -0.6, -0.8, -1.0, -1.2],
        "低音+高音增强": [1.2, 0.9, 0.5, 0.0, -0.2, -0.4, 0.1, 0.6, 1.0, 1.3],
        "高音增强": [-0.8, -0.6, -0.3, 0.1, 0.4, 0.7, 1.0, 1.3, 1.5, 1.7],
        "耳机": [0.5, 0.7, 0.9, 1.0, 0.8, 0.6, 0.4, 0.2, 0.1, 0.0],
        "大厅": [0.4, 0.5, 0.6, 0.7, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2],
        "现场": [0.6, 0.5, 0.3, 0.1, -0.1, -0.2, 0.0, 0.4, 0.7, 0.9],
        "派对": [1.0, 0.8, 0.4, 0.0, -0.2, 0.0, 0.4, 0.8, 1.0, 1.2],
        "流行": [0.8, 0.7, 0.4, 0.0, -0.2, -0.1, 0.3, 0.6, 0.8, 0.9],
        "雷鬼": [0.5, 0.4, 0.1, -0.2, -0.1, 0.1, 0.4, 0.8, 1.0, 0.7],
        "摇滚": [1.2, 0.9, 0.5, 0.0, -0.2, -0.4, -0.2, 0.4, 0.8, 1.2],
        "斯卡": [0.7, 0.6, 0.3, 0.0, -0.2, -0.1, 0.2, 0.6, 0.9, 0.8],
        "柔和": [-0.4, -0.3, -0.1, 0.2, 0.5, 0.8, 0.8, 0.6, 0.4, 0.2],
        "柔摇滚": [0.3, 0.4, 0.5, 0.6, 0.6, 0.5, 0.4, 0.3, 0.2, 0.1],
        "电子": [0.9, 0.8, 0.6, 0.2, -0.1, -0.3, 0.2, 0.7, 1.0, 1.2],
    }
    return presets.get(preset_name, presets["平直"])[:]


SETTINGS_FILE = os.path.join(os.path.dirname(__file__), "ui_settings.json")


def load_settings():
    """读取 ui_settings.json（失败/非法时返回空字典）"""
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_settings(data):
    """写入 ui_settings.json"""
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def load_ui_scale():
    """读取保存的界面缩放比例，非法/缺失时返回 1.0"""
    try:
        scale = float(load_settings().get("ui_scale", 1.0))
        return 0.5 if scale < 0.5 else 2.0 if scale > 2.0 else scale
    except Exception:
        return 1.0


def save_ui_scale(scale):
    """保存界面缩放比例（保留其它设置项）"""
    data = load_settings()
    data["ui_scale"] = round(scale, 2)
    save_settings(data)


def load_fullscreen_screen():
    """读取用户偏好的全屏显示器名称（未设置返回 None）"""
    name = load_settings().get("fullscreen_screen")
    return name if isinstance(name, str) and name else None


def save_fullscreen_screen(name):
    """记住用户选择的全屏显示器名称"""
    data = load_settings()
    data["fullscreen_screen"] = name
    save_settings(data)


def load_lyrics_meta_pref():
    """是否优先从文件元数据读取内嵌歌词（默认 True）"""
    val = load_settings().get("lyrics_from_metadata", True)
    return val if isinstance(val, bool) else True


def save_lyrics_meta_pref(enabled):
    """保存“从元数据读取歌词”这一偏好"""
    data = load_settings()
    data["lyrics_from_metadata"] = bool(enabled)
    save_settings(data)


# 音乐库条目上限：避免 ui_settings.json 随扫描无限膨胀
MAX_LIBRARY_ENTRIES = 20000


def load_music_folders():
    """读取上次扫描使用的文件夹列表"""
    folders = load_settings().get("music_folders")
    if not isinstance(folders, list):
        return []
    return [f for f in folders if isinstance(f, str) and f.strip()]


def save_music_folders(folders):
    """保存扫描文件夹列表（保留最近 50 个）"""
    data = load_settings()
    data["music_folders"] = [f for f in (folders or []) if f][:50]
    save_settings(data)


def load_min_duration():
    """读取音乐库“短音频”过滤阈值（秒）；0 表示不过滤"""
    try:
        value = int(load_settings().get("min_audio_duration", DEFAULT_MIN_DURATION))
    except (TypeError, ValueError):
        return DEFAULT_MIN_DURATION
    return max(0, value)


def save_min_duration(seconds):
    """保存音乐库“短音频”过滤阈值"""
    try:
        seconds = max(0, int(seconds))
    except (TypeError, ValueError):
        return
    data = load_settings()
    data["min_audio_duration"] = seconds
    save_settings(data)


def load_music_library():
    """读取已保存的音乐库。

    返回 (entries, hashes)：
    - entries: path -> 条目字典（含 sha256/size/mtime/标题等）
    - hashes:  sha256 -> 库中首次出现该内容的路径，用于下次扫描快速判重
    """
    raw = load_settings().get("music_library")
    entries = {}
    hashes = {}
    if not isinstance(raw, list):
        return entries, hashes

    def as_int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    for item in raw[:MAX_LIBRARY_ENTRIES]:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        if not isinstance(path, str) or not path:
            continue
        entry = {
            "path": path,
            "sha256": item.get("sha256") if isinstance(item.get("sha256"), str) else "",
            "size": as_int(item.get("size")),
            "mtime": as_int(item.get("mtime")),
            "title": item.get("title") if isinstance(item.get("title"), str) else None,
            "artist": item.get("artist") if isinstance(item.get("artist"), str) else None,
            "album": item.get("album") if isinstance(item.get("album"), str) else None,
            "duration": as_int(item.get("duration")) or None,
        }
        entries[path] = entry
        if entry["sha256"]:
            hashes.setdefault(entry["sha256"], path)
    return entries, hashes


def persist_music_library(entries):
    """保存音乐库条目（超出上限时保留最新入库的部分）"""
    values = list((entries or {}).values())
    if len(values) > MAX_LIBRARY_ENTRIES:
        values = values[-MAX_LIBRARY_ENTRIES:]
    data = load_settings()
    data["music_library"] = values
    save_settings(data)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = MediaPlayer()
    win.show()
    app.exec()
