"""Telegram 频道/群组音乐抓取（轻量版）。

设计目标：怎么轻怎么来。
- 客户端只用 Telethon（纯 Python，无编译）
- 无数据库：少量状态写在本目录的 tg_music.json
- 复用主程序已有的 M3U 解析/播放链路（写出 telegram.m3u 后交给播放器）

用法：由 p2.py 的「Telegram 音乐」按钮打开 TelegramMusicDialog。
"""

import os
import re
import json
import asyncio
import threading

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QInputDialog,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

try:
    from telethon import TelegramClient
    from telethon.errors import (
        FloodWaitError,
        SessionPasswordNeededError,
    )

    try:
        # 服务端“音乐”过滤器（只有 Music，没有通用 Audio）
        from telethon.tl.types import InputMessagesFilterMusic
    except Exception:  # 过滤器缺失不影响主功能
        InputMessagesFilterMusic = None

    HAS_TELETHON = True
except Exception:  # pragma: no cover - 缺少依赖时程序仍可运行
    TelegramClient = None
    FloodWaitError = SessionPasswordNeededError = Exception
    InputMessagesFilterMusic = None
    HAS_TELETHON = False


# --------------------------------------------------------------------------
# 轻量工具函数（可独立测试，不依赖网络/Telethon）
# --------------------------------------------------------------------------
_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(_DIR, "tg_music.json")
SESSION_FILE = os.path.join(_DIR, "tg_music.session")
DEFAULT_OUT_DIR = os.path.join(_DIR, "music")

# 推荐把凭据放到用户/系统环境变量里，避免写进项目目录
ENV_API_ID = "TG_API_ID"
ENV_API_HASH = "TG_API_HASH"
ENV_PHONE = "TG_PHONE"

# 这些字段不应长期留在项目目录（来自环境变量时绝不写入）
_CREDENTIAL_KEYS = ("api_id", "api_hash", "phone")


def env_value(name):
    """读取环境变量并去掉空白；空值返回 """
    value = os.environ.get(name)
    return value.strip() if isinstance(value, str) and value.strip() else ""


def get_env_credentials():
    """从环境变量读取 (api_id, api_hash, phone)"""
    return env_value(ENV_API_ID), env_value(ENV_API_HASH), env_value(ENV_PHONE)


_INVALID_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def load_config():
    """读取 tg_music.json（失败返回空字典）"""
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_config(data):
    """写入 tg_music.json"""
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def update_config(**kwargs):
    """在保留其它键（如 downloaded）的前提下更新配置。

    若凭据来自环境变量，则不把它们写进项目目录。
    """
    data = load_config()
    data.update(kwargs)
    if env_value(ENV_API_ID) and env_value(ENV_API_HASH):
        for key in _CREDENTIAL_KEYS:
            data.pop(key, None)
    save_config(data)


def sanitize_filename(name, max_len=120):
    """把歌曲标题/艺术家清洗成合法文件名"""
    name = _INVALID_CHARS.sub("_", name or "").strip().strip(".")
    name = re.sub(r"\s+", " ", name)
    return (name or "audio")[:max_len]


# 常见音频扩展名（用于识别“以文件形式发送的音频”）
AUDIO_FILE_EXTS = {
    ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus",
    ".wav", ".wma", ".ape", ".alac", ".aiff", ".mp2",
}

_MIME_EXT = {
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/flac": ".flac",
    "audio/x-flac": ".flac",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/aac": ".aac",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
}


def is_audio_document(message):
    """判断是否为“以文件形式发送的音频”（群组里很常见）"""
    doc = getattr(message, "document", None)
    if doc is None:
        return False
    mime = (getattr(doc, "mime_type", None) or "").lower()
    if mime.startswith("audio/"):
        return True
    file_attr = getattr(message, "file", None)
    ext = (getattr(file_attr, "ext", None) or "").lower()
    return ext in AUDIO_FILE_EXTS


def is_audio_message(message):
    """音乐（带元数据）或以文件形式发送的音频"""
    if getattr(message, "audio", None) is not None:
        return True
    return is_audio_document(message)


def message_audio_title(message):
    """取显示名：优先 艺术家-标题，其次文件名，最后 audio_<id>"""
    audio = getattr(message, "audio", None)
    if audio is not None:
        title = getattr(audio, "title", None) or ""
        performer = getattr(audio, "performer", None) or ""
        if title and performer:
            return f"{performer} - {title}"
        if title or performer:
            return title or performer
    file_attr = getattr(message, "file", None)
    name = getattr(file_attr, "name", None) or ""
    if name:
        return os.path.splitext(os.path.basename(name))[0]
    return f"audio_{getattr(message, 'id', 'unknown')}"


def audio_ext(message):
    """推断音频扩展名：先看 file.ext，再看 mime，兜底 .mp3"""
    file_attr = getattr(message, "file", None)
    ext = getattr(file_attr, "ext", None)
    if isinstance(ext, str) and ext.startswith(".") and len(ext) <= 5:
        return ext
    doc = getattr(message, "document", None)
    mime = (getattr(doc, "mime_type", None) or "").lower()
    return _MIME_EXT.get(mime, ".mp3")


def write_m3u(path, entries):
    """把 [(显示名, 文件绝对路径), ...] 写成 .m3u 播放列表"""
    lines = ["#EXTM3U"]
    for name, file_path in entries:
        lines.append(f"#EXTINF:-1,{name}")
        lines.append(os.path.abspath(file_path))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# --------------------------------------------------------------------------
# 后台抓取线程
# --------------------------------------------------------------------------
class TelegramMusicWorker(QThread):
    """在后台线程运行 Telethon：登录 → 遍历频道 → 下载音频 → 写 M3U。

    登录需要的验证码/两步密码通过信号请求 UI 输入：
    工作线程 emit 信号后阻塞等待，UI 线程弹输入框并回填。
    """

    progress = pyqtSignal(int, int, str)   # 已完成, 总数, 当前项
    log = pyqtSignal(str)
    need_code = pyqtSignal()
    need_password = pyqtSignal()
    done = pyqtSignal(str, list)           # m3u 路径, [(显示名, 文件路径), ...]
    error = pyqtSignal(str)

    def __init__(self, api_id, api_hash, phone, channels, limit,
                 out_dir, full_scan=False, parent=None):
        super().__init__(parent)
        self.api_id = int(api_id)
        self.api_hash = str(api_hash).strip()
        self.phone = str(phone).strip()
        self.channels = list(channels)
        self.limit = max(1, int(limit))
        self.out_dir = out_dir
        self.full_scan = bool(full_scan)
        self._pending = None
        self._event = threading.Event()

    # 供 UI 线程回填
    def provide_code(self, code):
        self._pending = (code or "").strip()
        self._event.set()

    def provide_password(self, password):
        self._pending = password or ""
        self._event.set()

    def _ask(self, signal):
        self._pending = None
        self._event.clear()
        signal.emit()
        self._event.wait()
        return self._pending or ""

    def run(self):
        try:
            asyncio.run(self._main())
        except Exception as exc:  # pragma: no cover - 运行时错误回报 UI
            self.error.emit(f"{type(exc).__name__}: {exc}")

    # ---- 实际逻辑 ----
    async def _main(self):
        if not HAS_TELETHON:
            self.error.emit("未安装 telethon，请执行：pip install telethon")
            return

        client = TelegramClient(SESSION_FILE, self.api_id, self.api_hash)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                self.log.emit("未登录：正在发送验证码…")
                await client.send_code_request(self.phone)
                code = self._ask(self.need_code)
                try:
                    await client.sign_in(phone=self.phone, code=code)
                except SessionPasswordNeededError:
                    password = self._ask(self.need_password)
                    await client.sign_in(password=password)
            self.log.emit("登录成功")

            entries = await self._collect(client)
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

        if not entries:
            self.error.emit("没有下载到任何音频（检查频道名、权限或该频道是否含音乐）")
            return

        os.makedirs(self.out_dir, exist_ok=True)
        playlist = os.path.join(self.out_dir, "telegram.m3u")
        write_m3u(playlist, entries)
        self.log.emit(f"已生成播放列表：{playlist}")
        self.done.emit(playlist, entries)

    async def _collect(self, client):
        os.makedirs(self.out_dir, exist_ok=True)
        state = load_config()
        downloaded = state.get("downloaded", {})
        if not isinstance(downloaded, dict):
            downloaded = {}
        last_ids = state.get("last_id", {})
        if not isinstance(last_ids, dict):
            last_ids = {}

        entries = []
        max_total = self.limit * len(self.channels)
        count = 0
        skipped_locked = 0

        for channel in self.channels:
            self.log.emit(f"读取频道/群组：{channel}")
            try:
                entity = await client.get_entity(channel)
            except Exception as exc:
                self.log.emit(f"  跳过（无法解析 {channel}）：{exc}")
                continue

            kwargs = {"limit": self.limit}
            if not self.full_scan and InputMessagesFilterMusic is not None:
                # 服务端过滤，避免把整群历史拉下来
                kwargs["filter"] = InputMessagesFilterMusic()
            last = last_ids.get(str(channel))
            if last:
                # 增量：只取比上次更新的消息，按时间正序
                kwargs["min_id"] = int(last)
                kwargs["reverse"] = True

            new_last = int(last) if last else 0
            try:
                async for message in client.iter_messages(entity, **kwargs):
                    new_last = max(new_last, message.id)
                    # 受限内容（禁止保存/转发）无法下载
                    if getattr(message, "noforwards", False):
                        skipped_locked += 1
                        continue
                    if not is_audio_message(message):
                        continue

                    count += 1
                    display = message_audio_title(message)
                    self.progress.emit(count, max_total, display)

                    key = f"{message.chat_id}:{message.id}"
                    cached = downloaded.get(key)
                    if cached and os.path.exists(os.path.join(self.out_dir, cached)):
                        self.log.emit(f"  已存在：{display}")
                        entries.append((display, os.path.join(self.out_dir, cached)))
                        continue

                    dest = self._unique_path(display, audio_ext(message))
                    self.log.emit(f"  下载：{display}")
                    try:
                        await client.download_media(message, file=dest)
                    except FloodWaitError as exc:
                        wait = getattr(exc, "seconds", 10) or 10
                        self.log.emit(f"  触发限流，等待 {wait}s 后重试…")
                        await asyncio.sleep(wait + 1)
                        await client.download_media(message, file=dest)

                    downloaded[key] = os.path.basename(dest)
                    update_config(downloaded=downloaded)
                    entries.append((display, dest))
                    await asyncio.sleep(0.4)  # 轻度限速，降低被限制风险
            except Exception as exc:
                self.log.emit(f"  读取中断：{exc}")

            if new_last:
                last_ids[str(channel)] = new_last
                update_config(last_id=last_ids)

        if skipped_locked:
            self.log.emit(f"已跳过 {skipped_locked} 条受限内容（禁止保存/转发）")
        return entries

    def _unique_path(self, display, ext=".mp3"):
        base = sanitize_filename(display)
        dest = os.path.join(self.out_dir, base + ext)
        index = 2
        while os.path.exists(dest):
            dest = os.path.join(self.out_dir, f"{base} ({index}){ext}")
            index += 1
        return dest


# --------------------------------------------------------------------------
# 配置对话框
# --------------------------------------------------------------------------
class TelegramMusicDialog(QDialog):
    """Telegram 音乐抓取窗口：填 API/频道 → 抓取 → 交给播放器"""

    def __init__(self, player, parent=None):
        super().__init__(parent)
        self.player = player
        self.worker = None
        self.setWindowTitle("Telegram 音乐")
        self.setMinimumWidth(560)

        cfg = load_config()
        env_api_id, env_api_hash, env_phone = get_env_credentials()
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        tip = QLabel(
            "抓取 Telegram 频道/群组里的音乐并生成播放列表。\n"
            f"建议把 API ID / API Hash 放到用户/系统环境变量 {ENV_API_ID}、"
            f"{ENV_API_HASH}（手机号可用 {ENV_PHONE}），这样不会写进项目目录；"
            "未设置时也可在下方直接填写。\n"
            "登录时按提示输入验证码（会话保存在本地）；"
            "群组需账号已加入，开启限制保存的聊天会跳过受限内容。"
        )
        tip.setWordWrap(True)
        layout.addWidget(tip)

        form = QFormLayout()
        self.ed_api_id = QLineEdit(env_api_id or str(cfg.get("api_id", "")))
        self.ed_api_hash = QLineEdit(env_api_hash or str(cfg.get("api_hash", "")))
        self.ed_phone = QLineEdit(env_phone or str(cfg.get("phone", "")))
        for field, env_name in (
            (self.ed_api_id, ENV_API_ID),
            (self.ed_api_hash, ENV_API_HASH),
            (self.ed_phone, ENV_PHONE),
        ):
            if env_value(env_name):
                field.setReadOnly(True)
                field.setToolTip(f"来自环境变量 {env_name}（不会写入项目目录）")
        self.ed_channels = QLineEdit(str(cfg.get("channels", "")))
        self.ed_channels.setPlaceholderText("频道/群组 @name 或 ID，多个用逗号分隔")
        self.ed_limit = QLineEdit(str(cfg.get("limit", 20)))
        self.ed_out_dir = QLineEdit(str(cfg.get("out_dir", DEFAULT_OUT_DIR)))
        self.cbx_full = QCheckBox("完整模式（扫描非音频附件，较慢但更全）")
        self.cbx_full.setToolTip(
            "默认只拉取音频类消息（服务端过滤，快）。\n"
            "完整模式会扫描所有消息，可抓到以普通文件形式发送的音频。"
        )
        self.cbx_full.setChecked(bool(cfg.get("full_scan", False)))
        form.addRow("API ID：", self.ed_api_id)
        form.addRow("API Hash：", self.ed_api_hash)
        form.addRow("手机号：", self.ed_phone)
        form.addRow("频道/群组：", self.ed_channels)
        form.addRow("每项数量：", self.ed_limit)
        form.addRow("保存目录：", self.ed_out_dir)
        form.addRow("", self.cbx_full)
        layout.addLayout(form)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setValue(0)
        layout.addWidget(self.progress)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(140)
        layout.addWidget(self.log_view)

        row = QHBoxLayout()
        self.btn_start = QPushButton("开始抓取")
        self.btn_start.clicked.connect(self.start_fetch)
        self.btn_save = QPushButton("保存设置")
        self.btn_save.clicked.connect(self.save_settings)
        self.btn_close = QPushButton("关闭")
        self.btn_close.clicked.connect(self.close)
        row.addWidget(self.btn_start)
        row.addWidget(self.btn_save)
        row.addStretch()
        row.addWidget(self.btn_close)
        layout.addLayout(row)

        if not HAS_TELETHON:
            self.btn_start.setEnabled(False)
            self._append("未检测到 telethon：请执行 pip install telethon 后重开本窗口。")

    # ---- UI 辅助 ----
    def _append(self, text):
        self.log_view.appendPlainText(text)

    def _form_values(self):
        return {
            "api_id": self.ed_api_id.text().strip(),
            "api_hash": self.ed_api_hash.text().strip(),
            "phone": self.ed_phone.text().strip(),
            "channels": self.ed_channels.text().strip(),
            "limit": self.ed_limit.text().strip(),
            "out_dir": self.ed_out_dir.text().strip() or DEFAULT_OUT_DIR,
            "full_scan": self.cbx_full.isChecked(),
        }

    def save_settings(self):
        values = self._form_values()
        update_config(**values)
        self._append("设置已保存")

    def start_fetch(self):
        values = self._form_values()
        if not (values["api_id"] and values["api_hash"] and values["phone"]):
            QMessageBox.warning(self, "缺少信息", "请填写 API ID、API Hash 和手机号。")
            return
        try:
            api_id = int(values["api_id"])
        except ValueError:
            QMessageBox.warning(self, "格式错误", "API ID 必须是数字。")
            return
        channels = [
            c.strip()
            for c in re.split(r"[,;\n]+", values["channels"])
            if c.strip()
        ]
        if not channels:
            QMessageBox.warning(self, "缺少频道", "请至少填写一个频道（@name 或 ID）。")
            return
        try:
            limit = max(1, int(values["limit"] or 20))
        except ValueError:
            limit = 20

        update_config(**values)
        self.log_view.clear()
        self.progress.setRange(0, 0)
        self.btn_start.setEnabled(False)

        self.worker = TelegramMusicWorker(
            api_id, values["api_hash"], values["phone"],
            channels, limit, values["out_dir"],
            full_scan=values["full_scan"], parent=self,
        )
        self.worker.log.connect(self._append)
        self.worker.progress.connect(self._on_progress)
        self.worker.need_code.connect(lambda: self._ask_code(self.worker))
        self.worker.need_password.connect(lambda: self._ask_password(self.worker))
        self.worker.done.connect(self._on_done)
        self.worker.error.connect(self._on_error)
        self.worker.finished.connect(lambda: self.btn_start.setEnabled(True))
        self.worker.start()

    def _ask_code(self, worker):
        code, ok = QInputDialog.getText(self, "Telegram 登录", "请输入验证码：")
        worker.provide_code(code if ok else "")

    def _ask_password(self, worker):
        password, ok = QInputDialog.getText(
            self, "Telegram 两步验证", "请输入两步验证密码：",
            QLineEdit.EchoMode.Password,
        )
        worker.provide_password(password if ok else "")

    def _on_progress(self, done, total, name):
        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(min(done, total))
        self._append(f"[{done}/{total}] {name}")

    def _on_done(self, playlist, entries):
        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        self._append(f"完成，共 {len(entries)} 首")

        # 复用主程序已有的 M3U 解析与播放列表窗口
        channels = []
        try:
            channels = self.player.parse_m3u_file(playlist)
        except Exception:
            channels = []
        if not channels:
            channels = [(name, path) for name, path in entries]
        if channels:
            self.player.open_channel_dialog(channels)

    def _on_error(self, message):
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self._append(f"出错：{message}")
        QMessageBox.warning(self, "抓取失败", message)
