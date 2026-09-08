import unittest

import metadata_reader
import p2


class _ID3Frame:
    """模拟 mutagen ID3 文本帧：含 .text 列表"""

    def __init__(self, text):
        self.text = list(text)


class _Tags:
    """模拟可下标访问、缺失键不抛异常（dict 型）的标签容器"""

    def __init__(self, data):
        self._data = data

    def __contains__(self, key):
        return key in self._data

    def __getitem__(self, key):
        return self._data[key]


class _StrictTags(_Tags):
    """模拟 VorbisComment：对缺失键抛异常"""

    def __getitem__(self, key):
        if key not in self._data:
            raise KeyError(key)
        return self._data[key]


class TagTextTests(unittest.TestCase):
    def test_id3_mp3_frames(self):
        # MP3：键为 TIT2/TPE1/TALB，值为含 .text 的帧对象
        tags = _Tags({"TIT2": _ID3Frame(["标题"]),
                      "TPE1": _ID3Frame(["艺人"]),
                      "TALB": _ID3Frame(["专辑"])})
        self.assertEqual(p2.get_tag_text(tags, "title"), "标题")
        self.assertEqual(p2.get_tag_text(tags, "artist"), "艺人")
        self.assertEqual(p2.get_tag_text(tags, "album"), "专辑")

    def test_vorbis_comment(self):
        # FLAC/OGG：小写键、列表值，缺失键抛异常
        tags = _StrictTags({"title": ["V标题"], "artist": ["V艺人"]})
        self.assertEqual(p2.get_tag_text(tags, "title"), "V标题")
        self.assertEqual(p2.get_tag_text(tags, "artist"), "V艺人")
        # 缺失键不应抛异常
        self.assertIsNone(p2.get_tag_text(tags, "album"))

    def test_mp4_keys(self):
        # M4A/MP4：©nam/©ART/©alb
        tags = _Tags({"\xa9nam": ["M标题"], "\xa9ART": ["M艺人"], "\xa9alb": ["M专辑"]})
        self.assertEqual(p2.get_tag_text(tags, "title"), "M标题")
        self.assertEqual(p2.get_tag_text(tags, "artist"), "M艺人")
        self.assertEqual(p2.get_tag_text(tags, "album"), "M专辑")

    def test_missing_or_empty(self):
        self.assertIsNone(p2.get_tag_text(None, "title"))
        self.assertIsNone(p2.get_tag_text(_Tags({}), "artist"))

    def test_metadata_reader_helper_aligned(self):
        # metadata_reader.py（供 main.py 使用）与 p2.py 行为一致
        tags = _Tags({"TIT2": _ID3Frame(["标题"]),
                      "TPE1": _ID3Frame(["艺人"]),
                      "TALB": _ID3Frame(["专辑"])})
        self.assertEqual(metadata_reader.get_tag_text(tags, "title"), "标题")
        self.assertEqual(metadata_reader.get_tag_text(tags, "artist"), "艺人")
        self.assertEqual(metadata_reader.get_tag_text(tags, "album"), "专辑")


if __name__ == "__main__":
    unittest.main()
