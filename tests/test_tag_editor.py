# -*- coding: utf-8 -*-
"""音频标签读写模块测试。

- 用真实最小 WAV 文件验证 ID3 系的完整写入/读回/删除流程
- 用模拟对象验证 VorbisComment 与 MP4 两套映射
- 校验字段表本身（键唯一、覆盖用户要求的全部字段）
"""

import os
import struct
import tempfile
import unittest
import wave

import tag_editor
from mutagen import id3 as mutagen_id3


def make_wav(path, seconds=0.05, rate=8000):
    """生成一个真实的最小 WAV 文件（单声道 16bit）"""
    with wave.open(path, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(struct.pack("<h", 0) * int(rate * seconds))
    return path


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()


class FakeAudio:
    """模拟 mutagen 音频对象：__setitem__ 委托给 tags（FLAC/MP4 的行为）"""

    def __init__(self, tags=None):
        self.tags = {} if tags is None else tags

    def add_tags(self):
        return None

    def __setitem__(self, key, value):
        self.tags[key] = [value]

    def __contains__(self, key):
        return key in self.tags

    def __delitem__(self, key):
        del self.tags[key]


class WavId3Tests(TempDirTestCase):
    """ID3 系（WAV）真实文件读写"""

    def setUp(self):
        super().setUp()
        self.path = make_wav(os.path.join(self.root, "sample.wav"))

    def test_probe_container(self):
        self.assertEqual(tag_editor.probe_container(self.path), "id3")

    def test_default_values_are_empty(self):
        values = tag_editor.read_tags(self.path)
        self.assertEqual(set(values), {f.key for f in tag_editor.TAG_FIELDS})
        self.assertTrue(all(value == "" for value in values.values()))

    def test_write_and_read_back(self):
        ok, message = tag_editor.save_tags(self.path, {
            "title": "测试标题",
            "artist": "测试艺人",
            "album": "测试专辑",
            "albumartist": "合辑",
            "track": "3/12",
            "discnumber": "1/2",
            "genre": "摇滚",
            "date": "2026",
            "bpm": "128",
            "catalognumber": "CAT-001",
            "groupdesc": "GROUP-A",
            "grouping": "曲集A",
            "isrc": "CNA001234567",
            "language": "chi",
            "comment": "一条备注",
            "lyrics": "第一行\n第二行",
        })
        self.assertTrue(ok, message)

        values = tag_editor.read_tags(self.path)
        self.assertEqual(values["title"], "测试标题")
        self.assertEqual(values["artist"], "测试艺人")
        self.assertEqual(values["album"], "测试专辑")
        self.assertEqual(values["albumartist"], "合辑")
        self.assertEqual(values["track"], "3/12")
        self.assertEqual(values["discnumber"], "1/2")
        self.assertEqual(values["genre"], "摇滚")
        self.assertEqual(values["date"], "2026")
        self.assertEqual(values["bpm"], "128")
        self.assertEqual(values["catalognumber"], "CAT-001")
        self.assertEqual(values["groupdesc"], "GROUP-A")
        self.assertEqual(values["grouping"], "曲集A")
        self.assertEqual(values["isrc"], "CNA001234567")
        self.assertEqual(values["language"], "chi")
        self.assertEqual(values["comment"], "一条备注")
        self.assertEqual(values["lyrics"], "第一行\n第二行")

    def test_clear_field_removes_value(self):
        tag_editor.save_tags(self.path, {"title": "临时标题"})
        self.assertEqual(tag_editor.read_tags(self.path)["title"], "临时标题")

        ok, _ = tag_editor.save_tags(self.path, {"title": ""})
        self.assertTrue(ok)
        self.assertEqual(tag_editor.read_tags(self.path)["title"], "")

    def test_saving_subset_keeps_other_fields(self):
        tag_editor.save_tags(self.path, {"title": "A", "artist": "B"})
        tag_editor.save_tags(self.path, {"title": "C"})

        values = tag_editor.read_tags(self.path)
        self.assertEqual(values["title"], "C")
        self.assertEqual(values["artist"], "B")  # 上次写入的艺人保持不变

    def test_txxx_fields_are_independent(self):
        # 自定义帧共用 TXXX，但要按描述区分，不能互相覆盖
        tag_editor.save_tags(self.path, {"author": "作者甲", "catalognumber": "CAT-9"})
        values = tag_editor.read_tags(self.path)
        self.assertEqual(values["author"], "作者甲")
        self.assertEqual(values["catalognumber"], "CAT-9")

    def test_missing_file_returns_empty_values(self):
        values = tag_editor.read_tags(os.path.join(self.root, "missing.mp3"))
        self.assertEqual(values["title"], "")
        self.assertEqual(len(values), len(tag_editor.TAG_FIELDS))

    def test_unsupported_file_cannot_be_saved(self):
        path = os.path.join(self.root, "note.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("not audio")

        self.assertEqual(tag_editor.probe_container(path), "unknown")
        ok, message = tag_editor.save_tags(path, {"title": "X"})
        self.assertFalse(ok)
        self.assertIn("格式不受支持", message)


class VorbisMappingTests(unittest.TestCase):
    """VorbisComment（FLAC/OGG/Opus）键映射"""

    def setUp(self):
        self.audio = FakeAudio()

    def test_write_and_read(self):
        tag_editor._write_vorbis(self.audio, {"title": "标题", "bpm": "120"})

        self.assertEqual(self.audio.tags["title"], ["标题"])
        self.assertEqual(tag_editor._read_vorbis(self.audio.tags, "title"), "标题")
        self.assertEqual(tag_editor._read_vorbis(self.audio.tags, "bpm"), "120")

    def test_empty_value_deletes_key(self):
        tag_editor._write_vorbis(self.audio, {"title": "标题"})
        tag_editor._write_vorbis(self.audio, {"title": ""})
        self.assertNotIn("title", self.audio.tags)

    def test_grouping_and_content_group_use_distinct_keys(self):
        tag_editor._write_vorbis(self.audio, {"grouping": "曲集", "groupdesc": "组群"})
        self.assertEqual(self.audio.tags["contentgroup"], ["曲集"])
        self.assertEqual(self.audio.tags["grouping"], ["组群"])

    def test_unknown_key_returns_empty(self):
        self.assertEqual(tag_editor._read_vorbis(self.audio.tags, "nope"), "")
        self.assertEqual(tag_editor._read_vorbis(self.audio.tags, ""), "")


class Mp4MappingTests(unittest.TestCase):
    """MP4 原子映射（trkn/disk 是 (编号, 总数) 元组，tmpo 是整数）"""

    def setUp(self):
        self.audio = FakeAudio()

    def test_write_and_read(self):
        tag_editor._write_mp4(self.audio, {
            "title": "标题", "track": "3/12", "discnumber": "1/2", "bpm": "128",
        })

        self.assertEqual(self.audio.tags["\xa9nam"], ["标题"])
        self.assertEqual(self.audio.tags["trkn"], [(3, 12)])
        self.assertEqual(self.audio.tags["disk"], [(1, 2)])
        self.assertEqual(self.audio.tags["tmpo"], [128])

    def test_read_back_roundtrip(self):
        tag_editor._write_mp4(self.audio, {
            "title": "标题", "track": "3/12", "bpm": "128",
        })
        values = {
            f.key: tag_editor._read_mp4(self.audio.tags, f.mp4)
            for f in tag_editor.TAG_FIELDS
        }
        self.assertEqual(values["title"], "标题")
        self.assertEqual(values["track"], "3/12")
        self.assertEqual(values["bpm"], "128")

    def test_track_without_total(self):
        self.assertEqual(tag_editor._read_mp4({"trkn": [(5, 0)]}, "trkn"), "5")
        self.assertEqual(tag_editor._read_mp4({"trkn": [(0, 0)]}, "trkn"), "")

    def test_empty_value_deletes_atom(self):
        tag_editor._write_mp4(self.audio, {"title": "标题"})
        tag_editor._write_mp4(self.audio, {"title": ""})
        self.assertNotIn("\xa9nam", self.audio.tags)

    def test_invalid_number_falls_back_to_zero(self):
        tag_editor._write_mp4(self.audio, {"track": "abc/xyz"})
        self.assertEqual(self.audio.tags["trkn"], [(0, 0)])

    def test_unsupported_key_returns_empty(self):
        self.assertEqual(tag_editor._read_mp4({}, ""), "")


class Id3FrameTests(unittest.TestCase):
    """ID3 帧构造"""

    def test_text_frame(self):
        frame = tag_editor._build_id3_frame("TIT2", "标题")
        self.assertEqual(frame.text, ["标题"])

    def test_txxx_frame_keeps_description(self):
        frame = tag_editor._build_id3_frame("TXXX:AUTHOR", "作者")
        self.assertEqual(frame.desc, "AUTHOR")
        self.assertEqual(frame.text, ["作者"])

    def test_comment_and_lyrics_frames(self):
        comment = tag_editor._build_id3_frame("COMM", "备注")
        self.assertEqual(comment.text, ["备注"])
        lyrics = tag_editor._build_id3_frame("USLT", "歌词第一行\n第二行")
        self.assertEqual(lyrics.text, "歌词第一行\n第二行")

    def test_unknown_frame_returns_none(self):
        self.assertIsNone(tag_editor._build_id3_frame("ZZZZ", "x"))

    def test_read_id3_missing_frame(self):
        tags = mutagen_id3.ID3()
        self.assertEqual(tag_editor._read_id3(tags, "TIT2"), "")
        self.assertEqual(tag_editor._read_id3(tags, ""), "")

    def test_read_id3_comment_and_lyrics_with_language_subkey(self):
        """COMM/USLT 的帧键带语言子键（COMM::XXX），必须能读到（回归测试）"""
        tags = mutagen_id3.ID3()
        tags.add(mutagen_id3.COMM(encoding=3, lang="XXX", desc="", text=["备注内容"]))
        tags.add(mutagen_id3.USLT(encoding=3, lang="XXX", desc="", text="歌词内容"))

        self.assertEqual(tag_editor._read_id3(tags, "COMM"), "备注内容")
        self.assertEqual(tag_editor._read_id3(tags, "USLT"), "歌词内容")


class FieldTableTests(unittest.TestCase):
    """字段表一致性"""

    def test_keys_and_labels_are_unique(self):
        keys = [f.key for f in tag_editor.TAG_FIELDS]
        labels = [f.label for f in tag_editor.TAG_FIELDS]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(len(labels), len(set(labels)))

    def test_container_keys_do_not_collide(self):
        for attr in ("id3", "vorbis", "mp4"):
            used = [getattr(f, attr) for f in tag_editor.TAG_FIELDS if getattr(f, attr)]
            self.assertEqual(len(used), len(set(used)), f"{attr} 键重复：{used}")

    def test_every_field_has_at_least_one_mapping(self):
        for field in tag_editor.TAG_FIELDS:
            self.assertTrue(field.id3 or field.vorbis or field.mp4, field.key)

    def test_covers_requested_fields(self):
        labels = {f.label for f in tag_editor.TAG_FIELDS}
        requested = (
            "标题", "艺人", "专辑", "注释", "日期", "音轨编号", "流派", "专辑艺人",
            "曲作者", "作者", "每分钟节拍数", "分类号码", "曲集", "指挥", "版权",
            "唱片编号", "编码方式", "编码设置", "编码时间", "组群中", "初始调性",
            "ISRC", "语言", "词作者", "歌词", "媒体", "氛围",
        )
        for name in requested:
            self.assertIn(name, labels)

    def test_only_lyrics_is_multiline(self):
        multiline = {f.label for f in tag_editor.TAG_FIELDS if f.multiline}
        self.assertEqual(multiline, {"歌词"})


if __name__ == "__main__":
    unittest.main()
