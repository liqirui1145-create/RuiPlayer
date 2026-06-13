#!/usr/bin/env python3
"""纯OAuth风格Telegram登录测试"""

import asyncio
from telegram_manager import TelegramManager

async def main():
    print("=" * 60)
    print("🔐 Telegram OAuth 登录测试")
    print("=" * 60)
    
    manager = TelegramManager()
    
    def on_login(is_success, message):
        print(f"\n登录状态: {'✅ 成功' if is_success else '❌ 失败'}")
        print(f"消息: {message}")
    
    manager.login_status_changed.connect(on_login)
    
    print("\n🚀 正在启动OAuth登录流程...")
    print("   将自动打开浏览器进行授权")
    await manager.connect()
    
    if manager.is_logged_in:
        print("\n🎉 登录成功！")
        print(f"\n👤 用户信息:")
        print(f"   ID: {manager.current_user.get('id')}")
        print(f"   姓名: {manager.current_user.get('first_name')} {manager.current_user.get('last_name', '')}")
        print(f"   用户名: @{manager.current_user.get('username', '')}")
        
        dialogs = await manager.get_dialogs()
        print(f"\n📋 对话列表 ({len(dialogs)}):")
        for d in dialogs:
            print(f"   - {d['name']} ({d['unread']}未读)")
        
        media = await manager.get_media_from_chats()
        print(f"\n🎵 媒体列表 ({len(media)}):")
        for m in media:
            print(f"   - {m['name']} ({manager.format_size(m['file_size'])})")

if __name__ == "__main__":
    asyncio.run(main())