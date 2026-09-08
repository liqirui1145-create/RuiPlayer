import sys
import os
from datetime import datetime
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                             QPushButton, QSlider, QFileDialog, QLabel, QListWidget, QComboBox,
                             QMessageBox)
from PyQt6.QtCore import Qt, QTimer

from config import SUPPORTED_FORMATS, TIMER_INTERVAL, SPACE_LONG_PRESS_THRESHOLD, VOLUME_STEP, SEEK_STEP
from lyric_parser import LyricParser
from metadata_reader import MetadataReader
from cover_manager import CoverManager

try:
    import vlc
except ImportError:
    print("错误：请安装 VLC 播放器，执行 pip install python-vlc")
    sys.exit()


class MediaPlayer(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("多媒体播放器")
        self.resize(1024, 600)
        self.showMaximized()

        self.vlc_instance = vlc.Instance("--quiet", "--aout=waveout")
        self.media_player = self.vlc_instance.media_player_new()

        self.cur_media_path = ""
        self.is_video = False
        self.lrc_lines = []
        self.lrc_time_list = []
        self.cur_lrc_idx = -1
        self.cur_speed = 1.0
        self.loop_single = False

        self.lyric_parser = LyricParser()
        self.metadata_reader = MetadataReader()
        self.cover_manager = CoverManager(self)

        self.space_pressed = False
        self.is_space_long = False
        self.original_speed = 1.0
        self.space_timer = QTimer()
        self.space_timer.setSingleShot(True)
        self.space_timer.setInterval(SPACE_LONG_PRESS_THRESHOLD)
        self.space_timer.timeout.connect(self.on_space_long_press)

        self.init_ui()

        self.timer = QTimer(interval=TIMER_INTERVAL)
        self.timer.timeout.connect(self.update_progress_and_lrc)
        self.timer.start()

    def on_space_long_press(self):
        self.is_space_long = True

    def init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.setSpacing(15)

        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setSpacing(15)

        self.video_label = QLabel()
        self.video_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.video_label.setMinimumSize(320, 320)
        self.video_label.setText("请打开媒体文件")
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

        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setSpacing(10)
        title = QLabel("媒体信息")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        right_layout.addWidget(title)
        self.info_panel = QListWidget()
        right_layout.addWidget(self.info_panel)

        main_layout.addWidget(left_widget, stretch=3)
        main_layout.addWidget(right_widget, stretch=1)

    def set_custom_default_cover(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "选择默认封面图片", "",
            "图片文件 (*.jpg *.jpeg *.png *.bmp *.gif)"
        )
        if not file_path:
            return

        if self.cover_manager.set_custom_default_cover(file_path):
            QMessageBox.information(self, "设置成功", "默认封面已更换！\n无专辑封面的音频将自动展示该图片")
            if self.cur_media_path and not self.is_video:
                self.video_label.setPixmap(self.cover_manager.get_audio_cover(self.cur_media_path))
        else:
            QMessageBox.warning(self, "加载失败", "图片格式错误或文件损坏！")

    def keyPressEvent(self, event):
        if event.isAutoRepeat():
            return
        key = event.key()

        if key == Qt.Key.Key_Space:
            self.space_pressed = True
            self.is_space_long = False
            self.original_speed = self.cur_speed
            self.cur_speed = 2.0
            self.set_play_speed(str(self.cur_speed))
            self.space_timer.start()
        elif key == Qt.Key.Key_Left:
            current_ms = self.media_player.get_time()
            self.media_player.set_time(max(current_ms - SEEK_STEP, 0))
        elif key == Qt.Key.Key_Right:
            current_ms = self.media_player.get_time()
            total_ms = self.media_player.get_length()
            self.media_player.set_time(min(current_ms + SEEK_STEP, total_ms))
        elif key == Qt.Key.Key_Up:
            vol = self.media_player.audio_get_volume()
            self.media_player.audio_set_volume(min(vol + VOLUME_STEP, 100))
            self.slider_vol.setValue(self.media_player.audio_get_volume())
        elif key == Qt.Key.Key_Down:
            vol = self.media_player.audio_get_volume()
            self.media_player.audio_set_volume(max(vol - VOLUME_STEP, 0))
            self.slider_vol.setValue(self.media_player.audio_get_volume())
        elif key == Qt.Key.Key_Delete:
            self.delete_current_media()
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        key = event.key()
        if key == Qt.Key.Key_Space and self.space_pressed:
            self.space_pressed = False
            self.space_timer.stop()
            if not self.is_space_long:
                self.play_pause()
            self.cur_speed = self.original_speed
            self.set_play_speed(str(self.cur_speed))
        super().keyReleaseEvent(event)

    def open_file_folder(self):
        if self.cur_media_path and os.path.exists(self.cur_media_path):
            os.startfile(os.path.dirname(self.cur_media_path))

    def delete_current_media(self):
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

    def bind_video_window(self):
        """将 VLC 视频画面嵌入视频标签，按平台使用正确的窗口句柄。
        Windows→set_hwnd；macOS→set_nsobject；Linux(X11/XWayland)→set_xwindow。
        set_hwnd 在 Linux 上无效，会导致有声音无画面。
        """
        win_id = int(self.video_label.winId())
        if sys.platform.startswith("win"):
            self.media_player.set_hwnd(win_id)
        elif sys.platform == "darwin":
            self.media_player.set_nsobject(win_id)
        else:
            self.media_player.set_xwindow(win_id)

    def set_play_speed(self, text):
        self.cur_speed = float(text.replace("x", ""))
        self.media_player.set_rate(self.cur_speed)

    def load_subtitle(self):
        path, _ = QFileDialog.getOpenFileName(self, "加载SRT字幕", "", "*.srt")
        if path:
            self.media_player.subtitle_set_file(path)

    def load_lrc_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择LRC歌词", "", "*.lrc")
        if path:
            self.lrc_time_list, self.lrc_lines = self.lyric_parser.load_from_file(path)
            self.lrc_list.clear()
            self.lrc_list.addItems(self.lrc_lines)
            self.lrc_list.setVisible(True)

    def show_media_info(self, path):
        self.info_panel.clear()
        stat = os.stat(path)
        duration = self.media_player.get_length()
        dur = f"{duration//60000}:{duration%60000//1000:02d}" if duration > 0 else "未知"
        res = f"{self.media_player.video_get_width()}×{self.media_player.video_get_height()}" if self.is_video else "纯音频"

        metadata = self.metadata_reader.read(path, self.is_video)

        items = [
            f"1. 文件名：{os.path.basename(path)[:15]}",
            f"2. 标题：{metadata['title'][:15]}",
            f"3. 格式：{os.path.splitext(path)[1][1:].upper()}",
            f"4. 大小：{stat.st_size/1024/1024:.2f} MB",
            f"5. 修改时间：{datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M')}",
            f"6. 时长：{dur}",
            f"7. 分辨率：{res}",
            f"8. 音频码率：{metadata['bitrate']}",
            f"9. 声道：{metadata['channels']}",
            f"10. 采样率：{metadata['sample_rate']}",
            f"11. 艺术家：{metadata['artist'][:15]}",
            f"12. 专辑：{metadata['album'][:15]}",
            f"13. 倍速：{self.cur_speed}x"
        ]
        self.info_panel.addItems(items)

    def open_media(self):
        all_formats = ' '.join([f'*.{ext}' for ext in SUPPORTED_FORMATS['audio'] + SUPPORTED_FORMATS['video']])
        path, _ = QFileDialog.getOpenFileName(
            self, "打开媒体文件", "",
            f"媒体文件({all_formats})"
        )
        if not path:
            return

        self.cur_media_path = path
        ext = os.path.splitext(path)[1][1:].lower()
        self.is_video = ext in SUPPORTED_FORMATS['video']

        self.lrc_list.clear()
        self.lrc_list.hide()

        if self.is_video:
            self.bind_video_window()
            self.video_label.clear()
        else:
            self.video_label.setPixmap(self.cover_manager.get_audio_cover(path))

        media = self.vlc_instance.media_new(path)
        self.media_player.set_media(media)
        self.media_player.play()
        self.media_player.set_rate(self.cur_speed)

        self.show_media_info(path)

        if not self.is_video:
            self.try_load_auto_lrc(path)

    def try_load_auto_lrc(self, path):
        media_dir = os.path.dirname(path)
        media_name = os.path.splitext(os.path.basename(path))[0]
        lrc_path = os.path.join(media_dir, f"{media_name}.lrc")
        if os.path.exists(lrc_path):
            self.lrc_time_list, self.lrc_lines = self.lyric_parser.load_from_file(lrc_path)
            if self.lrc_lines:
                self.lrc_list.addItems(self.lrc_lines)
                self.lrc_list.setVisible(True)

    def play_pause(self):
        if self.media_player.is_playing():
            self.media_player.pause()
        else:
            self.media_player.play()
            self.media_player.set_rate(self.cur_speed)

    def stop_play(self):
        self.media_player.stop()
        self.slider_pos.setValue(0)

    def seek_pos(self, val):
        self.media_player.set_time(val)

    def set_volume(self, vol):
        self.media_player.audio_set_volume(vol)

    def update_progress_and_lrc(self):
        if not self.cur_media_path:
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


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = MediaPlayer()
    win.show()
    app.exec()