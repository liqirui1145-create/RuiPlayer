#!/usr/bin/env python3
"""测试Telegram模块是否能正常导入"""

try:
    from telegram_manager import TelegramManager, TelegramManagerSync
    print("✓ Telegram模块导入成功")
except Exception as e:
    print(f"✗ Telegram模块导入失败: {e}")
    exit(1)

try:
    import telethon
    print(f"✓ Telethon库版本: {telethon.__version__}")
except Exception as e:
    print(f"✗ Telethon库导入失败: {e}")

try:
    from dotenv import load_dotenv
    print("✓ python-dotenv库导入成功")
except Exception as e:
    print(f"✗ python-dotenv库导入失败: {e}")

print("\n所有依赖检查通过！")
print("\n请确保在.env文件中配置正确的Telegram API凭证：")
print("- API_ID: 从 my.telegram.org 获取")
print("- API_HASH: 从 my.telegram.org 获取")