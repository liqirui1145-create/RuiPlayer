#!/bin/bash
# RuiPlayer deb 打包脚本
#
# 用法：
#   ./packaging/build_deb.sh [版本号]         # 默认 1.0.0
#
# 产物：dist/ruiplayer_<版本>_all.deb
#
# 说明：不依赖 debhelper，只用 dpkg-deb 手工构建 Debian 包结构，
#       因此在任何 Ubuntu/Debian 上都能直接跑（需要 dpkg-deb 与 python3-pil）。
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="${1:-1.0.0}"
PKG_NAME="ruiplayer"
ARCH="all"
MAINTAINER="liqirui1145-create <liqirui1145-create@users.noreply.github.com>"
HOMEPAGE="https://github.com/liqirui1145-create/RuiPlayer"
INSTALL_DIR="/usr/share/${PKG_NAME}"

OUT_DIR="${PROJECT_ROOT}/dist"
BUILD_DIR="$(mktemp -d)"
PKG_ROOT="${BUILD_DIR}/${PKG_NAME}_${VERSION}_${ARCH}"
trap 'rm -rf "${BUILD_DIR}"' EXIT

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }

# ---------------------------------------------------------------- 目录结构
say "创建包目录结构"
install -d "${PKG_ROOT}${INSTALL_DIR}"
install -d "${PKG_ROOT}/usr/bin"
install -d "${PKG_ROOT}/usr/share/applications"
install -d "${PKG_ROOT}/usr/share/icons/hicolor"
install -d "${PKG_ROOT}/usr/share/doc/${PKG_NAME}"
install -d "${PKG_ROOT}/DEBIAN"

# ---------------------------------------------------------------- 程序文件
say "复制程序文件"
PY_FILES=(
    p2.py
    app_paths.py
    config.py
    cover_manager.py
    lyric_parser.py
    metadata_reader.py
    music_scanner.py
    mpris_player.py
    tag_editor.py
    tg_music.py
)
for f in "${PY_FILES[@]}"; do
    if [ ! -f "${PROJECT_ROOT}/${f}" ]; then
        echo "缺少文件：${f}" >&2
        exit 1
    fi
    install -Dm644 "${PROJECT_ROOT}/${f}" "${PKG_ROOT}${INSTALL_DIR}/${f}"
done

# 资源文件（图标、默认封面等）
shopt -s nullglob
for res in "${PROJECT_ROOT}"/*.png "${PROJECT_ROOT}"/*.jpg "${PROJECT_ROOT}"/*.svg; do
    install -Dm644 "${res}" "${PKG_ROOT}${INSTALL_DIR}/$(basename "${res}")"
done
shopt -u nullglob

# 文档与许可证
for doc in README.md LICENSE COPYRIGHT.md; do
    [ -f "${PROJECT_ROOT}/${doc}" ] && install -Dm644 "${PROJECT_ROOT}/${doc}" \
        "${PKG_ROOT}/usr/share/doc/${PKG_NAME}/${doc}"
done

# ---------------------------------------------------------------- 启动器
say "生成启动器 /usr/bin/${PKG_NAME}"
cat > "${PKG_ROOT}/usr/bin/${PKG_NAME}" <<EOF
#!/bin/sh
# RuiPlayer 启动器：由 deb 包生成
exec python3 ${INSTALL_DIR}/p2.py "\$@"
EOF
chmod 0755 "${PKG_ROOT}/usr/bin/${PKG_NAME}"

# ---------------------------------------------------------------- 桌面入口
say "生成桌面入口与图标"
cat > "${PKG_ROOT}/usr/share/applications/${PKG_NAME}.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=RuiPlayer
Name[zh_CN]=RuiPlayer 多媒体播放器
GenericName=Media Player
GenericName[zh_CN]=多媒体播放器
Comment=PyQt6 + VLC based media player with lyrics, equalizer, music library and tag editor
Comment[zh_CN]=基于 PyQt6 + VLC 的多媒体播放器，支持歌词同步、均衡器、音乐库与标签编辑
Exec=${PKG_NAME} %F
Icon=${PKG_NAME}
Terminal=false
Categories=AudioVideo;Audio;Video;Player;
MimeType=audio/mpeg;audio/flac;audio/x-wav;audio/mp4;audio/ogg;audio/x-vorbis+ogg;video/mp4;video/x-matroska;video/x-msvideo;application/vnd.apple.mpegurl;audio/x-mpegurl;
Keywords=player;audio;video;music;ruiplayer;
Keywords[zh_CN]=播放器;音乐;视频;歌词;均衡器;
StartupNotify=true
EOF
chmod 0644 "${PKG_ROOT}/usr/share/applications/${PKG_NAME}.desktop"

# 图标：把项目图标缩放出多个尺寸（没有 PIL 时只装原始尺寸）
python3 - "${PROJECT_ROOT}/qrstudio-icon.png" "${PKG_ROOT}/usr/share/icons/hicolor" <<'PY'
import os
import sys

try:
    from PIL import Image
except Exception:
    Image = None

source, target_root = sys.argv[1], sys.argv[2]
sizes = (16, 24, 32, 48, 64, 128, 256)

if Image is None or not os.path.exists(source):
    # 没有 PIL：至少保证有一个可用图标
    if os.path.exists(source):
        dest_dir = os.path.join(target_root, "256x256", "apps")
        os.makedirs(dest_dir, exist_ok=True)
        with open(source, "rb") as src, open(os.path.join(dest_dir, "ruiplayer.png"), "wb") as dst:
            dst.write(src.read())
    sys.exit(0)

image = Image.open(source).convert("RGBA")
for size in sizes:
    dest_dir = os.path.join(target_root, f"{size}x{size}", "apps")
    os.makedirs(dest_dir, exist_ok=True)
    image.resize((size, size), Image.Resampling.LANCZOS).save(
        os.path.join(dest_dir, "ruiplayer.png"), "PNG"
    )
print(f"    已生成图标尺寸：{', '.join(str(s) for s in sizes)}")
PY

# ---------------------------------------------------------------- 文档
say "生成 changelog 与版权文件"
cat > "${PKG_ROOT}/usr/share/doc/${PKG_NAME}/changelog" <<EOF
${PKG_NAME} (${VERSION}) unstable; urgency=medium

  * 音乐库：指定文件夹递归扫描，按 SHA256 校验内容去重，支持短音频过滤
  * 标签编辑：修改正在播放音乐的 27 项元数据并写回文件
  * 系统媒体控制：MPRIS（桌面媒体控件/锁屏/耳机按键/playerctl）、
    系统托盘图标、键盘媒体键
  * 全屏播放控制栏、界面缩放、均衡器与网络串流等既有功能

 -- ${MAINTAINER}  $(date -R)
EOF
gzip -9n "${PKG_ROOT}/usr/share/doc/${PKG_NAME}/changelog"

cat > "${PKG_ROOT}/usr/share/doc/${PKG_NAME}/copyright" <<EOF
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: RuiPlayer
Source: ${HOMEPAGE}

Files: *
Copyright: $(date +%Y) liqirui1145-create
License: GPL-3.0+
 This program is free software: you can redistribute it and/or modify it under
 the terms of the GNU General Public License as published by the Free Software
 Foundation, either version 3 of the License, or (at your option) any later
 version.
 .
 On Debian systems the full text of the GNU General Public License version 3
 can be found in /usr/share/common-licenses/GPL-3.
EOF
chmod 0644 "${PKG_ROOT}/usr/share/doc/${PKG_NAME}/copyright"

# ---------------------------------------------------------------- 控制文件
say "生成 DEBIAN/control"
INSTALLED_SIZE="$(du -sk "${PKG_ROOT}" | cut -f1)"
cat > "${PKG_ROOT}/DEBIAN/control" <<EOF
Package: ${PKG_NAME}
Version: ${VERSION}
Architecture: ${ARCH}
Maintainer: ${MAINTAINER}
Installed-Size: ${INSTALLED_SIZE}
Depends: python3 (>= 3.10), python3-pyqt6, python3-vlc, python3-mutagen, python3-pil, python3-gi, vlc
Recommends: python3-dotenv, python3-telethon, fonts-noto-cjk, playerctl
Section: sound
Priority: optional
Homepage: ${HOMEPAGE}
Description: RuiPlayer - 基于 PyQt6 + VLC 的多媒体播放器
 支持音频/视频播放、LRC 歌词同步与全屏歌词、内嵌封面显示、
 10 段均衡器、倍速播放、左右分栏的界面缩放自适应。
 .
 特色功能：
  * 音乐库：指定文件夹递归扫描，按 SHA256 校验内容去重，可过滤系统音效等短音频
  * 标签编辑：编辑正在播放音乐的标题/艺人/专辑/歌词等 27 项元数据并写回文件
  * 系统媒体控制：通过 MPRIS 接入桌面媒体控件、锁屏、蓝牙耳机按键与 playerctl
  * 网络串流与 M3U/M3U8 电视台列表、Telegram 音乐抓取
EOF
chmod 0644 "${PKG_ROOT}/DEBIAN/control"

# 维护脚本：刷新桌面数据库与图标缓存（失败不影响安装）
cat > "${PKG_ROOT}/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = "configure" ]; then
    command -v update-desktop-database >/dev/null 2>&1 && \
        update-desktop-database -q /usr/share/applications || true
    command -v gtk-update-icon-cache >/dev/null 2>&1 && \
        gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true
fi
exit 0
EOF
cat > "${PKG_ROOT}/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = "remove" ] || [ "$1" = "purge" ]; then
    command -v update-desktop-database >/dev/null 2>&1 && \
        update-desktop-database -q /usr/share/applications || true
    command -v gtk-update-icon-cache >/dev/null 2>&1 && \
        gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true
fi
exit 0
EOF
chmod 0755 "${PKG_ROOT}/DEBIAN/postinst" "${PKG_ROOT}/DEBIAN/postrm"

# md5sums（dpkg-deb 不会自动生成）
( cd "${PKG_ROOT}" && find . -type f ! -path './DEBIAN/*' -printf '%P\0' \
    | xargs -0 -r md5sum > DEBIAN/md5sums )
chmod 0644 "${PKG_ROOT}/DEBIAN/md5sums"

# ---------------------------------------------------------------- 打包
say "构建 deb（${PKG_NAME}_${VERSION}_${ARCH}.deb）"
mkdir -p "${OUT_DIR}"
dpkg-deb --root-owner-group --build "${PKG_ROOT}" \
    "${OUT_DIR}/${PKG_NAME}_${VERSION}_${ARCH}.deb" >/dev/null

DEB_FILE="${OUT_DIR}/${PKG_NAME}_${VERSION}_${ARCH}.deb"
say "完成：${DEB_FILE}"
ls -lh "${DEB_FILE}" | awk '{print "    大小：" $5}'
echo
echo "安装：  sudo apt install ${DEB_FILE}"
echo "卸载：  sudo apt remove ${PKG_NAME}"
echo "检查：  dpkg-deb -c ${DEB_FILE} | less"
