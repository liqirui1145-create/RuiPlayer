from config import DEFAULT_METADATA

try:
    from mutagen import File as MutagenFile
except ImportError:
    MutagenFile = None

# 各音频格式的标题/艺术家/专辑标签键（按优先级排列）
TAG_KEY_MAP = {
    "title": ("TIT2", "title", "TITLE", "\xa9nam"),
    "artist": ("TPE1", "artist", "ARTIST", "\xa9ART"),
    "album": ("TALB", "album", "ALBUM", "\xa9alb"),
}


def get_tag_text(tags, field):
    """从音频标签对象中读取首个文本值，兼容 MP3(ID3)/FLAC・OGG(Vorbis)/M4A(MP4) 等格式。
    Vorbis 容器对不存在的键会抛异常，因此统一用成员判断 + try/except 兜底。"""
    if not tags:
        return None
    for key in TAG_KEY_MAP.get(field, ()):
        try:
            if key not in tags:
                continue
            value = tags[key]
        except Exception:
            continue
        if hasattr(value, "text") and value.text:
            return str(value.text[0])
        if isinstance(value, (list, tuple)) and len(value) > 0:
            return str(value[0])
        if isinstance(value, str) and value.strip():
            return str(value)
    return None


class MetadataReader:
    @staticmethod
    def read(file_path: str, is_video: bool = False) -> dict:
        metadata = DEFAULT_METADATA.copy()
        if not MutagenFile or is_video:
            return metadata

        try:
            audio = MutagenFile(file_path)
            if not audio:
                return metadata

            if hasattr(audio.info, 'sample_rate'):
                metadata["sample_rate"] = f"{audio.info.sample_rate} Hz"
            if hasattr(audio.info, 'channels'):
                metadata["channels"] = f"{audio.info.channels} 声道"
            if hasattr(audio.info, 'bitrate'):
                metadata["bitrate"] = f"{audio.info.bitrate // 1000} kbps"

            tags = audio.tags
            if tags:
                metadata["title"] = get_tag_text(tags, "title") or "--"
                metadata["artist"] = get_tag_text(tags, "artist") or "--"
                metadata["album"] = get_tag_text(tags, "album") or "--"

        except Exception:
            pass

        return metadata

    @staticmethod
    def extract_cover(file_path: str):
        if not MutagenFile:
            return None

        try:
            from mutagen.mp3 import MP3
            from mutagen.flac import FLAC
            from io import BytesIO
            from PIL import Image

            audio = MutagenFile(file_path)
            cover_data = None

            if isinstance(audio, MP3):
                for tag in audio.tags.values():
                    if tag.FrameID == "APIC":
                        cover_data = tag.data
                        break
            elif isinstance(audio, FLAC):
                for pic in audio.pictures:
                    cover_data = pic.data

            if cover_data:
                img = Image.open(BytesIO(cover_data)).convert("RGBA")
                return img
        except Exception:
            pass

        return None