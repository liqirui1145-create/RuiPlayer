# -*- coding: utf-8 -*-
"""MPRIS2 服务：把播放器接入 Linux 桌面的系统媒体控制。

接上之后可以用于：
- KDE/GNOME 顶栏的媒体控件、锁屏界面、通知中心的播放条
- 蓝牙耳机、键盘上的播放/暂停、上一曲、下一曲按键（桌面转发给 MPRIS 播放器）
- 命令行工具，如 playerctl、gdbus、dbus-send
- 把当前曲目的标题/艺术家/专辑/时长/封面回传给桌面显示

实现方式与约束：
- 用 PyGObject 的 Gio.DBusConnection（Linux 上实现 MPRIS 的标准做法）。
  没有安装 PyGObject（python3-gi）时 is_available() 返回 False，程序照常运行，
  只是没有系统集成。
- GLib 主循环跑在独立后台线程；所有播放动作都通过 Qt 信号排队回主线程执行，
  绝不跨线程操作 Qt/VLC 对象。
- 曲目信息、播放状态等由主线程写入带锁的快照，D-Bus 线程只读快照，避免竞态。

（PyQt6 自带的 QtDBus 无法做到：QDBusAbstractAdaptor 依赖 Q_CLASSINFO 指定接口名，
PyQt6 不导出该宏；QDBusVirtualObject 也未被 PyQt6 提供。）
"""

import threading
import warnings

from PyQt6.QtCore import QObject, pyqtSignal

try:
    import gi

    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib

    HAS_GIO = True
except Exception:  # 缺少 PyGObject 时优雅降级
    Gio = GLib = None
    HAS_GIO = False

MPRIS_PATH = "/org/mpris/MediaPlayer2"
ROOT_IFACE = "org.mpris.MediaPlayer2"
PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"
PROPS_IFACE = "org.freedesktop.DBus.Properties"

# 曲目变化时用的轨道 ID 前缀（MPRIS 要求是 object path）
TRACK_ID_PREFIX = "/org/mpris/MediaPlayer2/Track/"

MPRIS_XML = """
<node>
  <interface name="org.mpris.MediaPlayer2">
    <method name="Raise"/>
    <method name="Quit"/>
    <property name="CanQuit" type="b" access="read"/>
    <property name="CanRaise" type="b" access="read"/>
    <property name="HasTrackList" type="b" access="read"/>
    <property name="Identity" type="s" access="read"/>
    <property name="DesktopEntry" type="s" access="read"/>
    <property name="SupportedUriSchemes" type="as" access="read"/>
    <property name="SupportedMimeTypes" type="as" access="read"/>
  </interface>
  <interface name="org.mpris.MediaPlayer2.Player">
    <method name="Next"/>
    <method name="Previous"/>
    <method name="Pause"/>
    <method name="PlayPause"/>
    <method name="Stop"/>
    <method name="Play"/>
    <method name="Seek">
      <arg direction="in" type="x" name="Offset"/>
    </method>
    <method name="SetPosition">
      <arg direction="in" type="o" name="TrackId"/>
      <arg direction="in" type="x" name="Position"/>
    </method>
    <method name="OpenUri">
      <arg direction="in" type="s" name="Uri"/>
    </method>
    <property name="PlaybackStatus" type="s" access="read"/>
    <property name="LoopStatus" type="s" access="readwrite"/>
    <property name="Rate" type="d" access="readwrite"/>
    <property name="Shuffle" type="b" access="readwrite"/>
    <property name="Metadata" type="a{sv}" access="read"/>
    <property name="Volume" type="d" access="readwrite"/>
    <property name="Position" type="x" access="read"/>
    <property name="MinimumRate" type="d" access="read"/>
    <property name="MaximumRate" type="d" access="read"/>
    <property name="CanGoNext" type="b" access="read"/>
    <property name="CanGoPrevious" type="b" access="read"/>
    <property name="CanPlay" type="b" access="read"/>
    <property name="CanPause" type="b" access="read"/>
    <property name="CanSeek" type="b" access="read"/>
    <property name="CanControl" type="b" access="read"/>
    <signal name="Seeked">
      <arg type="x" name="Position"/>
    </signal>
  </interface>
</node>
"""


def is_available():
    """是否具备提供 MPRIS 服务的条件（需要 PyGObject）"""
    return HAS_GIO


def make_track_id(seed):
    """由任意标识生成合法的 MPRIS 轨道 ID（object path）

    D-Bus 的 object path 每段只允许 [A-Za-z0-9_]，且必须以字母或下划线开头。
    中文（isalnum() 为 True 但不是 ASCII）、空格、斜杠等都必须替换掉，
    数字开头的段要补一个下划线，否则 GLib.Variant("o", ...) 会构造失败。
    """
    safe = "".join(
        ch if (ch.isascii() and ch.isalnum()) else "_" for ch in str(seed)
    )[-64:]
    if not safe or safe[0].isdigit():
        safe = "_" + safe
    return f"{TRACK_ID_PREFIX}{safe}"


def to_variant(value):
    """把 Python 值转成 GLib.Variant。

    广播 PropertiesChanged 时，a{sv} 里的每个值都必须是 Variant 对象，
    PyGObject 不会自动包装，所以要显式转换。
    """
    if HAS_GIO and isinstance(value, GLib.Variant):
        return value
    if isinstance(value, bool):
        return GLib.Variant("b", value)
    if isinstance(value, int):
        return GLib.Variant("x", value)
    if isinstance(value, float):
        return GLib.Variant("d", value)
    if isinstance(value, str):
        return GLib.Variant("s", value)
    if isinstance(value, dict):
        return GLib.Variant("a{sv}", {k: to_variant(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return GLib.Variant("as", [str(v) for v in value])
    return GLib.Variant("s", str(value))


class MprisState:
    """线程安全的播放器状态快照：主线程写，D-Bus 线程读"""

    def __init__(self):
        self._lock = threading.Lock()
        self.identity = "RuiPlayer"
        self.desktop_entry = "ruiplayer"
        self.status = "Stopped"
        self.metadata = {}
        self.position_us = 0
        self.volume = 1.0
        self.loop_status = "None"
        self.shuffle = False
        self.rate = 1.0
        self.can_next = False
        self.can_previous = False
        self.can_play = True
        self.can_pause = False
        self.can_seek = False

    def update(self, **fields):
        """更新若干字段，返回实际发生变化的字段名集合"""
        changed = set()
        with self._lock:
            for key, value in fields.items():
                if getattr(self, key, None) != value:
                    setattr(self, key, value)
                    changed.add(key)
        return changed

    def get(self, field, default=None):
        with self._lock:
            return getattr(self, field, default)

    def snapshot(self):
        with self._lock:
            return dict(self.__dict__)


class MprisController(QObject):
    """MPRIS 服务控制器（主线程侧）。

    播放器把动作接到下面这些信号上即可；状态用 update_* 方法回写。
    """

    # 来自桌面/媒体键的动作请求（跨线程信号，自动排队到主线程）
    play_pause_requested = pyqtSignal()
    play_requested = pyqtSignal()
    pause_requested = pyqtSignal()
    stop_requested = pyqtSignal()
    next_requested = pyqtSignal()
    previous_requested = pyqtSignal()
    seek_requested = pyqtSignal(int)          # 相对偏移（微秒）
    set_position_requested = pyqtSignal(int)  # 绝对位置（微秒）
    volume_requested = pyqtSignal(float)
    loop_status_requested = pyqtSignal(str)
    shuffle_requested = pyqtSignal(bool)
    raise_requested = pyqtSignal()
    quit_requested = pyqtSignal()

    def __init__(self, bus_name="org.mpris.MediaPlayer2.ruiplayer", parent=None):
        super().__init__(parent)
        self.bus_name = bus_name
        self.state = MprisState()
        self._server = None
        self._thread = None

    # ---------- 生命周期 ----------
    def start(self):
        """启动 D-Bus 服务；返回是否成功"""
        if not HAS_GIO or self._thread is not None:
            return False
        self._server = _MprisDBusServer(self)
        self._thread = threading.Thread(
            target=self._server.run, name="mpris-dbus", daemon=True
        )
        self._thread.start()
        return True

    def stop(self):
        """停止服务并释放总线名"""
        if self._server is not None:
            self._server.quit()
        self._server = None
        self._thread = None

    # ---------- 状态回写（主线程调用） ----------
    def update_track(self, *, title=None, artist=None, album=None,
                     length_us=0, art_url=None, track_seed=None):
        """更新当前曲目信息（标题/艺术家/专辑/时长/封面）"""
        track_id = make_track_id(track_seed or title or "unknown")
        metadata = {"mpris:trackid": GLib.Variant("o", track_id)}
        if length_us and length_us > 0:
            metadata["mpris:length"] = GLib.Variant("x", int(length_us))
        if title:
            metadata["xesam:title"] = GLib.Variant("s", str(title))
        if artist:
            metadata["xesam:artist"] = GLib.Variant("as", [str(artist)])
        if album:
            metadata["xesam:album"] = GLib.Variant("s", str(album))
        if art_url:
            metadata["mpris:artUrl"] = GLib.Variant("s", str(art_url))
        self.state.update(metadata=metadata)
        self._emit_changed({"Metadata": metadata})

    def update_playback(self, status=None, **flags):
        """更新播放状态与各 Can* 标志"""
        payload = {}
        if status is not None:
            payload["status"] = status
        for key, value in flags.items():
            payload[f"can_{key}"] = bool(value)
        changed = self.state.update(**payload)
        if changed:
            self._emit_changed(
                {self._player_property_name(name): self._player_property_value(name)
                 for name in changed}
            )

    def update_volume(self, volume):
        """更新音量（0.0 ~ 1.0）"""
        volume = max(0.0, min(1.0, float(volume)))
        if self.state.update(volume=volume):
            self._emit_changed({"Volume": volume})

    def update_rate(self, rate):
        """更新播放倍速"""
        rate = float(rate)
        if self.state.update(rate=rate):
            self._emit_changed({"Rate": rate})

    def update_loop_status(self, status):
        """更新循环状态：None / Track / Playlist"""
        if status not in ("None", "Track", "Playlist"):
            status = "None"
        if self.state.update(loop_status=status):
            self._emit_changed({"LoopStatus": status})

    def update_shuffle(self, enabled):
        """更新随机播放状态"""
        if self.state.update(shuffle=bool(enabled)):
            self._emit_changed({"Shuffle": bool(enabled)})

    def update_position(self, position_us):
        """更新播放位置（用于 Position 属性；规范要求客户端轮询，无需发信号）"""
        self.state.update(position_us=max(0, int(position_us)))

    def notify_seeked(self, position_us):
        """跳转完成后通知桌面（Seeked 信号）"""
        if self._server is None:
            return
        self._server.emit_seeked(max(0, int(position_us)))

    # ---------- 内部 ----------
    def _player_property_name(self, state_field):
        mapping = {
            "status": "PlaybackStatus",
            "can_next": "CanGoNext",
            "can_previous": "CanGoPrevious",
            "can_play": "CanPlay",
            "can_pause": "CanPause",
            "can_seek": "CanSeek",
        }
        return mapping.get(state_field, state_field)

    def _player_property_value(self, state_field):
        return self.state.get(state_field)

    def _emit_changed(self, changed_props):
        """把属性变化广播给桌面（PropertiesChanged）"""
        if self._server is None or not changed_props:
            return
        self._server.emit_properties_changed(PLAYER_IFACE, changed_props)


class _MprisDBusServer:
    """在后台线程里跑 GLib 主循环并提供 D-Bus 接口。

    只读取 MprisState 快照、只通过 Qt 信号请求播放动作，不直接碰播放器。
    """

    def __init__(self, controller):
        self.controller = controller
        self.state = controller.state
        self._connection = None
        self._loop = None
        self._name_id = 0
        self._ready = threading.Event()

    # ---------- 线程入口 ----------
    def run(self):
        try:
            self._connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except Exception as exc:
            print(f"[MPRIS] 无法连接会话总线：{exc}")
            return
        if self._connection is None:
            print("[MPRIS] 无法连接会话总线（D-Bus 不可用）")
            return

        node = Gio.DBusNodeInfo.new_for_xml(MPRIS_XML)
        for iface_name in (ROOT_IFACE, PLAYER_IFACE):
            info = node.lookup_interface(iface_name)
            try:
                # PyGObject 把 register_object 标为过时（推荐 with_closures），
                # 但其功能完全正常，这里只在注册时隐去警告噪音
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", DeprecationWarning)
                    self._connection.register_object(
                        MPRIS_PATH, info, self._on_method_call,
                        self._on_get_property, self._on_set_property,
                    )
            except Exception as exc:
                # 同一进程里同一个 object path 只能导出一份接口；
                # 注册不上时只提示，不让整个播放器崩掉
                print(f"[MPRIS] 导出接口 {iface_name} 失败：{exc}")
                return

        self._name_id = Gio.bus_own_name_on_connection(
            self._connection, self.controller.bus_name,
            Gio.BusNameOwnerFlags.NONE, None, None,
        )
        self._ready.set()
        print(f"[MPRIS] 已注册 {self.controller.bus_name}")

        self._loop = GLib.MainLoop()
        try:
            self._loop.run()
        finally:
            if self._name_id:
                Gio.bus_unown_name(self._name_id)
            self._loop = None

    def quit(self):
        """停止主循环（可从任意线程调用）"""
        self._ready.wait(timeout=2)
        loop = self._loop
        if loop is not None:
            loop.quit()

    # ---------- D-Bus 回调（GLib 线程） ----------
    def _on_method_call(self, _conn, _sender, _path, iface, method, params, invocation):
        try:
            if iface == ROOT_IFACE:
                if method == "Raise":
                    self.controller.raise_requested.emit()
                elif method == "Quit":
                    self.controller.quit_requested.emit()
                else:
                    invocation.return_error_literal(
                        Gio.DBusError.quark(), Gio.DBusError.UNKNOWN_METHOD, method
                    )
                    return
                invocation.return_value(None)
                return

            if iface != PLAYER_IFACE:
                invocation.return_error_literal(
                    Gio.DBusError.quark(), Gio.DBusError.UNKNOWN_INTERFACE, iface
                )
                return

            if method in ("Play", "Pause", "PlayPause", "Stop", "Next", "Previous"):
                {
                    "Play": self.controller.play_requested,
                    "Pause": self.controller.pause_requested,
                    "PlayPause": self.controller.play_pause_requested,
                    "Stop": self.controller.stop_requested,
                    "Next": self.controller.next_requested,
                    "Previous": self.controller.previous_requested,
                }[method].emit()
            elif method == "Seek":
                (offset,) = params.unpack()
                self.controller.seek_requested.emit(int(offset))
            elif method == "SetPosition":
                track_id, position = params.unpack()
                if self._is_current_track(str(track_id)):
                    self.controller.set_position_requested.emit(int(position))
            elif method == "OpenUri":
                pass  # 不支持远程打开链接
            else:
                invocation.return_error_literal(
                    Gio.DBusError.quark(), Gio.DBusError.UNKNOWN_METHOD, method
                )
                return
            invocation.return_value(None)
        except Exception as exc:  # 回调里抛异常会让客户端一直等，必须兜住
            print(f"[MPRIS] 处理 {method} 出错：{exc}")
            try:
                invocation.return_error_literal(
                    Gio.DBusError.quark(), Gio.DBusError.FAILED, str(exc)
                )
            except Exception:
                pass

    def _on_get_property(self, _conn, _sender, _path, iface, prop):
        if iface == ROOT_IFACE:
            root_values = {
                "CanQuit": GLib.Variant("b", True),
                "CanRaise": GLib.Variant("b", True),
                "HasTrackList": GLib.Variant("b", False),
                "Identity": GLib.Variant("s", self.state.get("identity")),
                "DesktopEntry": GLib.Variant("s", self.state.get("desktop_entry")),
                "SupportedUriSchemes": GLib.Variant("as", ["file"]),
                "SupportedMimeTypes": GLib.Variant("as", ["audio/mpeg"]),
            }
            return root_values.get(prop)

        if iface == PLAYER_IFACE:
            values = {
                "PlaybackStatus": GLib.Variant("s", self.state.get("status")),
                "LoopStatus": GLib.Variant("s", self.state.get("loop_status")),
                "Rate": GLib.Variant("d", self.state.get("rate")),
                "Shuffle": GLib.Variant("b", self.state.get("shuffle")),
                "Metadata": GLib.Variant("a{sv}", self.state.get("metadata") or {}),
                "Volume": GLib.Variant("d", self.state.get("volume")),
                "Position": GLib.Variant("x", self.state.get("position_us")),
                "MinimumRate": GLib.Variant("d", 0.5),
                "MaximumRate": GLib.Variant("d", 2.0),
                "CanGoNext": GLib.Variant("b", self.state.get("can_next")),
                "CanGoPrevious": GLib.Variant("b", self.state.get("can_previous")),
                "CanPlay": GLib.Variant("b", self.state.get("can_play")),
                "CanPause": GLib.Variant("b", self.state.get("can_pause")),
                "CanSeek": GLib.Variant("b", self.state.get("can_seek")),
                "CanControl": GLib.Variant("b", True),
            }
            return values.get(prop)
        return None

    def _on_set_property(self, _conn, _sender, _path, iface, prop, value):
        if iface != PLAYER_IFACE:
            return False
        try:
            payload = value.unpack() if hasattr(value, "unpack") else value
            if prop == "Volume":
                self.controller.volume_requested.emit(float(payload))
                return True
            if prop == "LoopStatus":
                self.controller.loop_status_requested.emit(str(payload))
                return True
            if prop == "Shuffle":
                self.controller.shuffle_requested.emit(bool(payload))
                return True
            if prop == "Rate":
                return True  # 倍速由播放器自己控制，忽略外部设置
        except Exception as exc:
            print(f"[MPRIS] 设置属性 {prop} 出错：{exc}")
        return False

    def _is_current_track(self, track_id):
        """判断给定的 MPRIS 轨道 ID 是否是当前曲目

        注意两点：
        - 字典的 get 默认值会被无条件求值，不能在里面构造 GLib.Variant("o", "")
          （空字符串不是合法 object path，会抛错）
        - Variant.print_(True) 的 True 是“带类型标注”，会返回
          "objectpath '/...'" 这种形式，取值要用 unpack()
        """
        current = self.state.get("metadata") or {}
        current_id = current.get("mpris:trackid")
        if current_id is None:
            return True  # 没有曲目信息时不挑剔，允许跳转
        try:
            current_path = (current_id.unpack() if hasattr(current_id, "unpack")
                            else str(current_id))
        except Exception:
            return True
        return not current_path or current_path == track_id

    # ---------- 发信号 ----------
    def emit_properties_changed(self, iface, changed):
        if self._connection is None:
            return
        try:
            # a{sv} 的每个值都要是 Variant，否则 GLib 会报类型错
            wrapped = {key: to_variant(value) for key, value in changed.items()}
            payload = GLib.Variant("(sa{sv}as)", (iface, wrapped, []))
            self._connection.emit_signal(
                None, MPRIS_PATH, PROPS_IFACE, "PropertiesChanged", payload
            )
        except Exception as exc:
            print(f"[MPRIS] 广播属性失败：{exc}")

    def emit_seeked(self, position_us):
        if self._connection is None:
            return
        try:
            self._connection.emit_signal(
                None, MPRIS_PATH, PLAYER_IFACE, "Seeked",
                GLib.Variant("(x)", (int(position_us),)),
            )
        except Exception as exc:
            print(f"[MPRIS] 广播 Seeked 失败：{exc}")
