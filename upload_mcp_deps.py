#!/usr/bin/env python3
"""通过inst18 API上传mcp-deps到主仓库Releases（简化版）"""
import requests
import time

INST18 = "https://inst18.kekeke.cc.cd"
TOKEN = "QylTKgWtBXZJrX3wdX2Ycgt89Eb-XJk1"

def api_exec(cmd, timeout=90):
    r = requests.post(f"{INST18}/api/exec",
        json={"token": TOKEN, "cmd": cmd, "timeout": timeout},
        timeout=timeout + 15)
    try:
        d = r.json()
        return d.get("stdout", ""), d.get("stderr", ""), d.get("code", -1)
    except Exception:
        return "", f"HTTP {r.status_code}: {r.text[:200]}", -1

# 直接用python3在inst18上创建脚本文件
# 脚本内容用三引号字符串，避免引号冲突
create_script = """python3 << 'HEREDOC'
script = '''import sys,os
sys.path.insert(0,"/home/runner/work/demo-vps/demo-vps")
os.environ["INSTANCE_ID"]="inst18"
os.environ["REPO"]="eqdwgyxhyjuvyhhyg/demo-vps"
from core import storage
import config
import io,tarfile

buf = io.BytesIO()
with tarfile.open(fileobj=buf, mode="w:gz") as tar:
    tar.add(os.path.expanduser("~/files/mcp-server/node_modules"), "node_modules")
    tar.add(os.path.expanduser("~/files/mcp-server/package.json"), "package.json")
data = buf.getvalue()
print(f"packed {len(data)} bytes", flush=True)

s, p = storage.upload_asset_chunked("mcp-deps.tar.gz.enc", data, token=config.GH_TOKEN, repo="qqztceghrgji/demo-vps")
print(f"uploaded {s} bytes in {p} parts", flush=True)
print("UPLOAD_DONE", flush=True)
'''
with open("/tmp/upload_deps.py", "w") as f:
    f.write(script)
print("SCRIPT_WRITTEN")
HEREDOC"""

print("=== 创建上传脚本 ===")
out, err, code = api_exec(create_script, timeout=15)
print(f"  out: {out[:200]}")
if err:
    print(f"  err: {err[:200]}")

print("\n=== 后台执行上传 ===")
out, err, code = api_exec("nohup python3 /tmp/upload_deps.py > /tmp/upload_deps.log 2>&1 & echo PID=$!", timeout=10)
print(f"  {out}")

print("\n=== 轮询结果 ===")
for i in range(12):
    time.sleep(10)
    out, err, code = api_exec("cat /tmp/upload_deps.log 2>/dev/null", timeout=10)
    out = (out or "").strip()
    if "UPLOAD_DONE" in out:
        print(f"  [{(i+1)*10}s] 上传完成！")
        print(out)
        break
    if "Error" in out or "Traceback" in out:
        print(f"  [{(i+1)*10}s] 出错：")
        print(out[-500:])
        break
    lines = out.split("\n") if out else ["(无输出)"]
    print(f"  [{(i+1)*10}s] {lines[-1][:80]}")
else:
    out, _, _ = api_exec("cat /tmp/upload_deps.log 2>/dev/null", timeout=10)
    print(f"  最终日志：\n{(out or '(空)')[-500:]}")
