# 多媒体播放器 (Media Player)

基于 PyQt6 + VLC 的多媒体播放器，支持音频/视频播放、LRC歌词同步、封面显示等功能。

## 功能特性

- 🎵 支持多种音视频格式（MP3、FLAC、MP4、MKV等）
- 🎤 音频元数据读取（艺术家、专辑、采样率等）
- 📖 LRC歌词自动加载与同步显示
- 📷 专辑封面显示（支持内嵌封面和自定义默认封面）
- ⚡ 倍速播放（0.5x - 2.0x）
- 🎚️ 10段均衡器（31Hz ~ 16kHz，18种中文预设 + 手动调节，独立弹窗操作）
- 📐 屏幕分辨率自适应（窗口、封面、字号随分辨率缩放，左右区域保持 7:3 ~ 8:2）
- 🔍 界面缩放设置（50%~200%，高清屏防文字小且模糊，独立弹窗调节并自动保存）
- ⌨️ 丰富的键盘快捷键
- 📡 网络串流播放（HTTP、RTSP等）
- 📺 M3U/M3U8 电视台列表（支持上传本地文件或链接自动识别，列出频道点台播放，支持搜索过滤）

## 安装依赖

```bash
pip install pyqt6 python-vlc mutagen pillow pywin32 telethon python-dotenv
```

## 运行

```bash
python p2.py
```

## 快捷键

| 按键 | 功能 |
|------|------|
| 空格 | 播放/暂停 |
| ← | 回退10秒 |
| → | 快进10秒 |
| ↑ | 音量+5 |
| ↓ | 音量-5 |

## 项目版权

- GNU GPL v3.0，见 `LICENSE` 文件，作者：[liqirui1145-create]也可以叫我李启睿
- 默认封面：[https://commons.wikimedia.org/wiki/File:Xinjiang_Old_and_young_(Populus_diversifolia_%E8%83%A1%E6%9D%A8)_(4973519309).jpg
](url) This file is licensed under the Creative Commons Attribution 2.0 Generic license.
- 部分代码工作以及MD的撰写由AI完成（豆包以及DEEPSEEK）
