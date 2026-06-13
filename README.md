AI with liqirui1145-create开发，由本人输入提示词，doubao模型
默认封面：https://commons.wikimedia.org/wiki/File:Xinjiang_Old_and_young_(Populus_diversifolia_%E8%83%A1%E6%9D%A8)_(4973519309).jpg
# 多媒体播放器 (Media Player)

基于 PyQt6 + VLC 的多媒体播放器，支持音频/视频播放、LRC歌词同步、封面显示以及 Telegram 媒体下载等功能。

## 功能特性

- 🎵 支持多种音视频格式（MP3、FLAC、MP4、MKV等）
- 🎤 音频元数据读取（艺术家、专辑、采样率等）
- 📖 LRC歌词自动加载与同步显示
- 📷 专辑封面显示（支持内嵌封面和自定义默认封面）
- ⚡ 倍速播放（0.5x - 2.0x）
- ⌨️ 丰富的键盘快捷键
- 📡 网络串流播放（HTTP、RTSP等）
- 📱 **Telegram媒体下载** - 登录Telegram后可下载聊天中的音视频文件并本地播放

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

## Telegram 功能使用说明

### 1. 获取 Telegram API 凭证

1. 访问 [my.telegram.org](https://my.telegram.org/)
2. 使用你的Telegram账号登录
3. 创建新应用（Create new application）
4. 记录下 `api_id` 和 `api_hash`

### 2. 配置 .env 文件

编辑 `.env` 文件，填入你的 API 凭证：

```env
# Telegram API配置
API_ID=你的api_id
API_HASH=你的api_hash
SESSION_NAME=telegram_player_session
DOWNLOAD_DIR=./downloads
```

### 3. 使用步骤

1. 在播放器右侧面板的 Telegram 区域输入手机号（如 +8612345678900）
2. 点击「登录」按钮，首次登录需要输入验证码
3. 登录成功后会自动加载你聊天中的媒体文件列表
4. 选择想要下载的媒体文件，点击「下载并播放」
5. 下载完成后会自动开始播放

### 注意事项

- 首次登录需要验证码，请确保你的Telegram账号可以正常接收验证码
- 下载的文件保存在 `./downloads` 目录下
- 仅支持下载聊天中的音视频文件