#!/usr/bin/env python3
"""纯OAuth风格的Telegram认证模块 - 使用内置urllib"""

import os
import json
import hashlib
import webbrowser
import time
from urllib.parse import urlencode, urlparse, parse_qs
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread
from datetime import datetime, timedelta

class TelegramOAuth:
    """Telegram OAuth认证管理器"""
    
    def __init__(self):
        self.client_id = int(os.getenv('API_ID', '0'))
        self.client_secret = os.getenv('API_HASH', '')
        self.redirect_uri = 'http://localhost:8080/callback'
        self.scope = 'auth'
        self.access_token = None
        self.user_info = None
        self.auth_file = 'telegram_oauth.json'
        
    def get_authorization_url(self):
        """生成授权URL"""
        params = {
            'client_id': self.client_id,
            'redirect_uri': self.redirect_uri,
            'scope': self.scope,
            'response_type': 'code',
            'state': hashlib.sha256(os.urandom(32)).hexdigest()
        }
        return f"https://oauth.telegram.org/authorize?{urlencode(params)}"
    
    def start_local_server(self, callback):
        """启动本地HTTP服务器接收回调"""
        class OAuthHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urlparse(self.path)
                if parsed.path == '/callback':
                    query = parse_qs(parsed.query)
                    code = query.get('code', [None])[0]
                    error = query.get('error', [None])[0]
                    
                    if code:
                        self.send_response(200)
                        self.send_header('Content-type', 'text/html; charset=utf-8')
                        self.end_headers()
                        self.wfile.write(b'<html><body><h1>Authentication Successful!</h1><p>You can close this window now.</p></body></html>')
                        self.server.oauth_callback(code)
                    elif error:
                        self.send_response(400)
                        self.send_header('Content-type', 'text/html; charset=utf-8')
                        self.end_headers()
                        error_html = f'<html><body><h1>Authentication Failed: {error}</h1></body></html>'.encode('utf-8')
                        self.wfile.write(error_html)
                        self.server.oauth_callback(None, error)
                else:
                    self.send_response(404)
            
            def log_message(self, format, *args):
                pass
        
        server = HTTPServer(('localhost', 8080), OAuthHandler)
        server.oauth_callback = callback
        return server
    
    def _http_post(self, url, data):
        """发送POST请求"""
        try:
            encoded_data = urlencode(data).encode('utf-8')
            request = Request(url, data=encoded_data, method='POST')
            with urlopen(request) as response:
                return response.read().decode('utf-8')
        except HTTPError as e:
            print(f"HTTP错误: {e.code} - {e.read().decode('utf-8')}")
            return None
        except URLError as e:
            print(f"URL错误: {e}")
            return None
        except Exception as e:
            print(f"请求失败: {e}")
            return None
    
    def _http_get(self, url, headers=None):
        """发送GET请求"""
        try:
            request = Request(url, headers=headers or {})
            with urlopen(request) as response:
                return response.read().decode('utf-8')
        except HTTPError as e:
            print(f"HTTP错误: {e.code} - {e.read().decode('utf-8')}")
            return None
        except URLError as e:
            print(f"URL错误: {e}")
            return None
        except Exception as e:
            print(f"请求失败: {e}")
            return None
    
    def exchange_code_for_token(self, code):
        """使用授权码交换访问令牌"""
        url = 'https://oauth.telegram.org/token'
        data = {
            'grant_type': 'authorization_code',
            'code': code,
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'redirect_uri': self.redirect_uri
        }
        
        response_text = self._http_post(url, data)
        if response_text:
            try:
                return json.loads(response_text)
            except json.JSONDecodeError:
                print("响应不是有效的JSON")
                return None
        return None
    
    def get_user_info(self, access_token):
        """获取用户信息"""
        url = 'https://oauth.telegram.org/userinfo'
        headers = {'Authorization': f'Bearer {access_token}'}
        
        response_text = self._http_get(url, headers)
        if response_text:
            try:
                return json.loads(response_text)
            except json.JSONDecodeError:
                print("响应不是有效的JSON")
                return None
        return None
    
    def login(self):
        """执行完整的OAuth登录流程"""
        print("🔐 Telegram OAuth Login")
        print("=" * 50)
        
        # 1. 检查已有会话
        if self._load_session():
            print("✅ Valid session detected, auto login")
            return True
        
        # 2. 生成授权URL
        auth_url = self.get_authorization_url()
        print(f"\n🌐 Authorization URL: {auth_url}")
        
        # 3. 启动本地服务器
        result = {'code': None, 'error': None}
        
        def callback(code, error=None):
            result['code'] = code
            result['error'] = error
        
        server = self.start_local_server(callback)
        server_thread = Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        
        # 4. 打开浏览器
        print("\n🚀 Opening browser for authorization...")
        webbrowser.open(auth_url)
        
        # 5. 等待回调
        print("⏳ Waiting for user authorization...")
        while result['code'] is None and result['error'] is None:
            time.sleep(1)
        
        # 6. 关闭服务器
        server.shutdown()
        
        if result['error']:
            print(f"❌ Authorization failed: {result['error']}")
            return False
        
        # 7. 交换令牌
        print("\n🔄 Exchanging code for token...")
        token_data = self.exchange_code_for_token(result['code'])
        if not token_data or 'access_token' not in token_data:
            print("❌ Token exchange failed")
            return False
        
        self.access_token = token_data['access_token']
        print(f"✅ Token obtained: {self.access_token[:20]}...")
        
        # 8. 获取用户信息
        print("\n👤 Fetching user info...")
        self.user_info = self.get_user_info(self.access_token)
        if not self.user_info:
            print("❌ Failed to get user info")
            return False
        
        print(f"✅ User info obtained: {self.user_info.get('first_name', 'Unknown')}")
        
        # 9. 保存会话
        self._save_session()
        
        return True
    
    def _save_session(self):
        """保存会话到本地"""
        session = {
            'access_token': self.access_token,
            'user_info': self.user_info,
            'created_at': datetime.now().isoformat(),
            'expires_at': (datetime.now() + timedelta(days=30)).isoformat()
        }
        
        with open(self.auth_file, 'w', encoding='utf-8') as f:
            json.dump(session, f, indent=2)
        
        print(f"💾 Session saved to {self.auth_file}")
    
    def _load_session(self):
        """加载本地会话"""
        if not os.path.exists(self.auth_file):
            return False
        
        try:
            with open(self.auth_file, 'r', encoding='utf-8') as f:
                session = json.load(f)
            
            expires_at = datetime.fromisoformat(session['expires_at'])
            if datetime.now() > expires_at:
                print("⚠️  Session expired")
                return False
            
            self.access_token = session['access_token']
            self.user_info = session['user_info']
            return True
        except Exception as e:
            print(f"Failed to load session: {e}")
            return False
    
    def logout(self):
        """登出并清除会话"""
        if os.path.exists(self.auth_file):
            os.remove(self.auth_file)
            print("🗑️  Session cleared")
        
        self.access_token = None
        self.user_info = None
    
    def is_logged_in(self):
        """检查是否已登录"""
        return self.access_token is not None

if __name__ == "__main__":
    oauth = TelegramOAuth()
    if oauth.login():
        print("\n🎉 OAuth Login Successful!")
        print(f"User: {oauth.user_info.get('first_name')} {oauth.user_info.get('last_name', '')}")
        print(f"User ID: {oauth.user_info.get('id')}")
        print(f"Username: @{oauth.user_info.get('username', '')}")
    else:
        print("\n❌ OAuth Login Failed")