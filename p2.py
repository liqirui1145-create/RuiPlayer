import os
import sys
import re
import json
from datetime import datetime
from io import BytesIO
from pathlib import Path
from urllib.parse import urljoin

from PyQt6.QtCore import QEvent, Qt, QTimer, QUrl
from PyQt6.QtGui import (
    QColor,
    QDesktopServices,
    QFont,
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
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QMainWindow,
    QPushButton,
    QSlider,
    QSplitter,
    QVBoxLayout,
    QWidget,
)
from PIL import Image

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


# 视频扩展名：本地文件与网络串流分开判定
LOCAL_VIDEO_EXTS = (".mp4", ".mkv", ".avi", ".mov", ".flv", ".webm", ".mpeg", ".mpg")
STREAM_VIDEO_EXTS = LOCAL_VIDEO_EXTS + (".ts", ".m3u8")


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
        lay.addWidget(self.btn_monitor)
        lay.addWidget(self.btn_exit)

        self.btn_play.clicked.connect(self.player.play_pause)
        self.btn_stop.clicked.connect(self._stop)
        self.slider_pos.sliderMoved.connect(self.player.seek_pos)
        self.slider_vol.valueChanged.connect(self.player.set_volume)
        self.cbx_speed.currentTextChanged.connect(self.player.set_play_speed)
        self.btn_loop.clicked.connect(self.player.toggle_loop)
        self.btn_monitor.clicked.connect(self.player.open_screen_chooser)
        self.btn_exit.clicked.connect(self.player.exit_fullscreen)
        self.refresh_monitor_button()

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
    ESC 退出全屏；底部浮动控制栏鼠标靠近底部时浮现、无操作自动隐藏。"""

    HIDE_DELAY = 3500
    ESC 退出全屏；底部浮动控制栏鼠标靠近底部时浮现、无操作自动隐藏。"""

    HIDE_DELAY = 3500

    def __init__(self, player):
        super().__init__()
        self.player = player
        self.setObjectName("fsRoot")
        self.setObjectName("fsRoot")
        self.setWindowTitle("全屏播放")
        self.setWindowFlags(
            Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint
        )
        self.setStyleSheet("#fsRoot { background: #000000; }")
        self.setMouseTracking(True)

        self.setStyleSheet("#fsRoot { background: #000000; }")
        self.setMouseTracking(True)

        self.label = QLabel(self)
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setStyleSheet("background: #000000; color: #888888;")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.label)

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
        self.hotzone.raise_()
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
            self.show_controls()
            return
        self.show_controls()
        self.show_controls()
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if self.player.handle_global_key_release(event):
            return
        super().keyReleaseEvent(event)

    def mouseMoveEvent(self, event):
        self.show_controls()
        super().mouseMoveEvent(event)

    def mouseMoveEvent(self, event):
        self.show_controls()
        super().mouseMoveEvent(event)

    def mouseDoubleClickEvent(self, event):
        self.player.exit_fullscreen()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._layout_overlay()
        self._layout_overlay()
        # 音频全屏：封面随窗口尺寸自适应居中显示
        if self.player is not None and not self.player.is_video:
            self.player.update_fullscreen_art()


class MediaPlayer(QMainWindow):
    DEFAULT_COVER_FILENAME = "Xinjiang_Old_and_young_(Populus_diversifolia_胡杨)_(4973519309).jpg"

    def __init__(self):
        super().__init__()
        self.setWindowTitle("多媒体播放器")
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

        self.timer = QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self.update_progress_and_lrc)
        self.timer.start()

        # 网络串流使用低频定时器，避免每 50ms 高频调用 libvlc 导致 UI 抽帧/无响应
        self.stream_timer = QTimer(self)
        self.stream_timer.setInterval(1000)
        self.stream_timer.timeout.connect(self.update_stream_status)

    def _set_stream_mode(self, streaming):
        """切换轮询模式：网络串流用低频定时器，本地媒体用高频定时器"""
        if streaming:
            self.timer.stop()
            if not self.stream_timer.isActive():
                self.stream_timer.start()
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
        self.btn_lyric.clicked.connect(self.load_lrc_file)
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
        right_layout.setSpacing(10)

        stream_layout = QHBoxLayout()
        stream_layout.setSpacing(5)
        stream_label = QLabel("网络串流 URL:")
        self.stream_url_input = QComboBox()
        self.stream_url_input.setEditable(True)
        self.stream_url_input.addItems(["https://example.com/stream", "rtsp://example.com/camera"])
        self.stream_url_input.setPlaceholderText("请输入网络串流地址")
        self.btn_stream = QPushButton("播放串流")
        stream_layout.addWidget(stream_label)
        stream_layout.addWidget(self.stream_url_input)
        stream_layout.addWidget(self.btn_stream)
        right_layout.addLayout(stream_layout)
        self.btn_stream.clicked.connect(self.play_stream)

        self.btn_m3u = QPushButton("上传M3U/M3U8文件")
        self.btn_m3u.setToolTip("选择本地M3U/M3U8播放列表，列出电视台点台播放")
        self.btn_m3u.clicked.connect(self.load_m3u_file)
        right_layout.addWidget(self.btn_m3u)

        self.btn_equalizer = QPushButton("均衡器")
        self.btn_equalizer.setToolTip("点击打开均衡器窗口")
        self.btn_equalizer.clicked.connect(self.open_equalizer_dialog)

        self.btn_scale = QPushButton("缩放")
        self.btn_scale.setToolTip("点击设置界面缩放比例")
        self.btn_scale.clicked.connect(self.open_scale_dialog)

        eq_scale_row = QHBoxLayout()
        eq_scale_row.setSpacing(6)
        eq_scale_row.addWidget(self.btn_equalizer)
        eq_scale_row.addWidget(self.btn_scale)
        right_layout.addLayout(eq_scale_row)

        title = QLabel("媒体信息")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        right_layout.addWidget(title)
        self.info_panel = QListWidget()
        right_layout.addWidget(self.info_panel)

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

        # 字号自适应（1920x1080 基准 13px，叠加屏幕缩放与用户缩放）
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
        QDesktopServices.openUrl(QUrl("https://github.com/liqirui1145-create/player"))

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

    def load_lrc_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择LRC歌词", "", "*.lrc")
        if path:
            with open(path, "r", encoding="utf-8") as f:
                self.parse_lrc(f.read())
            self.lrc_list.clear()
            self.lrc_list.addItems(self.lrc_lines)
            self.lrc_list.setVisible(True)

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
        url = self.stream_url_input.currentText().strip()
        if not url:
            QMessageBox.warning(self, "输入错误", "请输入有效的网络串流地址！")
            return
        if not (url.startswith(("http://", "https://", "rtsp://", "rtmp://", "udp://", "tcp://"))):
            QMessageBox.warning(self, "格式错误", "请输入有效的网络协议地址（如 http://, https://, rtsp://）")
            return

        # 自动识别 M3U/M3U8 播放列表链接
        if self.is_m3u_url(url):
            self.load_m3u_playlist(url)
            return

        self._play_stream_url(url)

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
            reply.deleteLater()

            text = None
            for enc in ("utf-8", "gbk", "latin-1"):
                try:
                    text = raw.decode(enc)
                    break
                except Exception:
                    continue
            if text is None:
                text = raw.decode("utf-8", errors="ignore")

            channels = self.parse_m3u_content(text, final_url)
            if channels:
                self.open_channel_dialog(channels)
            else:
                # 解析不到频道（可能是 HLS 单流），按普通串流播放
                self._play_stream_url(final_url)
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
        """播放选中的电视台"""
        self._play_stream_url(url, display_name=name)

    def _play_stream_url(self, url, display_name=None):
        """按地址播放单个网络串流（本地链接或频道流共用）"""
        self.close_fullscreen_on_media_change()
        if self.media_player.is_playing():
            self.media_player.stop()

        self.cur_media_path = url
        self.is_streaming = True
        self._set_stream_mode(True)
        self.lrc_list.clear()
        self.lrc_list.hide()

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
        self.lrc_list.clear()
        self.lrc_list.hide()

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
            media_dir = os.path.dirname(path)
            media_name = os.path.splitext(os.path.basename(path))[0]
            lrc_path = os.path.join(media_dir, f"{media_name}.lrc")
            if os.path.exists(lrc_path):
                try:
                    with open(lrc_path, "r", encoding="utf-8") as f:
                        self.parse_lrc(f.read())
                    self.lrc_list.addItems(self.lrc_lines)
                    self.lrc_list.setVisible(True)
                except Exception:
                    pass

    def play_pause(self):
        if self.media_player.is_playing():
            self.media_player.pause()
        else:
            self.media_player.play()
            self.media_player.set_rate(self.cur_speed)
            self.apply_equalizer_to_player()

    def stop_play(self):
        self.close_fullscreen_on_media_change()
        self.media_player.stop()
        self.slider_pos.setValue(0)

    def seek_pos(self, val):
        self.media_player.set_time(val)

    def set_volume(self, vol):
        self.media_player.audio_set_volume(vol)

    def update_progress_and_lrc(self):
        # 网络串流走低频 update_stream_status，避免高频调用 libvlc 导致 UI 卡顿
        if not self.cur_media_path or self.is_streaming:
            return
        cur_ms = self.media_player.get_time()
        total_ms = self.media_player.get_length()

        if total_ms > 0:
            self.slider_pos.setRange(0, total_ms)
            self.slider_pos.setValue(cur_ms)

        if self.lrc_list.isVisible() and self.lrc_time_list:
            target = -1
            for i, t in enumerate(self.lrc_time_list):
                if cur_ms >= t:
                    target = i
            if target != -1 and target != self.cur_lrc_idx:
                self.cur_lrc_idx = target
                self.lrc_list.setCurrentRow(target)

        if self.loop_single and total_ms > 0 and cur_ms >= total_ms - 100:
            self.media_player.set_time(0)
            self.media_player.play()

        if self.is_fullscreen and self.fullscreen_window is not None:
            self.fullscreen_window.sync_from_player()

        if self.is_fullscreen and self.fullscreen_window is not None:
            self.fullscreen_window.sync_from_player()

    def update_stream_status(self):
        """网络串流低频轮询：仅做轻量检查，避免高频调用 libvlc 卡 UI"""
        if not self.cur_media_path or not self.is_streaming:
            return
        # 网络流没有可靠进度；此处保持轻量，避免主线程阻塞。
        # 后续如需检测断流/缓冲状态，可在这里低频处理。
        if self.is_fullscreen and self.fullscreen_window is not None:
            self.fullscreen_window.sync_from_player()
        if self.is_fullscreen and self.fullscreen_window is not None:
            self.fullscreen_window.sync_from_player()

    def open_media_from_path(self, path):
        self.close_fullscreen_on_media_change()
        self.cur_media_path = path
        self.is_streaming = False
        self._set_stream_mode(False)
        self.is_video = path.lower().endswith(LOCAL_VIDEO_EXTS)
        self.lrc_list.clear()
        self.lrc_list.hide()
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


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = MediaPlayer()
    win.show()
    app.exec()
