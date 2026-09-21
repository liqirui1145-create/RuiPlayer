# -*- coding: utf-8 -*-
"""音乐库扫描 / SHA256 去重 测试。

覆盖：
- 音频文件递归收集与扩展名过滤
- SHA256 计算（分块一致性、可取消）
- MusicScanWorker 的去重 / 增量复用 / 中断 / 失败统计
- 显示辅助函数与音乐库的持久化
"""

import hashlib
import os
import tempfile
import unittest

import music_scanner
import p2


def write_file(path, content=b"fake-audio-data"):
    """在指定路径写入测试文件（自动建目录）"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(content)
    return path


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def run_worker(self, folders, **kwargs):
        """在调用线程内直接执行扫描（不启动 QThread，便于断言）

        stats 里会额外带上短音频路径列表，方便断言被跳过的文件。
        """
        worker = music_scanner.MusicScanWorker(folders, **kwargs)
        found, dups, shorts, progress, stats = [], [], [], [], {}
        worker.file_found.connect(found.append)
        worker.duplicate_found.connect(dups.append)
        worker.short_found.connect(shorts.append)
        worker.progress.connect(lambda done, total: progress.append((done, total)))
        worker.finished_scan.connect(
            lambda result: stats.update(result, short_paths=[s["path"] for s in shorts])
        )
        worker.run()
        return found, dups, progress, stats

    def patch_track_duration(self, mapping):
        """把 read_track_meta 换成按文件名返回指定时长的假实现（None = 读不到）"""
        original = music_scanner.read_track_meta

        def fake(path):
            return {"title": None, "artist": None, "album": None,
                    "duration": mapping.get(os.path.basename(path))}

        music_scanner.read_track_meta = fake
        self.addCleanup(setattr, music_scanner, "read_track_meta", original)


class CollectAudioFilesTests(TempDirTestCase):
    def test_recursive_collect_and_filter(self):
        a = write_file(os.path.join(self.root, "a.mp3"))
        b = write_file(os.path.join(self.root, "sub", "b.FLAC"))
        write_file(os.path.join(self.root, "sub", "cover.jpg"))
        write_file(os.path.join(self.root, "note.txt"))

        found = music_scanner.collect_audio_files([self.root])
        # 目录内文件先收集，子目录递归在后；大小写扩展名都识别
        self.assertEqual(found, [a, b])

    def test_accepts_single_file_and_ignores_missing(self):
        a = write_file(os.path.join(self.root, "a.mp3"))
        found = music_scanner.collect_audio_files(
            [a, os.path.join(self.root, "不存在"), ""]
        )
        self.assertEqual(found, [a])

    def test_same_path_not_repeated_across_folders(self):
        a = write_file(os.path.join(self.root, "a.mp3"))
        found = music_scanner.collect_audio_files([self.root, self.root, a])
        self.assertEqual(found, [a])


class FileHashTests(TempDirTestCase):
    def test_digest_matches_hashlib(self):
        data = os.urandom(3000)
        path = write_file(os.path.join(self.root, "a.mp3"), data)
        self.assertEqual(
            music_scanner.file_sha256(path),
            hashlib.sha256(data).hexdigest(),
        )

    def test_chunk_size_does_not_change_digest(self):
        data = os.urandom(5000)
        path = write_file(os.path.join(self.root, "a.mp3"), data)
        self.assertEqual(
            music_scanner.file_sha256(path, chunk_size=7),
            music_scanner.file_sha256(path, chunk_size=4096),
        )

    def test_cancel_returns_none(self):
        path = write_file(os.path.join(self.root, "a.mp3"), os.urandom(100))
        self.assertIsNone(music_scanner.file_sha256(path, cancel=lambda: True))


class ScanWorkerTests(TempDirTestCase):
    def test_duplicate_content_is_skipped(self):
        a = write_file(os.path.join(self.root, "a.mp3"), b"same-bytes")
        b = write_file(os.path.join(self.root, "b.mp3"), b"same-bytes")
        write_file(os.path.join(self.root, "c.mp3"), b"other-bytes")

        found, dups, _, stats = self.run_worker([self.root])

        self.assertEqual(sorted(e["path"] for e in found), [a, os.path.join(self.root, "c.mp3")])
        self.assertEqual(stats["total"], 3)
        self.assertEqual(stats["added"], 2)
        self.assertEqual(stats["duplicate"], 1)
        self.assertEqual(len(dups), 1)
        self.assertEqual(dups[0]["path"], b)
        self.assertEqual(dups[0]["original"], a)

    def test_dedup_disabled_keeps_all_paths(self):
        write_file(os.path.join(self.root, "a.mp3"), b"same-bytes")
        write_file(os.path.join(self.root, "b.mp3"), b"same-bytes")

        found, dups, _, stats = self.run_worker([self.root], dedup=False)

        self.assertEqual(len(found), 2)
        self.assertEqual(dups, [])
        self.assertEqual(stats["duplicate"], 0)

    def test_unchanged_file_reuses_saved_digest(self):
        path = write_file(os.path.join(self.root, "a.mp3"), b"abc")
        st = os.stat(path)
        known = {path: {"path": path, "sha256": "deadbeef",
                        "size": int(st.st_size), "mtime": int(st.st_mtime)}}

        found, _, _, stats = self.run_worker(
            [self.root], known_entries=known, known_hashes={"deadbeef": path}
        )

        # 大小与修改时间都没变 → 直接复用历史摘要，不重新读盘
        self.assertEqual(found[0]["sha256"], "deadbeef")
        self.assertEqual(stats["existing"], 1)
        self.assertEqual(stats["added"], 0)
        self.assertEqual(stats["duplicate"], 0)

    def test_modified_file_recomputes_digest(self):
        path = write_file(os.path.join(self.root, "a.mp3"), b"abc")
        known = {path: {"path": path, "sha256": "deadbeef", "size": 3, "mtime": 0}}

        found, _, _, stats = self.run_worker(
            [self.root], known_entries=known, known_hashes={"deadbeef": path}
        )

        self.assertEqual(found[0]["sha256"], hashlib.sha256(b"abc").hexdigest())
        self.assertEqual(stats["existing"], 1)

    def test_rescan_same_folder_is_not_treated_as_duplicate(self):
        write_file(os.path.join(self.root, "a.mp3"), b"same-bytes")
        write_file(os.path.join(self.root, "b.mp3"), b"same-bytes")
        found, _, _, _ = self.run_worker([self.root])
        entries = {e["path"]: e for e in found}
        hashes = {e["sha256"]: e["path"] for e in found}

        found2, dups2, _, stats2 = self.run_worker(
            [self.root], known_entries=entries, known_hashes=hashes
        )

        # 已在库中的文件按路径跳过判重：不再重复入库，也不重复计数
        self.assertEqual(len(found2), 1)
        self.assertEqual(found2[0]["path"], found[0]["path"])
        self.assertEqual(stats2["existing"], 1)
        self.assertEqual(stats2["added"], 0)
        # b.mp3 内容与 a.mp3 相同且未能入库，仍会被识别为重复
        self.assertEqual([d["path"] for d in dups2], [os.path.join(self.root, "b.mp3")])
        self.assertEqual(stats2["duplicate"], 1)

    def test_progress_reports_each_file(self):
        for name in ("a.mp3", "b.mp3", "c.mp3"):
            write_file(os.path.join(self.root, name), name.encode())

        _, _, progress, stats = self.run_worker([self.root])

        self.assertEqual(progress[0], (0, 3))
        self.assertEqual(progress[-1], (3, 3))
        self.assertEqual(stats["total"], 3)

    def test_interruption_marks_cancelled(self):
        write_file(os.path.join(self.root, "a.mp3"), b"x")

        worker = music_scanner.MusicScanWorker([self.root])
        stats = {}
        worker.finished_scan.connect(lambda result: stats.update(result))
        # 未 start() 的 QThread 上 requestInterruption() 会被 Qt 忽略，
        # 这里直接模拟“用户已点击停止”后线程内的检查点
        worker.isInterruptionRequested = lambda: True
        worker.run()

        self.assertTrue(stats["cancelled"])

    def test_cancel_during_hashing_stops_scan(self):
        write_file(os.path.join(self.root, "a.mp3"), b"x")

        def cancel_after_first_chunk(path, chunk_size=0, cancel=None):
            # 模拟用户在大文件计算途中点停止：hash 返回 None
            return None

        original = music_scanner.file_sha256
        music_scanner.file_sha256 = cancel_after_first_chunk
        worker = music_scanner.MusicScanWorker([self.root])
        stats = {}
        worker.finished_scan.connect(lambda result: stats.update(result))
        worker.isInterruptionRequested = lambda: True
        try:
            worker.run()
        finally:
            music_scanner.file_sha256 = original

        self.assertTrue(stats["cancelled"])
        self.assertEqual(stats["failed"], 0)

    def test_read_error_counted_as_failed(self):
        write_file(os.path.join(self.root, "a.mp3"), b"x")
        original = music_scanner.file_sha256
        music_scanner.file_sha256 = lambda *a, **k: (_ for _ in ()).throw(OSError("boom"))
        try:
            found, _, _, stats = self.run_worker([self.root])
        finally:
            music_scanner.file_sha256 = original

        self.assertEqual(found, [])
        self.assertEqual(stats["failed"], 1)

    def test_empty_folder_finishes_cleanly(self):
        found, dups, progress, stats = self.run_worker([self.root])
        self.assertEqual((found, dups), ([], []))
        self.assertEqual(stats["total"], 0)
        self.assertEqual(progress, [(0, 0)])

    def test_short_audio_filtered_out(self):
        write_file(os.path.join(self.root, "beep.wav"), b"beep")
        write_file(os.path.join(self.root, "song.mp3"), b"song")
        self.patch_track_duration({"beep.wav": 1, "song.mp3": 200})

        found, _, _, stats = self.run_worker([self.root], min_duration=30)

        self.assertEqual(
            [e["path"] for e in found], [os.path.join(self.root, "song.mp3")]
        )
        self.assertEqual(stats["too_short"], 1)
        self.assertEqual(stats["short_paths"], [os.path.join(self.root, "beep.wav")])
        self.assertEqual(stats["added"], 1)

    def test_unknown_duration_is_kept(self):
        write_file(os.path.join(self.root, "unknown.mp3"), b"x")
        self.patch_track_duration({})  # duration 为 None：读不到时长时一律保留

        found, _, _, stats = self.run_worker([self.root], min_duration=30)

        self.assertEqual(len(found), 1)
        self.assertEqual(stats["too_short"], 0)

    def test_min_duration_zero_disables_filter(self):
        write_file(os.path.join(self.root, "beep.wav"), b"beep")
        self.patch_track_duration({"beep.wav": 1})

        found, _, _, stats = self.run_worker([self.root], min_duration=0)

        self.assertEqual(len(found), 1)
        self.assertEqual(stats["too_short"], 0)

    def test_short_file_not_registered_for_dedup(self):
        """被跳过的短音频不占用去重索引，不会连带把内容相同的长音频也判成重复"""
        a = write_file(os.path.join(self.root, "a.mp3"), b"same-bytes")
        b = write_file(os.path.join(self.root, "b.mp3"), b"same-bytes")
        self.patch_track_duration({"a.mp3": 1, "b.mp3": 120})

        found, dups, _, stats = self.run_worker([self.root], min_duration=30)

        self.assertEqual([e["path"] for e in found], [b])
        self.assertEqual(dups, [])
        self.assertEqual(stats["too_short"], 1)
        self.assertEqual(stats["duplicate"], 0)
        self.assertNotEqual(a, b)


class DisplayHelperTests(unittest.TestCase):
    def test_format_duration(self):
        self.assertEqual(music_scanner.format_duration(125), "02:05")
        self.assertEqual(music_scanner.format_duration(3725), "1:02:05")
        self.assertIsNone(music_scanner.format_duration(0))
        self.assertIsNone(music_scanner.format_duration(None))

    def test_format_size(self):
        self.assertEqual(music_scanner.format_size(512), "512 B")
        self.assertEqual(music_scanner.format_size(1536), "1.50 KB")
        self.assertEqual(music_scanner.format_size(None), "--")

    def test_display_text_prefers_tags(self):
        entry = {"path": "/m/song.mp3", "title": "标题", "artist": "艺人", "duration": 61}
        self.assertEqual(music_scanner.track_display_text(entry), "标题 - 艺人  [01:01]")

    def test_display_text_falls_back_to_filename(self):
        entry = {"path": "/m/我的歌.mp3", "title": None, "artist": None, "duration": None}
        self.assertEqual(music_scanner.track_display_text(entry), "我的歌")

    def test_display_text_skips_repeated_artist(self):
        entry = {"path": "/m/a.mp3", "title": "同名", "artist": "同名", "duration": None}
        self.assertEqual(music_scanner.track_display_text(entry), "同名")

    def test_tooltip_contains_path_and_sha(self):
        entry = {"path": "/m/a.mp3", "sha256": "a" * 64, "size": 1024, "album": "专辑"}
        tip = music_scanner.track_tooltip(entry)
        self.assertIn("/m/a.mp3", tip)
        self.assertIn("专辑", tip)
        self.assertIn("a" * 16, tip)


class LibraryPersistenceTests(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self._original_settings = p2.SETTINGS_FILE
        p2.SETTINGS_FILE = os.path.join(self.root, "ui_settings.json")

    def tearDown(self):
        p2.SETTINGS_FILE = self._original_settings
        super().tearDown()

    def test_roundtrip_entries_and_hashes(self):
        entries = {
            "/m/a.mp3": {"path": "/m/a.mp3", "sha256": "aa", "size": 10, "mtime": 5,
                         "title": "T", "artist": "A", "album": None, "duration": 125},
        }
        p2.persist_music_library(entries)

        loaded, hashes = p2.load_music_library()

        self.assertEqual(loaded["/m/a.mp3"]["title"], "T")
        self.assertEqual(loaded["/m/a.mp3"]["duration"], 125)
        self.assertEqual(hashes, {"aa": "/m/a.mp3"})

    def test_invalid_entries_are_ignored(self):
        p2.save_settings({"music_library": [
            "oops", {"no_path": 1}, {"path": ""}, {"path": "/m/ok.mp3", "sha256": "bb"},
        ]})

        loaded, hashes = p2.load_music_library()

        self.assertEqual(list(loaded), ["/m/ok.mp3"])
        self.assertEqual(hashes, {"bb": "/m/ok.mp3"})

    def test_missing_library_returns_empty(self):
        self.assertEqual(p2.load_music_library(), ({}, {}))

    def test_folders_roundtrip_keeps_other_settings(self):
        p2.save_ui_scale(1.5)
        p2.save_music_folders(["/music", "/backup"])

        self.assertEqual(p2.load_music_folders(), ["/music", "/backup"])
        self.assertEqual(p2.load_ui_scale(), 1.5)

    def test_invalid_folders_value(self):
        p2.save_settings({"music_folders": "not-a-list"})
        self.assertEqual(p2.load_music_folders(), [])

    def test_min_duration_roundtrip(self):
        self.assertEqual(p2.load_min_duration(), p2.DEFAULT_MIN_DURATION)
        p2.save_min_duration(0)
        self.assertEqual(p2.load_min_duration(), 0)
        p2.save_min_duration(15)
        self.assertEqual(p2.load_min_duration(), 15)
        # 不影响其它设置项
        self.assertEqual(p2.load_music_folders(), [])

    def test_min_duration_invalid_value_falls_back(self):
        p2.save_settings({"min_audio_duration": "abc"})
        self.assertEqual(p2.load_min_duration(), p2.DEFAULT_MIN_DURATION)
        p2.save_settings({"min_audio_duration": -5})
        self.assertEqual(p2.load_min_duration(), 0)

    def test_min_duration_choices_include_off_and_default(self):
        values = [seconds for _label, seconds in p2.MIN_DURATION_CHOICES]
        self.assertIn(0, values)  # “不过滤”
        self.assertIn(p2.DEFAULT_MIN_DURATION, values)


if __name__ == "__main__":
    unittest.main()
