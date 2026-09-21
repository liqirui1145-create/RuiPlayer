# -*- coding: utf-8 -*-
"""音乐库扫描与 SHA256 去重。

职责：
- 递归收集用户指定文件夹下的音频文件（扩展名取自 config.SUPPORTED_FORMATS）
- 以文件内容 SHA256 为唯一标识做去重：内容相同的文件只保留先遇到的那首
- 可选按时长过滤短音频（系统音效/提示音等），只影响是否入库，绝不动磁盘文件
- 扫描放在独立 QThread 中执行，避免大目录 + 全文件哈希计算卡住界面

本模块不依赖播放器实现，可独立运行自检：
    python music_scanner.py /path/to/music
"""

import hashlib
import os
import sys

from PyQt6.QtCore import QThread, pyqtSignal

try:
    from config import SUPPORTED_FORMATS

    AUDIO_EXTS = tuple(
        "." + ext.lower() for ext in SUPPORTED_FORMATS.get("audio", ())
    )
except Exception:  # 配置缺失时用内置兜底表
    AUDIO_EXTS = (
        ".mp3", ".flac", ".wav", ".m4a", ".aac", ".ogg", ".opus",
        ".wma", ".alac", ".aiff", ".ape",
    )

try:
    from mutagen import File as MutagenFile
except Exception:
    MutagenFile = None

# 摘要计算的分块大小：1 MiB，兼顾读取速度与内存占用
HASH_CHUNK = 1024 * 1024

# 各标签格式的键（按优先级），与 p2.py 的 get_tag_text 保持一致
_TAG_KEYS = {
    "title": ("TIT2", "title", "TITLE", "\xa9nam"),
    "artist": ("TPE1", "artist", "ARTIST", "\xa9ART"),
    "album": ("TALB", "album", "ALBUM", "\xa9alb"),
}


def _tag_text(tags, field):
    """兼容 ID3 帧 / VorbisComment / MP4 标签读取首个文本值"""
    if not tags:
        return None
    for key in _TAG_KEYS.get(field, ()):
        try:
            if key not in tags:
                continue
            value = tags[key]
        except Exception:
            continue
        if hasattr(value, "text") and value.text:
            return str(value.text[0]) or None
        if isinstance(value, (list, tuple)) and value:
            return str(value[0]) or None
        if isinstance(value, str) and value.strip():
            return value
    return None


def read_track_meta(path):
    """读取标题/艺术家/专辑/时长（秒）；任何失败都返回空字段，不中断扫描"""
    meta = {"title": None, "artist": None, "album": None, "duration": None}
    if MutagenFile is None:
        return meta
    try:
        audio = MutagenFile(path)
    except Exception:
        return meta
    if not audio:
        return meta
    length = getattr(getattr(audio, "info", None), "length", None)
    if isinstance(length, (int, float)) and length > 0:
        meta["duration"] = int(length)
    tags = getattr(audio, "tags", None)
    if tags:
        for field in ("title", "artist", "album"):
            meta[field] = _tag_text(tags, field)
    return meta


def format_duration(seconds):
    """秒数 → 'mm:ss' / 'h:mm:ss'；无效返回 None"""
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def format_size(num_bytes):
    """字节数 → 人类可读字符串"""
    try:
        size = float(num_bytes)
    except (TypeError, ValueError):
        return "--"
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.2f} {unit}"
        size /= 1024
    return "--"


def track_display_text(entry):
    """列表显示文本：'标题 - 艺术家  [时长]'，无标签时退回文件名"""
    path = entry.get("path") or ""
    title = (entry.get("title") or "").strip()
    artist = (entry.get("artist") or "").strip()
    stem = os.path.splitext(os.path.basename(path))[0]
    if title and artist and artist != title:
        text = f"{title} - {artist}"
    else:
        text = title or artist or stem
    duration = format_duration(entry.get("duration"))
    return f"{text}  [{duration}]" if duration else text


def track_tooltip(entry):
    """列表项悬浮提示：完整路径、大小、摘要"""
    lines = [entry.get("path") or ""]
    if entry.get("album"):
        lines.append(f"专辑：{entry['album']}")
    lines.append(f"大小：{format_size(entry.get('size'))}")
    sha = entry.get("sha256") or ""
    if sha:
        lines.append(f"SHA256：{sha[:16]}…")
    return "\n".join(lines)


def collect_audio_files(folders, exts=AUDIO_EXTS):
    """递归收集音频文件；传入文件本身也接受。

    返回绝对路径列表（同一路径只出现一次），排序保证结果稳定。
    """
    found = []
    seen = set()
    for folder in folders or ():
        if not folder:
            continue
        try:
            target = os.path.abspath(os.path.expanduser(str(folder)))
        except Exception:
            continue
        if os.path.isfile(target):
            if target.lower().endswith(exts) and target not in seen:
                seen.add(target)
                found.append(target)
            continue
        if not os.path.isdir(target):
            continue
        for root, dirs, files in os.walk(target):
            dirs.sort()  # 稳定的遍历顺序，便于复现问题
            for name in sorted(files):
                if not name.lower().endswith(exts):
                    continue
                path = os.path.join(root, name)
                if path in seen:
                    continue
                seen.add(path)
                found.append(path)
    return found


def file_sha256(path, chunk_size=HASH_CHUNK, cancel=None):
    """流式计算文件 SHA256 十六进制摘要。

    cancel 为无参可调用对象，返回 True 时提前放弃（此时函数返回 None），
    用于用户点击“停止”后尽快退出大文件的读取。
    """
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            if cancel is not None and cancel():
                return None
            block = f.read(chunk_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


class MusicScanWorker(QThread):
    """后台扫描线程：收集 → 计算 SHA256 → 内容去重 → 逐个上报。

    信号：
        file_found(dict)      —— 需要加入（或更新）库中的音乐条目
        duplicate_found(dict) —— 因内容重复被跳过的文件，含 original 来源路径
        short_found(dict)     —— 因时长过短被跳过的文件（系统音效等）
        progress(int, int)    —— (已处理, 总数)
        finished_scan(dict)   —— 汇总统计

    增量策略：路径 + 大小 + 修改时间都没变时直接复用历史 SHA256，
    避免每次扫描都把所有文件重读一遍。
    """

    file_found = pyqtSignal(dict)
    duplicate_found = pyqtSignal(dict)
    short_found = pyqtSignal(dict)
    progress = pyqtSignal(int, int)
    finished_scan = pyqtSignal(dict)

    def __init__(self, folders, known_entries=None, known_hashes=None,
                 dedup=True, read_meta=True, min_duration=0, parent=None):
        super().__init__(parent)
        self.folders = [f for f in (folders or []) if f]
        # path -> 历史条目（含 sha256/size/mtime/title/...）
        self.known_entries = dict(known_entries or {})
        # sha256 -> 库中首次出现该内容的路径
        self.known_hashes = dict(known_hashes or {})
        self.dedup = bool(dedup)
        self.read_meta = bool(read_meta)
        # 时长下限（秒）：大于 0 时，时长短于此值的文件不入库
        # （多为系统音效/提示音；读不到时长的文件一律保留，避免误杀）
        try:
            self.min_duration = max(0, int(min_duration or 0))
        except (TypeError, ValueError):
            self.min_duration = 0

    def run(self):  # noqa: C901 - 单线程流程，保持线性易读
        stats = {
            "total": 0, "added": 0, "existing": 0, "duplicate": 0,
            "too_short": 0, "failed": 0, "cancelled": False, "bytes": 0,
        }
        hash_owner = dict(self.known_hashes)

        try:
            files = collect_audio_files(self.folders)
        except Exception:
            files = []
        total = len(files)
        stats["total"] = total
        self.progress.emit(0, total)

        for index, path in enumerate(files, start=1):
            if self.isInterruptionRequested():
                stats["cancelled"] = True
                break

            try:
                st = os.stat(path)
                size, mtime = int(st.st_size), int(st.st_mtime)
            except OSError:
                stats["failed"] += 1
                self.progress.emit(index, total)
                continue

            known = self.known_entries.get(path)
            unchanged = bool(
                known
                and known.get("sha256")
                and known.get("size") == size
                and known.get("mtime") == mtime
            )

            if unchanged:
                digest = known["sha256"]
            else:
                try:
                    digest = file_sha256(path, cancel=self.isInterruptionRequested)
                except OSError:
                    digest = None
                if digest is None:
                    # 读取失败，或用户在计算途中点了停止
                    if self.isInterruptionRequested():
                        stats["cancelled"] = True
                        break
                    stats["failed"] += 1
                    self.progress.emit(index, total)
                    continue

            entry = {
                "path": path, "sha256": digest, "size": size, "mtime": mtime,
                "title": None, "artist": None, "album": None, "duration": None,
            }
            if unchanged:
                for field in ("title", "artist", "album", "duration"):
                    entry[field] = known.get(field)
            elif self.read_meta:
                entry.update(read_track_meta(path))

            # 时长过短（系统音效/提示音等）：不入库，也不占用去重索引，
            # 这样同内容的正常音频仍能以自己为第一份入库
            duration = entry.get("duration")
            if self.min_duration and duration and duration < self.min_duration:
                stats["too_short"] += 1
                self.short_found.emit({
                    "path": path, "sha256": digest, "size": size,
                    "duration": duration,
                })
                self.progress.emit(index, total)
                continue

            # 已入库的路径不参与判重（否则二次扫描会把自己当重复丢掉）
            is_known_path = path in self.known_entries
            dup_of = None if is_known_path else hash_owner.get(digest)
            if dup_of is not None and self.dedup:
                # 内容与库中已有的某首歌完全相同 → 重复，不占用列表
                stats["duplicate"] += 1
                self.duplicate_found.emit({
                    "path": path, "sha256": digest, "size": size,
                    "original": dup_of,
                })
                self.progress.emit(index, total)
                continue
            if digest not in hash_owner:
                hash_owner[digest] = path

            stats["bytes"] += size
            if is_known_path:
                stats["existing"] += 1
            else:
                stats["added"] += 1
            self.file_found.emit(entry)
            self.progress.emit(index, total)

        self.finished_scan.emit(stats)


def _main(argv):
    """命令行自检：python music_scanner.py 文件夹 [文件夹...]"""
    folders = argv[1:] or ["."]
    files = collect_audio_files(folders)
    print(f"扫描到 {len(files)} 个音频文件")
    seen = {}
    duplicates = 0
    for path in files:
        try:
            digest = file_sha256(path)
        except OSError as exc:
            print(f"  跳过（读取失败）：{path} ({exc})")
            continue
        if digest in seen:
            duplicates += 1
            print(f"  重复：{path}\n    与 {seen[digest]} 内容相同")
            continue
        seen[digest] = path
        print(f"  [{format_size(os.path.getsize(path))}] {path}")
    print(f"去重后 {len(seen)} 首，重复 {duplicates} 个")


if __name__ == "__main__":
    _main(sys.argv)
