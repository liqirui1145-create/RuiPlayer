#!/usr/bin/env python3
"""Telegram媒体管理器 - 用于登录、拉取媒体、下载等功能"""

import os
import asyncio
from telethon import TelegramClient, types
from telethon.tl.types import DocumentAttributeAudio, DocumentAttributeVideo
from dotenv import load_dotenv
from PyQt6.QtCore import QObject, pyqtSignal
from datetime import datetime

load_dotenv()

class TelegramManager(QObject):
    login_status_changed = pyqtSignal(bool, str)
    media_list_updated = pyqtSignal(list)
    download_progress = pyqtSignal(str, int)
    download_complete = pyqtSignal(str, str)
    download_failed = pyqtSignal(str, str)
    
    def __init__(self):
        super().__init__()
        self.api_id = int(os.getenv('API_ID', '0'))
        self.api_hash = os.getenv('API_HASH', '')
        self.session_name = os.getenv('SESSION_NAME', 'telegram_player_session')
        self.download_dir = os.getenv('DOWNLOAD_DIR', './downloads')
        
        os.makedirs(self.download_dir, exist_ok=True)
        
        self.client = None
        self.is_logged_in = False
        self.current_user = None
        self.media_cache = {}
    
    def _try_remove_file(self, filepath):
        """尝试删除文件，处理文件锁定情况"""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                if os.path.exists(filepath):
                    os.remove(filepath)
                    return True
            except PermissionError:
                if attempt < max_retries - 1:
                    import time
                    time.sleep(0.5)
                else:
                    print(f"警告：无法删除文件 {filepath}，可能被其他进程占用")
            except Exception as e:
                break
        return False
    
    async def connect(self, phone_number=None):
        """连接到Telegram并登录"""
        try:
            # 清理旧会话（安全方式）
            session_files = [f"{self.session_name}.session", f"{self.session_name}.session-journal"]
            for sf in session_files:
                self._try_remove_file(sf)
            
            self.client = TelegramClient(self.session_name, self.api_id, self.api_hash)
            
            if phone_number:
                await self.client.start(
                    phone=phone_number,
                    code_callback=self._code_callback
                )
            else:
                await self.client.start()
            
            if await self.client.is_user_authorized():
                me = await self.client.get_me()
                self.current_user = me
                self.is_logged_in = True
                self.login_status_changed.emit(True, f"已登录: {me.first_name}")
                print(f"✅ Telegram登录成功: {me.first_name}")
            else:
                self.login_status_changed.emit(False, "登录失败：未获得授权")
                
        except PermissionError as e:
            error_msg = str(e)
            print(f"❌ 文件访问错误: {error_msg}")
            self.login_status_changed.emit(False, "错误：会话文件被其他程序占用，请关闭其他Telegram客户端后重试")
        except Exception as e:
            error_msg = str(e)
            print(f"❌ Telegram连接错误: {error_msg}")
            
            if "PhoneNumberInvalid" in error_msg:
                self.login_status_changed.emit(False, "错误：手机号码格式无效，请使用+国家码格式")
            elif "ApiIdInvalid" in error_msg:
                self.login_status_changed.emit(False, "错误：API_ID无效，请检查配置")
            elif "ApiHashInvalid" in error_msg:
                self.login_status_changed.emit(False, "错误：API_HASH无效，请检查配置")
            elif "ConnectionError" in error_msg or "TimeoutError" in error_msg:
                self.login_status_changed.emit(False, "错误：网络连接失败，请检查网络")
            elif "FloodWait" in error_msg:
                self.login_status_changed.emit(False, "错误：请求过于频繁，请稍后重试")
            else:
                self.login_status_changed.emit(False, f"登录失败: {error_msg}")
    
    def _code_callback(self):
        code = input("请输入Telegram验证码: ").strip()
        return code
    
    def _password_callback(self):
        from getpass import getpass
        password = getpass("请输入两步验证密码: ")
        return password.strip()
    
    async def disconnect(self):
        if self.client:
            await self.client.disconnect()
            self.is_logged_in = False
            self.current_user = None
            self.login_status_changed.emit(False, "已登出")
    
    async def get_media_from_chats(self, limit=50):
        """从所有对话拉取媒体文件"""
        if not self.is_logged_in or not self.client:
            return []
        
        media_list = []
        try:
            async for dialog in self.client.iter_dialogs(limit=30):
                if isinstance(dialog.entity, types.User):
                    async for message in self.client.iter_messages(dialog.entity.id, limit=limit):
                        if message.media and hasattr(message.media, 'document'):
                            doc = message.media.document
                            if doc:
                                media_info = self._parse_document(doc, message, dialog)
                                if media_info:
                                    media_list.append(media_info)
            
            media_list = sorted(media_list, key=lambda x: x['date'], reverse=True)
            self.media_cache = {m['id']: m for m in media_list}
            self.media_list_updated.emit(media_list)
            
        except Exception as e:
            print(f"获取媒体列表失败: {e}")
        
        return media_list
    
    def _parse_document(self, doc, message, dialog):
        """解析媒体文件信息"""
        media_info = {
            'id': doc.id,
            'message_id': message.id,
            'chat_id': message.peer_id.user_id if hasattr(message.peer_id, 'user_id') else None,
            'date': message.date,
            'file_size': doc.size,
            'mime_type': doc.mime_type,
            'name': self._get_file_name(doc),
            'duration': 0,
            'is_audio': False,
            'is_video': False,
            'performer': '',
            'title': '',
            'sender': dialog.name if dialog else 'Unknown'
        }
        
        for attr in doc.attributes:
            if isinstance(attr, DocumentAttributeAudio):
                media_info['is_audio'] = True
                media_info['duration'] = attr.duration
                media_info['performer'] = attr.performer or ''
                media_info['title'] = attr.title or ''
            elif isinstance(attr, DocumentAttributeVideo):
                media_info['is_video'] = True
                media_info['duration'] = attr.duration
        
        if not media_info['is_audio'] and not media_info['is_video']:
            return None
        
        return media_info
    
    def _get_file_name(self, doc):
        for attr in doc.attributes:
            if isinstance(attr, types.DocumentAttributeFilename):
                return attr.file_name
        
        ext = self._get_extension(doc.mime_type)
        return f"media_{doc.id}{ext}"
    
    def _get_extension(self, mime_type):
        mapping = {
            'audio/mpeg': '.mp3',
            'audio/flac': '.flac',
            'audio/wav': '.wav',
            'audio/mp4': '.m4a',
            'video/mp4': '.mp4',
            'video/x-matroska': '.mkv',
            'video/quicktime': '.mov',
            'video/x-msvideo': '.avi'
        }
        return mapping.get(mime_type, '.dat')
    
    async def download_media(self, media_id):
        """下载媒体文件"""
        if not self.is_logged_in or not self.client:
            return None
        
        media_info = self.media_cache.get(media_id)
        if not media_info:
            self.download_failed.emit("未知文件", "媒体信息不存在")
            return None
        
        try:
            message = await self.client.get_messages(media_info['chat_id'], ids=media_info['message_id'])
            if not message or not message.media:
                self.download_failed.emit(media_info['name'], "消息不存在")
                return None
            
            file_name = media_info['name']
            download_path = os.path.join(self.download_dir, file_name)
            
            if os.path.exists(download_path):
                base, ext = os.path.splitext(file_name)
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                download_path = os.path.join(self.download_dir, f"{base}_{timestamp}{ext}")
            
            await self.client.download_media(
                message.media,
                file=download_path,
                progress_callback=self._download_progress_callback
            )
            
            self.download_complete.emit(file_name, download_path)
            return download_path
            
        except Exception as e:
            print(f"下载失败: {e}")
            self.download_failed.emit(media_info['name'], str(e))
            return None
    
    def _download_progress_callback(self, current, total):
        if total > 0:
            percentage = int((current / total) * 100)
            self.download_progress.emit("", percentage)
    
    async def get_dialogs(self):
        """获取对话列表"""
        if not self.is_logged_in or not self.client:
            return []
        
        dialogs = []
        try:
            async for dialog in self.client.iter_dialogs():
                if isinstance(dialog.entity, types.User):
                    dialogs.append({
                        'id': dialog.entity.id,
                        'name': dialog.name,
                        'unread': dialog.unread_count
                    })
        except Exception as e:
            print(f"获取对话列表失败: {e}")
        
        return dialogs
    
    @staticmethod
    def format_duration(seconds):
        if seconds <= 0:
            return "--:--"
        mins = seconds // 60
        secs = seconds % 60
        return f"{mins}:{secs:02d}"
    
    @staticmethod
    def format_size(bytes_size):
        if bytes_size <= 0:
            return "0 B"
        units = ['B', 'KB', 'MB', 'GB']
        unit_idx = 0
        size = bytes_size
        while size >= 1024 and unit_idx < len(units) - 1:
            size /= 1024
            unit_idx += 1
        return f"{size:.2f} {units[unit_idx]}"

class TelegramManagerSync:
    def __init__(self):
        self.manager = TelegramManager()
        self.loop = asyncio.new_event_loop()
    
    def connect(self, phone_number=None):
        return self.loop.run_until_complete(self.manager.connect(phone_number))
    
    def disconnect(self):
        return self.loop.run_until_complete(self.manager.disconnect())
    
    def get_media_from_chats(self, limit=50):
        return self.loop.run_until_complete(self.manager.get_media_from_chats(limit))
    
    def download_media(self, media_id):
        return self.loop.run_until_complete(self.manager.download_media(media_id))
    
    def get_dialogs(self):
        return self.loop.run_until_complete(self.manager.get_dialogs())

if __name__ == "__main__":
    async def main():
        manager = TelegramManager()
        
        phone = input("请输入手机号（格式：+8612345678900）：")
        await manager.connect(phone)
        
        if manager.is_logged_in:
            print(f"\n🎉 登录成功！用户: {manager.current_user.first_name}")
            
            dialogs = await manager.get_dialogs()
            print(f"\n📋 对话列表 ({len(dialogs)}):")
            for d in dialogs:
                print(f"   - {d['name']}")
            
            media = await manager.get_media_from_chats(limit=20)
            print(f"\n🎵 媒体文件 ({len(media)}):")
            for m in media:
                print(f"   [{m['sender']}] {m['name']} ({TelegramManager.format_size(m['file_size'])})")
            
            if media:
                print("\n📥 正在下载第一个媒体文件...")
                await manager.download_media(media[0]['id'])
    
    asyncio.run(main())