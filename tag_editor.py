# -*- coding: utf-8 -*-
"""音频标签读写（供右侧“标签编辑”标签页使用）。

覆盖三类容器风格：
- ID3 系：MP3 / WAV / AIFF —— ID3v2 帧（TIT2/TPE1/…，自定义帧走 TXXX:描述）
- Vorbis 系：FLAC / OGG / Opus —— VorbisComment 键值
- MP4 系：M4A / MP4 —— MP4 原子（©nam、trkn 等）

其它容器（WMA、APE 等）暂不支持写入，界面会提示只读。

本模块不依赖播放器，可独立自检：
    python tag_editor.py 文件.mp3
"""

import sys
from dataclasses import dataclass

try:
    import mutagen.id3 as id3
    from mutagen import File as MutagenFile
except Exception:  # 缺少 mutagen 时程序仍可运行，只是不能编辑标签
    id3 = None
    MutagenFile = None

try:
    from mutagen.aiff import AIFF
    from mutagen.flac import FLAC
    from mutagen.mp3 import MP3
    from mutagen.mp4 import MP4
    from mutagen.oggopus import OggOpus
    from mutagen.oggvorbis import OggVorbis
    from mutagen.wave import WAVE
except Exception:
    AIFF = FLAC = MP3 = MP4 = OggOpus = OggVorbis = WAVE = None

# VorbisComment 风格的容器（FLAC / OGG / Opus）
VORBIS_TYPES = tuple(t for t in (FLAC, OggVorbis, OggOpus) if t)
# ID3 风格的容器（MP3 / WAV / AIFF）——注意它们没有标签时 tags 为 None，
# 因此必须按音频对象类型判断，不能只看 tags 的类型
ID3_TYPES = tuple(t for t in (MP3, WAVE, AIFF) if t)

CONTAINER_LABELS = {
    "id3": "ID3v2",
    "vorbis": "VorbisComment",
    "mp4": "MP4",
    "unknown": "不支持",
}


@dataclass(frozen=True)
class TagField:
    """一个可编辑标签项：中文名 + 各容器对应的键"""

    key: str                       # 内部标识，界面用它取值
    label: str                     # 中文显示名
    id3: str = ""                  # ID3v2 帧（"TXXX:描述" 表示自定义帧）
    vorbis: str = ""               # VorbisComment 键
    mp4: str = ""                  # MP4 原子名
    multiline: bool = False        # 是否用多行编辑器（歌词）


# 可编辑字段（按常用程度排序，与主流标签编辑器一致）
TAG_FIELDS = (
    TagField("title", "标题", "TIT2", "title", "\xa9nam"),
    TagField("artist", "艺人", "TPE1", "artist", "\xa9ART"),
    TagField("album", "专辑", "TALB", "album", "\xa9alb"),
    TagField("albumartist", "专辑艺人", "TPE2", "albumartist", "aART"),
    TagField("composer", "曲作者", "TCOM", "composer", "\xa9wrt"),
    TagField("lyricist", "词作者", "TEXT", "lyricist"),
    TagField("author", "作者", "TXXX:AUTHOR", "author"),
    TagField("conductor", "指挥", "TPE3", "conductor"),
    TagField("genre", "流派", "TCON", "genre", "\xa9gen"),
    TagField("date", "日期", "TDRC", "date", "\xa9day"),
    TagField("track", "音轨编号", "TRCK", "tracknumber", "trkn"),
    TagField("discnumber", "唱片编号", "TPOS", "discnumber", "disk"),
    TagField("comment", "注释", "COMM", "comment", "\xa9cmt"),
    # 「曲集」(Content Group) 与「组群中」(Grouping) 在 MP3 里是两个不同帧，
    # Vorbis 侧用 contentgroup / grouping 区分，避免互相覆盖
    TagField("grouping", "曲集", "TIT1", "contentgroup", "\xa9grp"),
    TagField("groupdesc", "组群中", "TXXX:GROUPING", "grouping"),
    TagField("bpm", "每分钟节拍数", "TBPM", "bpm", "tmpo"),
    TagField("catalognumber", "分类号码", "TXXX:CATALOGNUMBER", "catalognumber"),
    TagField("copyright", "版权", "TCOP", "copyright", "cprt"),
    TagField("encodedby", "编码方式", "TENC", "encodedby", "\xa9enc"),
    TagField("encodersettings", "编码设置", "TSSE", "encodersettings"),
    TagField("encodingtime", "编码时间", "TDEN", "encodingtime"),
    TagField("key", "初始调性", "TKEY", "initialkey"),
    TagField("isrc", "ISRC", "TSRC", "isrc"),
    TagField("language", "语言", "TLAN", "language"),
    TagField("media", "媒体", "TMED", "media"),
    TagField("mood", "氛围", "TMOO", "mood"),
    TagField("lyrics", "歌词", "USLT", "lyrics", "\xa9lyr", multiline=True),
)

FIELD_LABELS = {field.key: field.label for field in TAG_FIELDS}

# MP4 中需要用整数或「编号/总数」写入的键
_MP4_INT_KEYS = {"tmpo"}
_MP4_PAIR_KEYS = {"trkn", "disk"}


def _to_int(value, default=0):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def open_audio(path):
    """用 mutagen 打开音频文件；失败返回 None"""
    if MutagenFile is None:
        return None
    try:
        return MutagenFile(path)
    except Exception:
        return None


def container_kind(audio):
    """判断已打开文件的容器风格：id3 / vorbis / mp4 / unknown"""
    if audio is None:
        return "unknown"
    if MP4 is not None and isinstance(audio, MP4):
        return "mp4"
    if VORBIS_TYPES and isinstance(audio, VORBIS_TYPES):
        return "vorbis"
    if ID3_TYPES and isinstance(audio, ID3_TYPES):
        return "id3"
    # 兜底：其它带 ID3 标签的容器（如第三方扩展类型）
    if id3 is not None and isinstance(getattr(audio, "tags", None), id3.ID3):
        return "id3"
    return "unknown"


def probe_container(path):
    """探测文件可写标签的容器风格（不可写时返回 unknown）"""
    return container_kind(open_audio(path))


def read_tags(path):
    """读取全部可编辑字段，返回 {key: 文本}；缺失的字段为空字符串。

    读取失败的单项留空，不会中断整体读取。
    """
    values = {field.key: "" for field in TAG_FIELDS}
    audio = open_audio(path)
    if audio is None:
        return values
    kind = container_kind(audio)
    if kind == "unknown":
        return values
    tags = getattr(audio, "tags", None)
    if not tags:
        return values

    for field in TAG_FIELDS:
        try:
            if kind == "id3":
                values[field.key] = _read_id3(tags, field.id3)
            elif kind == "vorbis":
                values[field.key] = _read_vorbis(tags, field.vorbis)
            else:
                values[field.key] = _read_mp4(tags, field.mp4)
        except Exception:
            values[field.key] = ""
    return values


def _read_id3(tags, spec):
    """读取 ID3 帧文本；spec 形如 'TIT2' 或 'TXXX:DESC'

    注释(COMM)/歌词(USLT) 的帧键是 (ID, 描述, 语言) 三元组（如 COMM::XXX），
    直接用单键查不到，因此未命中时回退到 getall 取第一帧。
    """
    if not spec:
        return ""
    frame = tags.get(spec)
    if frame is None:
        frames = tags.getall(spec)
        frame = frames[0] if frames else None
    if frame is None:
        return ""
    text = getattr(frame, "text", None)
    if text is None:
        return ""
    if isinstance(text, (list, tuple)):
        return str(text[0]) if text else ""
    return str(text)


def _read_vorbis(tags, key):
    """读取 VorbisComment 键（值为列表，取第一项）"""
    if not key:
        return ""
    try:
        value = tags.get(key)
    except Exception:
        return ""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return str(value[0]) if value else ""
    return str(value)


def _read_mp4(tags, key):
    """读取 MP4 原子；trkn/disk 还原为 '编号/总数'"""
    if not key:
        return ""
    try:
        value = tags.get(key)
    except Exception:
        return ""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        if not value:
            return ""
        first = value[0]
        if isinstance(first, tuple):  # trkn/disk 形如 [(1, 12)]
            number = _to_int(first[0]) if first else 0
            total = _to_int(first[1]) if len(first) > 1 else 0
            return f"{number}/{total}" if total else (str(number) if number else "")
        return str(first)
    return str(value)


def save_tags(path, values):
    """把 {key: 文本} 写回文件。

    返回 (是否成功, 提示文本)；空字符串表示删除该标签项。
    """
    if MutagenFile is None or id3 is None:
        return False, "未安装 mutagen，无法写入标签"

    audio = open_audio(path)
    if audio is None:
        return False, "无法打开该文件（格式不受支持或文件已损坏）"

    kind = container_kind(audio)
    if kind == "unknown":
        return False, "该文件格式暂不支持写入标签（支持 MP3/FLAC/OGG/Opus/M4A/WAV/AIFF）"

    try:
        if kind == "id3":
            _write_id3(audio, values)
        elif kind == "vorbis":
            _write_vorbis(audio, values)
        else:
            _write_mp4(audio, values)
        audio.save()
    except Exception as exc:
        return False, f"写入失败：{exc}"
    return True, f"标签已写入文件（{CONTAINER_LABELS.get(kind, kind)}）"


def _build_id3_frame(spec, text):
    """按帧名构造 ID3 帧对象"""
    if spec.startswith("TXXX:"):
        return id3.TXXX(encoding=3, desc=spec.split(":", 1)[1], text=[text])
    cls = getattr(id3, spec, None)
    if cls is None:
        return None
    if cls is id3.COMM:
        return id3.COMM(encoding=3, lang="XXX", desc="", text=[text])
    if cls is id3.USLT:
        return id3.USLT(encoding=3, lang="XXX", desc="", text=text)
    return cls(encoding=3, text=[text])


def _write_id3(audio, values):
    tags = audio.tags
    if tags is None:
        audio.add_tags()
        tags = audio.tags
    for field in TAG_FIELDS:
        if not field.id3 or field.key not in values:
            continue
        text = (values[field.key] or "").strip()
        try:
            if not text:
                tags.delall(field.id3)
                continue
            frame = _build_id3_frame(field.id3, text)
            if frame is None:
                continue
            # 先清掉同类帧再写：COMM/USLT 的键带语言子键，add 不会覆盖旧的
            tags.delall(field.id3)
            tags.add(frame)
        except Exception:
            continue


def _write_vorbis(audio, values):
    for field in TAG_FIELDS:
        if not field.vorbis or field.key not in values:
            continue
        key = field.vorbis
        text = (values[field.key] or "").strip()
        try:
            if text:
                audio[key] = text
            elif key in audio:
                del audio[key]
        except Exception:
            continue


def _write_mp4(audio, values):
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags
    for field in TAG_FIELDS:
        if not field.mp4 or field.key not in values:
            continue
        key = field.mp4
        text = (values[field.key] or "").strip()
        try:
            if not text:
                if key in tags:
                    del tags[key]
                continue
            if key in _MP4_PAIR_KEYS:
                number, _, total = text.partition("/")
                tags[key] = [(_to_int(number), _to_int(total))]
            elif key in _MP4_INT_KEYS:
                tags[key] = [_to_int(text)]
            else:
                tags[key] = [text]
        except Exception:
            continue


def _main(argv):
    """命令行自检：python tag_editor.py 文件"""
    if len(argv) < 2:
        print("用法：python tag_editor.py <音频文件>")
        return
    path = argv[1]
    kind = probe_container(path)
    print(f"{path}\n容器：{CONTAINER_LABELS.get(kind, kind)}")
    values = read_tags(path)
    for field in TAG_FIELDS:
        print(f"  {field.label:<8} {values.get(field.key, '')}")


if __name__ == "__main__":
    _main(sys.argv)
