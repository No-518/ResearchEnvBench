import requests
import sys

# === 配置区域 ===
TOKEN = "f25734e0-3162-4a3a-88dd-8cdf5b6aef5a"
BASE_HOST = "https://chat.soruxgpt.com"

# 增加了针对 ChatGPT-Next-Web 和常见镜像站的路径
paths = [
    "/api/openai/v1",       # ChatGPT-Next-Web 标准路径
    "/codex/api/openai/v1", # 嵌套路径猜测
    "/codex/v1",            # 之前猜测
    "/v1",                  # 根目录标准
    "/api/v1",              # 常见 API 路径
    "/codex",               # 无版本号
    "/api/chat-process",    # Next-Web 内部流式接口 (特殊)
    "/backend-api/conversation" # 官方网页版接口
]

headers = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json",
    # 关键：伪装成 Mac 上的 Chrome 浏览器，绕过 Cloudflare 简单拦截
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

data = {
    "model": "gpt-5.1-codex",
    "messages": [{"role": "user", "content": "ping"}],
    "stream": False
}

print(f"🚀 开始探测主机: {BASE_HOST}")
print(f"🔑 Token 前4位: {TOKEN[:4]}...")
print("-" * 60)

for path in paths:
    # 构造完整 URL
    if "chat-process" in path:
        url = f"{BASE_HOST}{path}" # 特殊接口不加 /chat/completions
    else:
        url = f"{BASE_HOST}{path}/chat/completions"
        
    try:
        # allow_redirects=False 是关键，让我们看到 302 到底去了哪
        response = requests.post(url, json=data, headers=headers, timeout=10, allow_redirects=False)
        
        status = response.status_code
        # 格式化输出
        display_path = path.ljust(25)
        
        if status == 200:
            print(f"✅ [SUCCESS] Path: {display_path} | Status: 200 OK")
            print(f"   ⬇️  Response snippet: {response.text[:100]}")
            print(f"   💡 请设置: LLM_BASE_URL={BASE_HOST}{path}")
            # 找到一个就退出吗？不，继续找，可能有多个
        elif status in [301, 302, 307, 308]:
            loc = response.headers.get('Location', 'Unknown')
            print(f"⚠️ [REDIRECT] Path: {display_path} | Status: {status} -> {loc}")
        elif status == 404:
            print(f"❌ [NOTFOUND] Path: {display_path} | Status: 404")
        elif status == 405:
            print(f"🚫 [METHOD  ] Path: {display_path} | Status: 405 (Method Not Allowed)")
        else:
            print(f"❌ [ERROR   ] Path: {display_path} | Status: {status}")
            
    except Exception as e:
        print(f"❌ [EXCEPT  ] Path: {path.ljust(25)} | Error: {str(e)[:50]}")

print("-" * 60)