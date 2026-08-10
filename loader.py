#!/usr/bin/env python3
"""
项目入口加载器

从环境变量 DECRYPT_KEY 读取密钥，解密 app.enc，
解压到临时目录，设置 sys.path，执行 app.py 入口。

仓库中只有这个文件和 app.enc 是可见的，
密钥不存在于代码中（从 GitHub Secrets 注入）。
"""
import os
import sys
import io
import tarfile


def main():
    key_hex = os.environ.get("DECRYPT_KEY", "")
    if not key_hex:
        print("Error: DECRYPT_KEY not set")
        sys.exit(1)

    # 定位 app.enc（与 loader.py 同目录）
    base_dir = os.path.dirname(os.path.abspath(__file__))
    enc_path = os.path.join(base_dir, "app.enc")
    if not os.path.exists(enc_path):
        print(f"Error: {enc_path} not found")
        sys.exit(1)

    # 读取加密包
    with open(enc_path, "rb") as f:
        enc_data = f.read()

    # AES-256-GCM 解密
    try:
        from Crypto.Cipher import AES
    except ImportError:
        print("Error: pycryptodome not installed (pip install pycryptodome)")
        sys.exit(1)

    key = bytes.fromhex(key_hex)
    if len(key) != 32:
        print("Error: DECRYPT_KEY must be 32 bytes hex (64 chars)")
        sys.exit(1)

    nonce = enc_data[:12]
    tag = enc_data[-16:]
    ciphertext = enc_data[12:-16]
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    try:
        plain = cipher.decrypt_and_verify(ciphertext, tag)
    except (ValueError, KeyError) as e:
        print(f"Error: decryption failed (wrong key or corrupted): {e}")
        sys.exit(1)

    # 解压到临时目录（重启清空，不留痕）
    cache_dir = "/tmp/.ghbox_cache"
    os.makedirs(cache_dir, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(plain), mode="r:gz") as tar:
        try:
            tar.extractall(path=cache_dir, filter="tar")
        except TypeError:
            tar.extractall(path=cache_dir)

    # 复制非 Python 文件到持久化目录
    import shutil
    files_dir = os.path.expanduser("~/files")
    mcp_src = os.path.join(cache_dir, "worker", "mcp-server")
    mcp_dst = os.path.join(files_dir, "mcp-server")
    if os.path.isdir(mcp_src):
        os.makedirs(mcp_dst, exist_ok=True)
        for fname in ("index.js", "package.json"):
            src = os.path.join(mcp_src, fname)
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(mcp_dst, fname))

    # 设置 Python 路径
    sys.path.insert(0, cache_dir)
    os.chdir(cache_dir)

    # 执行 app.py 入口
    app_path = os.path.join(cache_dir, "app.py")
    if not os.path.exists(app_path):
        print(f"Error: app.py not found in decrypted cache")
        sys.exit(1)

    with open(app_path, "r") as f:
        code = f.read()
    exec(compile(code, app_path, "exec"), {"__name__": "__main__", "__file__": app_path})


if __name__ == "__main__":
    main()
