#!/usr/bin/env python3
"""企业微信配置检查脚本。

甲方填完 .env 后运行此脚本，验证配置是否正确。
"""

import sys
import os

# 确保能导入项目代码
sys.path.insert(0, '/root/workspace/rag/gongwen-rag-system')

from app.config import settings

def check():
    print("=" * 50)
    print("企业微信配置检查")
    print("=" * 50)

    # 1. 检查配置是否已填写
    print("\n【1/4】检查配置项是否已填写...")
    missing = []
    if not settings.WECHAT_CORP_ID:
        missing.append("WECHAT_CORP_ID")
    else:
        print(f"  ✅ CorpID: {settings.WECHAT_CORP_ID}")

    if not settings.WECHAT_AGENT_ID:
        missing.append("WECHAT_AGENT_ID")
    else:
        print(f"  ✅ AgentId: {settings.WECHAT_AGENT_ID}")

    if not settings.WECHAT_SECRET:
        missing.append("WECHAT_SECRET")
    else:
        masked = settings.WECHAT_SECRET[:6] + "..." + settings.WECHAT_SECRET[-6:]
        print(f"  ✅ Secret: {masked}")

    if missing:
        print(f"\n  ❌ 以下配置项未填写: {', '.join(missing)}")
        print("  请编辑 .env 文件，填入上述参数后再运行此脚本。")
        return False

    # 2. 尝试获取 access_token（验证 CorpID + Secret）
    print("\n【2/4】验证 CorpID + Secret（获取 access_token）...")
    try:
        from app.services.wechat_service import wechat_service
        token = wechat_service._get_access_token()
        print("  ✅ access_token 获取成功")
    except Exception as e:
        print(f"  ❌ 失败: {e}")
        print("  可能原因:")
        print("    - CorpID 或 Secret 填错了")
        print("    - 企业微信应用已被删除或禁用")
        return False

    # 3. 尝试获取 jsapi_ticket（验证 IP 在白名单里）
    print("\n【3/4】验证服务器IP是否在白名单（获取 jsapi_ticket）...")
    try:
        import requests
        url = f"https://qyapi.weixin.qq.com/cgi-bin/get_jsapi_ticket?access_token={token}"
        resp = requests.get(url, timeout=10)
        data = resp.json()
        if data.get("errcode") == 0:
            print("  ✅ jsapi_ticket 获取成功")
            print("  ✅ 服务器IP已在企业微信可信IP白名单中")
        elif data.get("errcode") == 60020:
            print(f"  ❌ 失败: {data.get('errmsg')}")
            print("  原因: 服务器IP不在企业微信'企业可信IP'白名单里")
            print(f"  当前服务器出口IP: 请把这个IP发给甲方添加到白名单")
            return False
        else:
            print(f"  ❌ 失败: {data}")
            return False
    except Exception as e:
        print(f"  ❌ 请求异常: {e}")
        return False

    # 4. 生成一个测试用的 JS-SDK config
    print("\n【4/4】生成 JS-SDK config 参数（供前端测试）...")
    try:
        test_url = "https://example.com"
        cfg = wechat_service.get_js_config(test_url)
        print(f"  corpId: {cfg['corpId']}")
        print(f"  agentId: {cfg['agentId']}")
        print(f"  timestamp: {cfg['timestamp']}")
        print(f"  nonceStr: {cfg['nonceStr']}")
        print(f"  signature: {cfg['signature'][:20]}...")
        print("  ✅ JS-SDK 签名生成成功")
    except Exception as e:
        print(f"  ⚠️ 生成失败（非致命）: {e}")

    print("\n" + "=" * 50)
    print("✅ 所有检查通过！配置正确。")
    print("=" * 50)
    print("\n下一步:")
    print("  1. 确认企业微信后台已配置：可信域名、应用主页")
    print("  2. 重启后端服务: pkill -f uvicorn && 重新启动")
    print("  3. 在企业微信内打开应用测试免登录")
    return True


if __name__ == "__main__":
    success = check()
    sys.exit(0 if success else 1)
