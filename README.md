
[![License: GPLv3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)
[![Image License: CC BY 2.0](https://img.shields.io/badge/Image_License-CC_BY_2.0-lightgrey.svg)](COPYRIGHT.md)
[![依赖协议](https://img.shields.io/badge/Dependencies-LGPL%2FBSD%2FHPND-green.svg)](COPYRIGHT.md)

# RuiPlayer多媒体播放器 (RuiPlayer)

基于 PyQt6 + VLC 的多媒体播放器，支持音频/视频播放、LRC歌词同步、封面显示等功能。

## 功能特性

- 🎵 支持多种音视频格式（MP3、FLAC、MP4、MKV等）
- 🎤 音频元数据读取（艺术家、专辑、采样率等）
- 📖 LRC歌词自动加载与同步显示
- 📷 专辑封面显示（支持内嵌封面和自定义默认封面）
- ⚡ 倍速播放（0.5x - 2.0x）
- 🎚️ 10段均衡器（31Hz ~ 16kHz，18种中文预设 + 手动调节，独立弹窗操作）
- 🗂️ 音乐库扫描（右侧“音乐库”标签页：指定文件夹递归扫描，按 SHA256 校验内容去重，双击播放）
- ✏️ 标签编辑（右侧“标签”标签页：修改正在播放音乐的 27 项元数据并写回文件，支持 ID3/FLAC/OGG/M4A）
- 📐 屏幕分辨率自适应（窗口、封面、字号随分辨率缩放，左右区域保持 7:3 ~ 8:2）
- 🔍 界面缩放设置（50%~200%，高清屏防文字小且模糊，独立弹窗调节并自动保存）
- ⌨️ 丰富的键盘快捷键
- 📡 网络串流播放（HTTP、RTSP等）
- 📺 M3U/M3U8 电视台列表（支持上传本地文件或链接自动识别，列出频道点台播放，支持搜索过滤）
- 🎛️ 系统媒体控制（MPRIS：桌面媒体控件/锁屏/蓝牙耳机按键/playerctl，另有系统托盘与键盘媒体键）

## 安装依赖

```bash
pip install pyqt6 python-vlc mutagen pillow pywin32 python-dotenv
```

> Linux 下的系统媒体控制（MPRIS）需要 PyGObject：`sudo apt install python3-gi`。
> 大多数桌面发行版已预装；缺少时程序照常运行，只是桌面媒体控件无法控制它。

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

## 音乐库（右侧“音乐库”标签页）

1. 点击“添加文件夹”选择要扫描的音乐目录（可添加多个，扫描时全部合并处理）
2. 点击“扫描”：程序在后台线程递归收集音频文件，逐个计算 SHA256，
   **内容完全相同的文件只保留先扫描到的一首**，其余计入“跳过项”
3. 双击列表项播放，右键可“播放 / 打开所在文件夹 / 从列表移除”
4. 搜索框按标题、艺术家或完整路径过滤；“移除选中 / 清空列表”只影响列表，不删磁盘文件
5. 扫描结果与 SHA256 索引保存在 `ui_settings.json`，下次启动自动恢复；
   未修改（大小与修改时间不变）的文件在再次扫描时直接复用历史摘要，无需重读磁盘
6. 短音频过滤：“短音频”下拉框（默认 30 秒，可选不过滤/5/10/15/30/60 秒）——时长短于该值的
   文件不入库，用来挡掉系统音效、通知音、铃声等；**读不到时长的文件一律保留**，避免误杀
7. 底部“跳过项”按钮可查看上次扫描被跳过的文件明细（重复 + 短音频）

> - 过滤与去重只决定“是否加入列表”，**不会删除或移动磁盘上的任何文件**。
> - 取消“SHA256去重”勾选后仅按文件路径去重，扫描更快，但内容相同的副本都会出现在列表里。
> - 30 秒是比较激进的阈值：正规音乐里的 interlude、demo、儿歌、播客片头都可能被挡掉，
>   如果发现漏歌，把它调到 10~15 秒或“不过滤”重新扫描即可。

## 标签编辑（右侧“标签”标签页）

只对**正在播放的音乐文件**生效（网络串流、视频文件会自动置为不可编辑）：

1. 播放一首音乐后切到“标签”标签页，27 个字段会自动填入当前标签值
2. 修改后点“保存标签”直接写回文件（覆盖原标签，**不可撤销**）；留空表示删除该标签项
3. “重新读取”可丢弃未保存的改动；切换播放文件时未保存的改动会被丢弃并在状态栏提示
4. 保存后音乐库列表的显示文本、媒体信息面板会同步刷新

可编辑字段：标题、艺人、专辑、专辑艺人、曲作者、词作者、作者、指挥、流派、日期、
音轨编号、唱片编号、注释、曲集、组群中、每分钟节拍数、分类号码、版权、编码方式、
编码设置、编码时间、初始调性、ISRC、语言、媒体、氛围、歌词（多行）

> 容器支持：ID3v2（MP3 / WAV / AIFF）、VorbisComment（FLAC / OGG / Opus）、MP4（M4A）。
> WMA、APE 等暂不支持写入，界面会直接提示。

## 系统媒体控制（Linux）

程序启动后会以 `org.mpris.MediaPlayer2.ruiplayer` 注册到 D-Bus，因此可以使用：

- **桌面媒体控件**：KDE/GNOME 顶栏的播放条、锁屏界面、通知中心的媒体卡片
- **耳机/键盘按键**：蓝牙耳机与键盘上的播放/暂停、上一曲、下一曲（由桌面转发给 MPRIS 播放器）
- **命令行控制**：
  ```bash
  gdbus call --session --dest org.mpris.MediaPlayer2.ruiplayer \
      --object-path /org/mpris/MediaPlayer2 \
      --method org.mpris.MediaPlayer2.Player.PlayPause
  # 装了 playerctl 的话：playerctl -p ruiplayer play-pause
  ```
- **系统托盘图标**：右键菜单可播放/暂停、上一曲、下一曲、停止、显示主窗口、退出；双击图标显示主窗口
- **回传给桌面的信息**：标题、艺术家、专辑、时长、封面（内嵌封面会写入临时缓存供桌面读取）

关于上一曲/下一曲：**按右侧“音乐库”标签页的列表顺序**切换（跟随当前的搜索过滤结果），
到头会循环。列表为空时会提示先去扫描音乐文件夹。

> 媒体键有两条路径（桌面转发给 MPRIS、应用内快捷键），程序对应用内快捷键做了 0.3 秒去重，
> 避免一次按键把“播放/暂停”翻转两次。

## 测试

```bash
python -m unittest discover -s tests
```
# 项目版权

[![License: GPLv3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)
[![Image License: CC BY 2.0](https://img.shields.io/badge/Image_License-CC_BY_2.0-lightgrey.svg)](COPYRIGHT.md)
[![依赖协议](https://img.shields.io/badge/Dependencies-LGPL%2FBSD%2FHPND-green.svg)](COPYRIGHT.md)
