import sys
import os
import re
import threading
from io import BytesIO
from datetime import datetime
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                             QPushButton, QSlider, QFileDialog, QLabel, QListWidget, QComboBox,
                             QMessageBox, QSplitter, QLineEdit, QProgressBar)
from PyQt6.QtCore import Qt, QTimer, QUrl, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtGui import QImage, QPixmap, QPainter, QColor, QFont
from PIL import Image



# 导入VLC
try:
    import vlc
except:
    print("错误：请安装 VLC 播放器，执行 pip install python-vlc")
    sys.exit()

# 导入音频元数据读取库
try:
    from mutagen import File as MutagenFile
    from mutagen.mp3 import MP3
    from mutagen.flac import FLAC
    from mutagen.wave import WAVE
except:
    print("提示：未安装mutagen库，媒体信息不完整。执行 pip install mutagen")
    MutagenFile = None

# Windows电源事件监听（用于检测休眠/睡眠/合上盖子）
try:
    import win32api
    import win32con
    import win32gui
    HAS_WIN32 = True
except ImportError:
    print("提示：未安装pywin32，休眠检测功能不可用。执行 pip install pywin32")
    HAS_WIN32 = False

class MediaPlayer(QMainWindow):
    # 默认封面文件名
    DEFAULT_COVER_FILENAME = "Xinjiang_Old_and_young_(Populus_diversifolia_胡杨)_(4973519309).jpg"
    
    def __init__(self):
        super().__init__()
        self.setWindowTitle("多媒体播放器")
        self.resize(1024, 600)
        # 设置窗口状态为最大化
        self.setWindowState(Qt.WindowState.WindowMaximized)

        # VLC 播放器初始化（支持网络串流）
        self.vlc_instance = vlc.Instance(
            "--quiet",
            "--aout=waveout",
            "--network-caching=10000",
            "--live-caching=10000",
            "--rtsp-tcp",
            "--http-reconnect",
            "--no-video-title-show"
        )
        self.media_player = self.vlc_instance.media_player_new()

        # 全局播放状态
        self.cur_media_path = ""
        self.is_video = False
        self.is_streaming = False  # 是否正在播放网络串流
        self.lrc_lines = []
        self.lrc_time_list = []
        self.cur_lrc_idx = -1
        self.cur_speed = 1.0
        self.loop_single = False

        # 音频元数据缓存
        self.audio_metadata = {
            "sample_rate": "--", "channels": "--", "artist": "--",
            "album": "--", "title": "--", "bitrate": "--"
        }

        # 【新增】用户自定义默认封面（全局）
        self.custom_default_cover = None
        # 自动加载项目目录中的默认封面图片
        self.load_default_cover_from_file()

        # 键盘快捷键相关变量
        self.space_pressed = False
        self.is_space_long = False
        self.original_speed = 1.0
        self.space_timer = QTimer()
        self.space_timer.setSingleShot(True)
        self.space_timer.setInterval(200)
        self.space_timer.timeout.connect(self.on_space_long_press)

        self.init_ui()

        # 【新增】为所有子控件安装事件过滤器，实现全局快捷键
        self.install_global_shortcuts()

        # 【新增】休眠/睡眠检测功能
        self.was_playing_before_sleep = False
        self.setup_power_event_listener()

        # 全局刷新定时器（进度、歌词同步）
        self.timer = QTimer(interval=50)
        self.timer.timeout.connect(self.update_progress_and_lrc)
        self.timer.start()
    
    def setup_power_event_listener(self):
        """设置Windows电源事件监听（检测休眠/睡眠/合上盖子）"""
        if HAS_WIN32:
            # 注册电源事件回调
            self.power_event_window = PowerEventWindow(self)
    
    def on_system_suspend(self):
        """系统即将进入休眠/睡眠状态"""
        if self.media_player.is_playing():
            self.was_playing_before_sleep = True
            self.media_player.pause()
            print("系统即将休眠，已暂停播放")
    
    def on_system_resume(self):
        """系统从休眠/睡眠状态恢复"""
        if self.was_playing_before_sleep:
            self.media_player.play()
            self.was_playing_before_sleep = False
            print("系统已恢复，继续播放")
    
    def install_global_shortcuts(self):
        """为所有子控件安装事件过滤器，实现窗口内全局快捷键"""
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
        """事件过滤器：拦截所有子控件的键盘事件，实现全局快捷键"""
        if event.type() == event.Type.KeyPress:
            return self.handle_global_key_press(event)
        elif event.type() == event.Type.KeyRelease:
            return self.handle_global_key_release(event)
        return super().eventFilter(obj, event)

    def on_space_long_press(self):
        """空格长按判定"""
        self.is_space_long = True

    def init_ui(self):
        # 使用 QSplitter 实现可拖拽调节宽度
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.setCentralWidget(splitter)
        splitter.setContentsMargins(20, 20, 20, 20)
        splitter.setHandleWidth(8)

        # ========== 左侧播放区域 ==========
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setSpacing(15)

        # 播放画面/封面显示标签（已删除频谱堆叠组件）
        self.video_label = QLabel()
        self.video_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.video_label.setMinimumSize(320, 320)
        self.video_label.setText("请打开媒体文件")
        left_layout.addWidget(self.video_label)

        # 歌词栏（默认隐藏）
        self.lrc_list = QListWidget()
        self.lrc_list.setFixedHeight(110)
        self.lrc_list.setVisible(False)
        left_layout.addWidget(self.lrc_list)

        # 控制区域
        control_layout = QVBoxLayout()
        control_layout.setSpacing(8)

        # 第一行：功能按钮 + 倍速选择
        row1 = QHBoxLayout()
        row1.setSpacing(8)
        self.btn_open = QPushButton("打开音视频")
        self.btn_lyric = QPushButton("上传LRC歌词")
        self.btn_sub = QPushButton("加载SRT字幕")
        self.btn_play = QPushButton("播放/暂停")
        self.btn_stop = QPushButton("停止")
        self.btn_loop = QPushButton("单曲循环")
        self.btn_open_folder = QPushButton("打开文件夹")
        # 设置默认封面按钮
        self.btn_set_cover = QPushButton("设置默认封面")

        self.cbx_speed = QComboBox()
        self.cbx_speed.addItems(["0.5x", "0.7x", "1.0x", "1.2x", "1.5x", "2.0x"])
        self.cbx_speed.setCurrentText("1.0x")

        # 依次添加按钮
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

        # 第二行：进度条 + 音量条（已删除频谱单选框）
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

        # 绑定所有控件事件
        self.btn_open.clicked.connect(self.open_media)
        self.btn_lyric.clicked.connect(self.load_lrc_file)
        self.btn_sub.clicked.connect(self.load_subtitle)
        self.btn_play.clicked.connect(self.play_pause)
        self.btn_stop.clicked.connect(self.stop_play)
        self.btn_loop.clicked.connect(self.toggle_loop)
        self.btn_open_folder.clicked.connect(self.open_file_folder)
        self.btn_set_cover.clicked.connect(self.set_custom_default_cover)
        self.cbx_speed.currentTextChanged.connect(self.set_play_speed)
        self.slider_pos.sliderMoved.connect(self.seek_pos)
        self.slider_vol.valueChanged.connect(self.set_volume)

        control_layout.addLayout(row1)
        control_layout.addLayout(row2)
        left_layout.addLayout(control_layout)

        # ========== 右侧媒体信息面板（可拖拽宽度） ==========
        right_widget = QWidget()
        right_widget.setMinimumWidth(150)  # 设置最小宽度阈值
        right_layout = QVBoxLayout(right_widget)
        right_layout.setSpacing(10)

        # 网络串流功能
        stream_layout = QHBoxLayout()
        stream_layout.setSpacing(5)
        self.stream_url_edit = QLabel()
        self.stream_url_edit.setText("网络串流 URL:")
        self.stream_url_input = QComboBox()
        self.stream_url_input.setEditable(True)
        self.stream_url_input.addItems(["https://example.com/stream", "rtsp://example.com/camera"])
        self.stream_url_input.setPlaceholderText("请输入网络串流地址")
        self.btn_stream = QPushButton("播放串流")
        stream_layout.addWidget(self.stream_url_edit)
        stream_layout.addWidget(self.stream_url_input)
        stream_layout.addWidget(self.btn_stream)
        right_layout.addLayout(stream_layout)

        # 绑定网络串流事件
        self.btn_stream.clicked.connect(self.play_stream)

        # ========== 媒体信息区域 ==========
        title = QLabel("媒体信息")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        right_layout.addWidget(title)
        self.info_panel = QListWidget()
        right_layout.addWidget(self.info_panel)

        gpl_layout = QHBoxLayout()
        gpl_layout.addStretch()
        
        # 添加 qrstudio-icon（缩放为136x68）
        self.qrstudio_label = QLabel()
        qrstudio_icon_path = os.path.join(os.path.dirname(__file__), "qrstudio-icon.png")
        if os.path.exists(qrstudio_icon_path):
            pixmap = QPixmap(qrstudio_icon_path).scaled(
                136, 68,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation
            )
            self.qrstudio_label.setPixmap(pixmap)
            self.qrstudio_label.setCursor(Qt.CursorShape.PointingHandCursor)
            self.qrstudio_label.mousePressEvent = self.open_qrstudio_link
        gpl_layout.addWidget(self.qrstudio_label)
        
        # GPL logo
        self.gpl_label = QLabel()
        gpl_logo_path = os.path.join(os.path.dirname(__file__), "gplv3-with-text-136x68.png")
        if os.path.exists(gpl_logo_path):
            self.gpl_label.setPixmap(QPixmap(gpl_logo_path))
            self.gpl_label.setCursor(Qt.CursorShape.PointingHandCursor)
            self.gpl_label.mousePressEvent = self.open_gpl_link
        gpl_layout.addWidget(self.gpl_label)
        right_layout.addLayout(gpl_layout)

        # 添加到分割器，支持像素级自由拖拽
        splitter.addWidget(left_widget)
        splitter.addWidget(right_widget)
        # 设置初始宽度比例为 3:1（左侧75%，右侧25%）
        splitter.setSizes([768, 256])

    def open_gpl_link(self, event):
        QDesktopServices.openUrl(QUrl("https://www.gnu.org/licenses/gpl-3.0"))

    def open_qrstudio_link(self, event):
        QDesktopServices.openUrl(QUrl("https://github.com/liqirui1145-create/player"))  # 替换为实际链接

    # ====================== 自动加载项目目录中的默认封面 ======================
    def load_default_cover_from_file(self):
        """自动加载项目目录中的默认封面图片（保持原始分辨率）"""
        cover_path = os.path.join(os.path.dirname(__file__), self.DEFAULT_COVER_FILENAME)
        if os.path.exists(cover_path):
            try:
                # 保存原始分辨率的图片，不进行缩放
                self.custom_default_cover = QPixmap(cover_path)
            except Exception:
                self.custom_default_cover = None

    # ====================== 核心：设置用户自定义默认封面 ======================
    def set_custom_default_cover(self):
        """打开文件选择框，让用户上传图片作为全局默认封面（保持原始分辨率）"""
        file_path, _ = QFileDialog.getOpenFileName(
            self, "选择默认封面图片", "",
            "图片文件 (*.jpg *.jpeg *.png *.bmp *.gif)"
        )
        if not file_path:
            return

        # 加载图片，保持原始分辨率
        try:
            # 保存原始分辨率的图片
            self.custom_default_cover = QPixmap(file_path)
            QMessageBox.information(self, "设置成功", "默认封面已更换！\n无专辑封面的音频将自动展示该图片")
            # 如果当前正在播放无封面音频，立即刷新显示
            if self.cur_media_path and not self.is_video:
                self.show_default_cover()
        except Exception:
            QMessageBox.warning(self, "加载失败", "图片格式错误或文件损坏！")

    # ====================== 展示默认封面（优先用户上传图片） ======================
    def show_default_cover(self):
        """音频无内嵌封面时，展示默认封面（原始分辨率自适应显示）"""
        if self.custom_default_cover is not None:
            # 存在用户上传的封面，以原始分辨率自适应显示
            self.video_label.setPixmap(self.custom_default_cover.scaled(
                self.video_label.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation
            ))
        else:
            # 未上传封面，显示简易占位图
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

    # ====================== 全局键盘事件处理（快捷键） ======================
    def handle_global_key_press(self, event):
        """全局快捷键处理 - 按键按下"""
    # ====================== 全局键盘事件处理（快捷键） ======================
    def handle_global_key_press(self, event):
        """全局快捷键处理 - 按键按下"""
        if event.isAutoRepeat():
            return False
        key = event.key()
        handled = True
        
        # 网络串流模式：只允许音量调节
        if self.is_streaming:
            # 上方向键：音量+5
            if key == Qt.Key.Key_Up:
                vol = self.media_player.audio_get_volume()
                self.media_player.audio_set_volume(min(vol + 5, 100))
                self.slider_vol.setValue(self.media_player.audio_get_volume())
            # 下方向键：音量-5
            elif key == Qt.Key.Key_Down:
                vol = self.media_player.audio_get_volume()
                self.media_player.audio_set_volume(max(vol - 5, 0))
                self.slider_vol.setValue(self.media_player.audio_get_volume())
            else:
                handled = False
            return handled
        
        # 本地文件模式：所有快捷键可用
        # 空格：短按暂停/播放，长按临时2倍速
        if key == Qt.Key.Key_Space:
            self.space_pressed = True
            self.is_space_long = False
            self.original_speed = self.cur_speed
            self.cur_speed = 2.0
            self.set_play_speed(str(self.cur_speed))
            self.space_timer.start()
        # 左方向键：回退10秒
        elif key == Qt.Key.Key_Left:
            current_ms = self.media_player.get_time()
            self.media_player.set_time(max(current_ms - 10000, 0))
        # 右方向键：快进10秒
        elif key == Qt.Key.Key_Right:
            current_ms = self.media_player.get_time()
            total_ms = self.media_player.get_length()
            self.media_player.set_time(min(current_ms + 10000, total_ms))
        # 上方向键：音量+5
        elif key == Qt.Key.Key_Up:
            vol = self.media_player.audio_get_volume()
            self.media_player.audio_set_volume(min(vol + 5, 100))
            self.slider_vol.setValue(self.media_player.audio_get_volume())
        # 下方向键：音量-5
        elif key == Qt.Key.Key_Down:
            vol = self.media_player.audio_get_volume()
            self.media_player.audio_set_volume(max(vol - 5, 0))
            self.slider_vol.setValue(self.media_player.audio_get_volume())
        # Delete键：删除文件（二次确认）
        elif key == Qt.Key.Key_Delete:
            self.delete_current_media()
        else:
            handled = False
        
        return handled  # 返回True表示事件已处理，不再传递
    
    def handle_global_key_release(self, event):
        """全局快捷键处理 - 按键释放"""
        key = event.key()
        handled = False
        
        # 网络串流模式：禁用空格键释放事件（避免暂停/播放）
        if self.is_streaming:
            return handled
        
        if key == Qt.Key.Key_Space and self.space_pressed:
            self.space_pressed = False
            self.space_timer.stop()
            # 短按空格：切换暂停/播放
            if not self.is_space_long:
                self.play_pause()
            # 恢复原始播放倍速
            self.cur_speed = self.original_speed
            self.set_play_speed(str(self.cur_speed))
            handled = True
        
        return handled  # 返回True表示事件已处理，不再传递
    
    def keyPressEvent(self, event):
        """主窗口按键事件 - 委托给全局处理"""
        if not self.handle_global_key_press(event):
            super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        """主窗口按键释放事件 - 委托给全局处理"""
        if not self.handle_global_key_release(event):
            super().keyReleaseEvent(event)
            handled = True
        
        return handled  # 返回True表示事件已处理，不再传递
    
    def keyPressEvent(self, event):
        """主窗口按键事件 - 委托给全局处理"""
        if not self.handle_global_key_press(event):
            super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        """主窗口按键释放事件 - 委托给全局处理"""
        if not self.handle_global_key_release(event):
            super().keyReleaseEvent(event)

    # ====================== 文件操作：打开文件夹 + 删除文件 ======================
    def open_file_folder(self):
        """一键打开当前文件所在文件夹"""
        if not self.cur_media_path or not os.path.exists(self.cur_media_path):
            return
        os.startfile(os.path.dirname(self.cur_media_path))

    def delete_current_media(self):
        """删除当前文件（二次弹窗确认）"""
        if not self.cur_media_path or not os.path.exists(self.cur_media_path):
            return
        reply = QMessageBox.question(
            self, "删除确认",
            f"确定永久删除该文件？\n{os.path.basename(self.cur_media_path)}\n操作无法撤销！",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            try:
                self.media_player.stop()
                os.remove(self.cur_media_path)
                # 重置界面状态
                self.cur_media_path = ""
                self.video_label.clear()
                self.video_label.setText("请打开媒体文件")
                self.lrc_list.clear()
                self.lrc_list.hide()
                self.info_panel.clear()
            except Exception:
                QMessageBox.warning(self, "删除失败", "文件被占用、权限不足或已删除！")

    # ====================== 读取音频元数据（采样率/声道/艺术家/专辑） ======================
    def read_audio_metadata(self, file_path):
        self.audio_metadata = {k: "--" for k in self.audio_metadata}
        if not MutagenFile or self.is_video:
            return
        try:
            audio = MutagenFile(file_path)
            if audio:
                # 音频技术参数
                if hasattr(audio.info, 'sample_rate'):
                    self.audio_metadata["sample_rate"] = f"{audio.info.sample_rate} Hz"
                if hasattr(audio.info, 'channels'):
                    self.audio_metadata["channels"] = f"{audio.info.channels} 声道"
                if hasattr(audio.info, 'bitrate'):
                    self.audio_metadata["bitrate"] = f"{audio.info.bitrate // 1000} kbps"
                # 标签信息（艺术家、专辑、歌曲标题）
                tags = audio.tags
                if tags:
                    self.audio_metadata["artist"] = tags.get('artist', tags.get('ARTIST', ['--']))[0]
                    self.audio_metadata["album"] = tags.get('album', tags.get('ALBUM', ['--']))[0]
                    self.audio_metadata["title"] = tags.get('title', tags.get('TITLE', ['--']))[0]
        except Exception:
            pass

    # ====================== 加载音频内嵌封面 ======================
    def load_audio_cover(self, file_path):
        """加载音频自带专辑封面（原始分辨率），无封面则展示默认封面"""
        try:
            if not MutagenFile:
                self.show_default_cover()
                return
            audio = MutagenFile(file_path)
            cover_data = None
            # 读取MP3/FLAC内嵌封面
            if isinstance(audio, MP3):
                for tag in audio.tags.values():
                    if tag.FrameID == "APIC":
                        cover_data = tag.data
                        break
            elif isinstance(audio, FLAC):
                for pic in audio.pictures:
                    cover_data = pic.data

            if cover_data:
                # 存在内嵌封面，以原始分辨率自适应展示
                img = Image.open(BytesIO(cover_data)).convert("RGBA")
                qimg = QImage(img.tobytes(), img.width, img.height, QImage.Format.Format_RGBA8888)
                pixmap = QPixmap.fromImage(qimg)
                # 自适应显示区域，保持原始宽高比
                self.video_label.setPixmap(pixmap.scaled(
                    self.video_label.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation
                ))
            else:
                # 无内嵌封面，展示用户自定义默认封面
                self.show_default_cover()
        except Exception:
            self.show_default_cover()

    # ====================== 播放模式、倍速、字幕、歌词 ======================
    def toggle_loop(self):
        """切换单曲循环"""
        self.loop_single = not self.loop_single
        self.btn_loop.setText("循环(开启)" if self.loop_single else "单曲循环")

    def bind_video_window(self):
        """绑定VLC视频渲染窗口"""
        self.media_player.set_hwnd(self.video_label.winId())

    def set_play_speed(self, text):
        """设置播放倍速"""
        self.cur_speed = float(text.replace("x", ""))
        self.media_player.set_rate(self.cur_speed)

    def load_subtitle(self):
        """加载SRT外挂字幕"""
        path, _ = QFileDialog.getOpenFileName(self, "加载SRT字幕", "", "*.srt")
        if path:
            self.media_player.subtitle_set_file(path)

    def parse_lrc(self, lrc_text):
        """解析LRC歌词"""
        reg = re.compile(r'\[(\d+):(\d+)\.(\d+)\]')
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
        """手动加载LRC歌词文件"""
        path, _ = QFileDialog.getOpenFileName(self, "选择LRC歌词", "", "*.lrc")
        if path:
            with open(path, "r", encoding="utf-8") as f:
                self.parse_lrc(f.read())
            self.lrc_list.clear()
            self.lrc_list.addItems(self.lrc_lines)
            self.lrc_list.setVisible(True)

    # ====================== 媒体信息面板刷新 ======================
    def show_media_info(self, path):
        self.info_panel.clear()
        duration = self.media_player.get_length()
        dur = f"{duration//60000}:{duration%60000//1000:02d}" if duration > 0 else "未知"
        res = f"{self.media_player.video_get_width()}×{self.media_player.video_get_height()}" if self.is_video else "纯音频"

        # 判断是否为网络串流
        is_stream = path.startswith(('http://', 'https://', 'rtsp://', 'rtmp://', 'udp://', 'tcp://'))

        if is_stream:
            # 网络串流：提取URL信息
            url = path
            # 提取协议
            protocol = url.split('://')[0].upper() + '://'
            # 提取域名/IP
            host = url.split('://')[1].split('/')[0] if '://' in url else url
            # 提取路径
            url_path = '/' + '/'.join(url.split('://')[1].split('/')[1:]) if '://' in url and len(url.split('://')[1].split('/')) > 1 else ''
            
            items = [
                f"1. 类型：网络串流",
                f"2. 协议：{protocol}",
                f"3. 地址：{host}",
                f"4. 路径：{url_path[:20]}",
                f"5. 时长：{dur}",
                f"6. 分辨率：{res}",
                f"7. 音频码率：{self.audio_metadata['bitrate']}",
                f"8. 声道：{self.audio_metadata['channels']}",
                f"9. 采样率：{self.audio_metadata['sample_rate']}",
                f"10. 倍速：{self.cur_speed}x"
            ]
        else:
            # 本地文件：使用原始逻辑
            try:
                stat = os.stat(path)
                items = [
                    f"1. 文件名：{os.path.basename(path)[:15]}",
                    f"2. 标题：{self.audio_metadata['title'][:15]}",
                    f"3. 格式：{os.path.splitext(path)[1][1:].upper()}",
                    f"4. 大小：{stat.st_size/1024/1024:.2f} MB",
                    f"5. 修改时间：{datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M')}",
                    f"6. 时长：{dur}",
                    f"7. 分辨率：{res}",
                    f"8. 音频码率：{self.audio_metadata['bitrate']}",
                    f"9. 声道：{self.audio_metadata['channels']}",
                    f"10. 采样率：{self.audio_metadata['sample_rate']}",
                    f"11. 艺术家：{self.audio_metadata['artist'][:15]}",
                    f"12. 专辑：{self.audio_metadata['album'][:15]}",
                    f"13. 倍速：{self.cur_speed}x"
                ]
            except Exception as e:
                items = [
                    f"1. 文件：{os.path.basename(path)[:15]}",
                    f"2. 错误：无法读取文件信息",
                    f"3. 时长：{dur}",
                    f"4. 分辨率：{res}",
                    f"5. 倍速：{self.cur_speed}x"
                ]

        self.info_panel.addItems(items)

    # ====================== 播放网络串流 ======================
    def play_stream(self):
        """播放网络串流"""
        try:
            url = self.stream_url_input.currentText().strip()
            if not url:
                QMessageBox.warning(self, "输入错误", "请输入有效的网络串流地址！")
                return

            # 验证URL格式
            if not (url.startswith('http://') or url.startswith('https://') or 
                    url.startswith('rtsp://') or url.startswith('rtmp://') or
                    url.startswith('udp://') or url.startswith('tcp://')):
                QMessageBox.warning(self, "格式错误", "请输入有效的网络协议地址（如 http://, https://, rtsp://）")
                return

            # 停止当前播放
            if self.media_player.is_playing():
                self.media_player.stop()

            self.cur_media_path = url
            self.is_streaming = True  # 标记为网络串流
            
            # 判断是否为视频流：检查扩展名或常见视频协议
            video_extensions = ('.mp4', '.mkv', '.avi', '.mov', '.flv', '.webm', '.ts', '.m3u8', 
                               '.mpeg', '.mpg', '.wmv', '.asf', '.m4v', '.3gp', '.ogv')
            video_protocols = ('rtsp://', 'rtmp://', 'rtp://')
            
            # HTTP/HTTPS 流默认视为视频流，除非明确是音频格式
            is_http_stream = url.lower().startswith('http://') or url.lower().startswith('https://')
            audio_extensions = ('.mp3', '.flac', '.wav', '.m4a', '.aac', '.ogg', '.opus', '.wma')
            
            self.is_video = url.lower().endswith(video_extensions) or \
                          any(protocol in url.lower() for protocol in video_protocols) or \
                          (is_http_stream and not url.lower().endswith(audio_extensions))
            
            self.lrc_list.clear()
            self.lrc_list.hide()
            
            # 禁用进度条（网络串流不支持拖拽进度）
            self.slider_pos.setEnabled(False)
            self.slider_pos.setStyleSheet("QSlider::groove:horizontal { background: #333; }")

            # 读取音频元数据（对于网络串流可能有限）
            self.read_audio_metadata(url)

            # 视频流：绑定渲染窗口到主窗口，清空封面
            if self.is_video:
                self.bind_video_window()
                self.video_label.clear()
                # 设置视频输出窗口
                self.media_player.set_hwnd(self.video_label.winId())
            else:
                # 音频流：展示默认封面
                self.show_default_cover()

            # 开始播放网络串流
            media = self.vlc_instance.media_new(url)
            # 增大网络缓存容量（毫秒），提高流畅度
            media.add_option(':network-caching=10000')
            # 增大缓冲区大小（KB）
            media.add_option(':buffer-size=8192')
            # 增加更多优化选项
            media.add_option(':live-caching=10000')
            media.add_option(':prefetch-buffer-size=8192')
            media.add_option(':input-buffer-size=8192')
            # 允许更大的抖动缓冲
            media.add_option(':clock-jitter=100')
            media.add_option(':clock-synchro=0')
            self.media_player.set_media(media)
            
            # 尝试播放
            result = self.media_player.play()
            if result == -1:
                QMessageBox.critical(self, "播放失败", "无法播放该网络串流，请检查URL是否正确或网络是否正常")
                return
                
            self.media_player.set_rate(self.cur_speed)

            # 刷新媒体信息面板
            self.show_media_info(url)

            # 将URL保存到历史记录（最多保存10条）
            current_items = [self.stream_url_input.itemText(i) for i in range(self.stream_url_input.count())]
            if url not in current_items:
                self.stream_url_input.insertItem(0, url)
                if self.stream_url_input.count() > 10:
                    self.stream_url_input.removeItem(self.stream_url_input.count() - 1)

            QMessageBox.information(self, "提示", "网络串流已开始播放\n如果长时间无画面，请检查网络连接或尝试其他地址")
            
        except Exception as e:
            QMessageBox.critical(self, "错误", f"播放网络串流时发生错误:\n{str(e)}")
            print(f"Stream error: {e}")

    # ====================== 打开媒体文件（自动加载同目录LRC） ======================
    def open_media(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "打开媒体文件", "",
            "媒体文件(*.mp3 *.flac *.wav *.mp4 *.mkv *.avi *.mov *.mpeg *.mpg *.flv *.webm *.m4a *.aac *.ogg *.opus *.wma *.alac *.aiff *.ape *.dsd *.sacd *.iso *.cue *.bin *.img *.dts *.dts-hd *.truehd *.mqa *.mqacd *.sacdsf *.sacdimg *.sacdcue)"
        )
        if not path:
            return

        self.cur_media_path = path
        self.is_streaming = False  # 标记为本地文件
        self.is_video = path.lower().endswith(('.mp4', '.mkv', '.avi', '.mov'))
        self.lrc_list.clear()
        self.lrc_list.hide()
        
        # 恢复进度条功能
        self.slider_pos.setEnabled(True)
        self.slider_pos.setStyleSheet("")

        # 读取音频元数据
        self.read_audio_metadata(path)

        if self.is_video:
            # 视频文件：绑定渲染窗口，清空封面
            self.bind_video_window()
            self.video_label.clear()
        else:
            # 音频文件：加载内嵌封面 / 默认封面
            self.load_audio_cover(path)

        # 开始播放
        media = self.vlc_instance.media_new(path)
        self.media_player.set_media(media)
        self.media_player.play()
        self.media_player.set_rate(self.cur_speed)

        # 刷新媒体信息面板
        self.show_media_info(path)

        # 自动加载【同目录同名LRC歌词】
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

    # ====================== 播放基础控制 ======================
    def play_pause(self):
        """播放 / 暂停"""
        if self.media_player.is_playing():
            self.media_player.pause()
        else:
            self.media_player.play()
            self.media_player.set_rate(self.cur_speed)

    def stop_play(self):
        """停止播放"""
        self.media_player.stop()
        self.slider_pos.setValue(0)

    def seek_pos(self, val):
        """进度条拖拽跳转"""
        self.media_player.set_time(val)

    def set_volume(self, vol):
        """音量调节"""
        self.media_player.audio_set_volume(vol)

    # ====================== 进度、歌词、单曲循环逻辑 ======================
    def update_progress_and_lrc(self):
        if not self.cur_media_path:
            return
        cur_ms = self.media_player.get_time()
        total_ms = self.media_player.get_length()

        # 同步进度条（网络串流时跳过）
        if not self.is_streaming and total_ms > 0:
            self.slider_pos.setRange(0, total_ms)
            self.slider_pos.setValue(cur_ms)

        # 同步歌词高亮
        if self.lrc_list.isVisible() and self.lrc_time_list:
            target = -1
            for i, t in enumerate(self.lrc_time_list):
                if cur_ms >= t:
                    target = i
            if target != -1 and target != self.cur_lrc_idx:
                self.cur_lrc_idx = target
                self.lrc_list.setCurrentRow(target)

        # 单曲循环：播放结束自动重播（网络串流不支持）
        if not self.is_streaming and self.loop_single and total_ms > 0 and cur_ms >= total_ms - 100:
            self.media_player.set_time(0)
            self.media_player.play()

    def play_downloaded_media(self, local_path):
        """播放下载的媒体文件"""
        if not os.path.exists(local_path):
            QMessageBox.warning(self, "播放失败", "文件不存在！")
            return

        # 使用现有的open_media方法播放
        self.open_media_from_path(local_path)

    def open_media_from_path(self, path):
        """从路径打开媒体文件（简化版）"""
        self.cur_media_path = path
        self.is_streaming = False
        self.is_video = path.lower().endswith(('.mp4', '.mkv', '.avi', '.mov'))
        self.lrc_list.clear()
        self.lrc_list.hide()
        
        self.slider_pos.setEnabled(True)
        self.slider_pos.setStyleSheet("")

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

        self.show_media_info(path)

        # 尝试加载同目录LRC
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

if HAS_WIN32:
    class PowerEventWindow:
        """监听Windows电源事件的隐藏窗口"""
        
        def __init__(self, media_player):
            self.media_player = media_player
            self.hwnd = None
            self.register_window()
        
        def register_window(self):
            """注册隐藏窗口以接收电源事件"""
            # 窗口类名
            wc = win32gui.WNDCLASS()
            wc.lpfnWndProc = self.wnd_proc
            wc.lpszClassName = "MediaPlayerPowerEventWindow"
            wc.hInstance = win32api.GetModuleHandle(None)
            
            # 注册窗口类
            try:
                win32gui.RegisterClass(wc)
            except:
                pass  # 类已注册
            
            # 创建隐藏窗口
            self.hwnd = win32gui.CreateWindowEx(
                0,
                "MediaPlayerPowerEventWindow",
                "",
                0,
                0, 0, 0, 0,
                0, 0,
                win32api.GetModuleHandle(None),
                None
            )
            
            # 无需额外注册，WM_POWERBROADCAST 消息会自动发送到所有顶级窗口
        
        def wnd_proc(self, hwnd, msg, wparam, lparam):
            """窗口消息处理函数"""
            if msg == win32con.WM_POWERBROADCAST:
                if wparam == win32con.PBT_APMSUSPEND:
                    # 系统即将进入休眠/睡眠
                    self.media_player.on_system_suspend()
                elif wparam == win32con.PBT_APMRESUMESUSPEND:
                    # 系统从休眠/睡眠恢复
                    self.media_player.on_system_resume()
            
            return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)


if __name__ == "__main__":
    print("Starting Media Player...")
    app = QApplication(sys.argv)
    print("QApplication created")
    win = MediaPlayer()
    print("MediaPlayer created")
    win.show()
    print("Window shown, entering event loop...")
    app.exec()
    print("Application exited")