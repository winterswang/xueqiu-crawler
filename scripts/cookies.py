#!/usr/bin/env python3
"""
雪球登录态管理器

功能:
- 检查 cookies 状态
- 通过 OpenClaw browser 登录雪球
- 提取和保存 cookies
- 刷新过期 cookies
"""

import os
import sys
import json
import time
from datetime import datetime, timedelta
from pathlib import Path

# 项目路径
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# 配置路径
COOKIES_FILE = PROJECT_ROOT / 'config' / 'xueqiu_cookies.json'
COOKIES_EXPIRE_DAYS = 30


class CookieManager:
    """雪球 Cookies 管理器"""
    
    def __init__(self):
        self.cookies_file = COOKIES_FILE
        self.expire_days = COOKIES_EXPIRE_DAYS
    
    # --------------------------------------------------------
    # 状态检查
    # --------------------------------------------------------
    
    def exists(self) -> bool:
        """检查 cookies 文件是否存在"""
        return self.cookies_file.exists()
    
    def is_expired(self) -> bool:
        """检查 cookies 是否过期"""
        if not self.exists():
            return True
        
        try:
            with open(self.cookies_file, 'r') as f:
                data = json.load(f)
            
            expires_at = data.get('expires_at')
            if expires_at:
                return datetime.now() > datetime.fromisoformat(expires_at)
            return True
            
        except Exception:
            return True
    
    def is_valid(self) -> bool:
        """检查 cookies 是否有效（存在且未过期）"""
        return self.exists() and not self.is_expired()
    
    def get_status(self) -> dict:
        """获取 cookies 状态"""
        status = {
            'exists': self.exists(),
            'expired': self.is_expired() if self.exists() else None,
            'valid': self.is_valid(),
            'file': str(self.cookies_file)
        }
        
        if self.exists():
            try:
                with open(self.cookies_file, 'r') as f:
                    data = json.load(f)
                
                status['created_at'] = data.get('created_at')
                status['expires_at'] = data.get('expires_at')
                status['has_cookies'] = bool(data.get('cookies'))
                
            except Exception as e:
                status['error'] = str(e)
        
        return status
    
    # --------------------------------------------------------
    # 读取和保存
    # --------------------------------------------------------
    
    def load(self) -> dict:
        """加载 cookies"""
        if not self.exists():
            return None
        
        with open(self.cookies_file, 'r') as f:
            data = json.load(f)
        
        return data.get('cookies')
    
    def save(self, cookies: dict, expire_days: int = None):
        """
        保存 cookies
        
        Args:
            cookies: cookies 字典
            expire_days: 过期天数
        """
        expire_days = expire_days or self.expire_days
        
        # 计算过期时间
        now = datetime.now()
        expires_at = now + timedelta(days=expire_days)
        
        data = {
            'cookies': cookies,
            'created_at': now.isoformat(),
            'expires_at': expires_at.isoformat(),
            'user_id': self._extract_user_id(cookies)
        }
        
        # 确保目录存在
        self.cookies_file.parent.mkdir(parents=True, exist_ok=True)
        
        # 保存
        with open(self.cookies_file, 'w') as f:
            json.dump(data, f, indent=2)
        
        print(f"✅ Cookies 已保存")
        print(f"   创建时间: {now.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"   过期时间: {expires_at.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"   文件位置: {self.cookies_file}")
    
    def _extract_user_id(self, cookies: dict) -> str:
        """从 cookies 中提取用户 ID"""
        # 雪球的用户 ID 通常在 'u' 或 'xq_id' cookie 中
        return cookies.get('u') or cookies.get('xq_id') or 'unknown'
    
    # --------------------------------------------------------
    # Browser 登录
    # --------------------------------------------------------
    
    def login_via_browser(self) -> bool:
        """
        通过 OpenClaw browser 登录雪球
        
        注意: 这个方法需要 OpenClaw 环境，在纯 Python 环境下不可用
        """
        print("🌐 启动浏览器登录...")
        print("   请在浏览器中完成登录操作")
        print("   登录完成后，cookies 将自动保存")
        
        # 这里需要调用 OpenClaw 的 browser 工具
        # 在 CLI 环境下，返回 False
        print("\n⚠️  当前环境不支持自动浏览器登录")
        print("   请使用 OpenClaw 对话环境，或手动提取 cookies")
        
        return False
    
    def manual_import(self, cookies_str: str = None):
        """
        手动导入 cookies
        
        Args:
            cookies_str: cookies 字符串（格式: "name=value; name2=value2"）
        """
        if cookies_str is None:
            print("\n请输入 cookies 字符串（从浏览器开发者工具复制）:")
            print("格式: name=value; name2=value2")
            print("> ", end='')
            cookies_str = input().strip()
        
        # 解析 cookies 字符串
        cookies = {}
        for item in cookies_str.split(';'):
            item = item.strip()
            if '=' in item:
                name, value = item.split('=', 1)
                cookies[name.strip()] = value.strip()
        
        if cookies:
            self.save(cookies)
            return True
        else:
            print("❌ 无效的 cookies 格式")
            return False
    
    # --------------------------------------------------------
    # 清理
    # --------------------------------------------------------
    
    def clear(self):
        """清除保存的 cookies"""
        if self.exists():
            self.cookies_file.unlink()
            print("✅ Cookies 已清除")
        else:
            print("ℹ️  没有保存的 cookies")


# ============================================================
# CLI 入口
# ============================================================

def main():
    """CLI 入口"""
    import argparse
    
    parser = argparse.ArgumentParser(description='雪球登录态管理器')
    parser.add_argument('--check', action='store_true', help='检查 cookies 状态')
    parser.add_argument('--login', action='store_true', help='通过浏览器登录')
    parser.add_argument('--import', dest='import_cookies', help='手动导入 cookies 字符串')
    parser.add_argument('--clear', action='store_true', help='清除保存的 cookies')
    
    args = parser.parse_args()
    
    manager = CookieManager()
    
    # 检查状态
    if args.check:
        status = manager.get_status()
        print("Cookies 状态:")
        print(f"  文件位置: {status['file']}")
        print(f"  是否存在: {'✅' if status['exists'] else '❌'}")
        
        if status['exists']:
            print(f"  是否过期: {'❌ 已过期' if status['expired'] else '✅ 未过期'}")
            print(f"  是否有效: {'✅' if status['valid'] else '❌'}")
            print(f"  创建时间: {status.get('created_at', '未知')}")
            print(f"  过期时间: {status.get('expires_at', '未知')}")
            print(f"  包含 cookies: {'✅' if status.get('has_cookies') else '❌'}")
        return
    
    # 浏览器登录
    if args.login:
        manager.login_via_browser()
        return
    
    # 手动导入
    if args.import_cookies:
        manager.manual_import(args.import_cookies)
        return
    
    # 清除
    if args.clear:
        manager.clear()
        return
    
    # 默认显示状态
    parser.print_help()
    print("\n当前状态:")
    status = manager.get_status()
    if status['valid']:
        print("✅ Cookies 有效")
    elif status['exists'] and status['expired']:
        print("⚠️  Cookies 已过期，请重新登录")
    else:
        print("❌ 未配置 Cookies")


if __name__ == '__main__':
    main()