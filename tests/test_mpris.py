# -*- coding: utf-8 -*-
"""MPRIS 模块测试。

不依赖桌面环境：只验证状态快照、类型转换、轨道 ID 生成与生命周期，
不启动真正的媒体控件交互（那部分用 gdbus 手工验证）。
"""

import time
import unittest

from PyQt6.QtWidgets import QApplication

import mpris_player
from mpris_player import (
    HAS_GIO,
    MprisController,
    MprisState,
    make_track_id,
    to_variant,
)

app = QApplication.instance() or QApplication([])

TRACK_PREFIX = "/org/mpris/MediaPlayer2/Track/"


class TrackIdTests(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(make_track_id("abc"), TRACK_PREFIX + "abc")

    def test_invalid_characters_are_replaced(self):
        # MPRIS 的 trackid 必须是合法的 D-Bus object path
        self.assertEqual(make_track_id("/tmp/a b.mp3"), TRACK_PREFIX + "_tmp_a_b_mp3")

    def test_empty_seed_falls_back(self):
        self.assertTrue(make_track_id("").startswith(TRACK_PREFIX))
        self.assertEqual(make_track_id(""), TRACK_PREFIX + "_")

    def test_numbers_cannot_start_a_segment(self):
        self.assertEqual(make_track_id("123abc"), TRACK_PREFIX + "_123abc")

    def test_chinese_seed_is_sanitized(self):
        # 中文 isalnum() 为 True，但不是合法的 object path 字符
        result = make_track_id("中文文件名.mp3")
        self.assertEqual(result, TRACK_PREFIX + "______mp3")
        self.assertTrue(result.isascii())

    def test_path_is_always_valid(self):
        import re as _re

        pattern = _re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
        for seed in ("a/b", "中文文件名.mp3", "###", "x" * 300, None, "123", "  "):
            path = make_track_id(seed)
            self.assertTrue(path.startswith(TRACK_PREFIX), seed)
            segment = path[len(TRACK_PREFIX):]
            self.assertLessEqual(len(segment), 65, seed)
            self.assertRegex(segment, pattern, seed)


@unittest.skipUnless(HAS_GIO, "需要 PyGObject(python3-gi)")
class ToVariantTests(unittest.TestCase):
    def test_scalar_types(self):
        self.assertEqual(to_variant(True).get_type_string(), "b")
        self.assertEqual(to_variant(42).get_type_string(), "x")
        self.assertEqual(to_variant(0.5).get_type_string(), "d")
        self.assertEqual(to_variant("标题").get_string(), "标题")
        self.assertEqual(to_variant(42).get_int64(), 42)

    def test_variant_passes_through(self):
        from gi.repository import GLib

        original = GLib.Variant("s", "keep")
        self.assertIs(to_variant(original), original)

    def test_dict_becomes_a_sv(self):
        result = to_variant({"title": "x", "count": 3})
        self.assertEqual(result.get_type_string(), "a{sv}")
        self.assertEqual(result.lookup_value("title", None).get_string(), "x")

    def test_list_becomes_string_array(self):
        result = to_variant(["a", "b"])
        self.assertEqual(result.get_type_string(), "as")
        self.assertEqual(list(result.get_strv()), ["a", "b"])


class MprisStateTests(unittest.TestCase):
    def test_defaults(self):
        state = MprisState()
        self.assertEqual(state.get("status"), "Stopped")
        self.assertEqual(state.get("volume"), 1.0)
        self.assertFalse(state.get("can_next"))
        self.assertEqual(state.get("loop_status"), "None")

    def test_update_reports_only_changed_fields(self):
        state = MprisState()
        self.assertEqual(state.update(status="Playing"), {"status"})
        self.assertEqual(state.update(status="Playing"), set())
        self.assertEqual(state.update(status="Paused", volume=0.5), {"status", "volume"})

    def test_snapshot_is_a_copy(self):
        state = MprisState()
        snapshot = state.snapshot()
        snapshot["status"] = "被外部改坏"
        self.assertEqual(state.get("status"), "Stopped")


@unittest.skipUnless(HAS_GIO, "需要 PyGObject(python3-gi)")
class MprisControllerTests(unittest.TestCase):
    def setUp(self):
        self.controller = MprisController()

    def test_update_track_fills_metadata(self):
        self.controller.update_track(
            title="标题", artist="艺人", album="专辑",
            length_us=180_000_000, art_url="file:///tmp/cover.jpg", track_seed="p1",
        )
        meta = self.controller.state.get("metadata")
        self.assertEqual(meta["xesam:title"].get_string(), "标题")
        self.assertEqual(list(meta["xesam:artist"].get_strv()), ["艺人"])
        self.assertEqual(meta["xesam:album"].get_string(), "专辑")
        self.assertEqual(meta["mpris:length"].get_int64(), 180_000_000)
        self.assertEqual(meta["mpris:artUrl"].get_string(), "file:///tmp/cover.jpg")
        self.assertEqual(meta["mpris:trackid"].unpack(), make_track_id("p1"))

    def test_update_track_skips_empty_optional_fields(self):
        self.controller.update_track(title="只有标题")
        meta = self.controller.state.get("metadata")
        self.assertIn("xesam:title", meta)
        self.assertIn("mpris:trackid", meta)
        self.assertNotIn("xesam:artist", meta)
        self.assertNotIn("mpris:length", meta)
        self.assertNotIn("mpris:artUrl", meta)

    def test_update_playback_sets_flags(self):
        self.controller.update_playback("Playing", next=True, seek=True)
        self.assertEqual(self.controller.state.get("status"), "Playing")
        self.assertTrue(self.controller.state.get("can_next"))
        self.assertTrue(self.controller.state.get("can_seek"))
        self.assertFalse(self.controller.state.get("can_previous"))

    def test_volume_is_clamped(self):
        self.controller.update_volume(2.5)
        self.assertEqual(self.controller.state.get("volume"), 1.0)
        self.controller.update_volume(-3)
        self.assertEqual(self.controller.state.get("volume"), 0.0)

    def test_loop_status_is_validated(self):
        self.controller.update_loop_status("Track")
        self.assertEqual(self.controller.state.get("loop_status"), "Track")
        self.controller.update_loop_status("不存在的值")
        self.assertEqual(self.controller.state.get("loop_status"), "None")

    def test_action_signals_are_wired(self):
        hits = []
        self.controller.play_pause_requested.connect(lambda: hits.append("play_pause"))
        self.controller.next_requested.connect(lambda: hits.append("next"))
        self.controller.previous_requested.connect(lambda: hits.append("previous"))
        self.controller.seek_requested.connect(hits.append)

        self.controller.play_pause_requested.emit()
        self.controller.next_requested.emit()
        self.controller.previous_requested.emit()
        self.controller.seek_requested.emit(1_000_000)

        self.assertEqual(hits, ["play_pause", "next", "previous", 1_000_000])

    def test_updates_without_server_are_safe(self):
        # 未启动 D-Bus 服务时（或 PyGObject 缺失时）更新状态不应抛异常
        self.controller.update_track(title="x", artist="y")
        self.controller.update_playback("Playing", next=True)
        self.controller.update_volume(0.3)
        self.controller.update_position(1234)
        self.controller.update_loop_status("Track")
        self.controller.update_shuffle(True)
        self.controller.notify_seeked(4321)
        self.assertIsNone(self.controller._server)

    def test_start_stop_service(self):
        # 同一进程内 /org/mpris/MediaPlayer2 只能导出一次，
        # 所以 start/stop 与重复 start 合在同一个用例里验证
        controller = MprisController(bus_name="org.mpris.MediaPlayer2.ruiplayer.unittest")
        self.assertTrue(controller.start())
        self.assertFalse(controller.start())  # 已在运行
        time.sleep(0.4)  # 等后台线程完成总线注册
        self.assertIsNotNone(controller._server)
        controller.stop()
        self.assertIsNone(controller._server)


class AvailabilityTests(unittest.TestCase):
    def test_is_available_matches_import(self):
        self.assertEqual(mpris_player.is_available(), HAS_GIO)


if __name__ == "__main__":
    unittest.main()
