#!/usr/bin/env python3
"""
微信 Mac 4.x SQLCipher 密钥提取。

技术原理（致谢 zhuyansen/wx-favorites-report，MIT License，见 README 致谢）：
微信 Mac 4.x 用 SQLCipher 4 加密本地数据库，密钥经系统 CommonCrypto 的
CCKeyDerivationPBKDF（PBKDF2）派生。用 frida hook 该函数即可在微信启动时
拿到每个数据库对应的 (salt, derived_key) 对，再用 salt 精确匹配到具体
db 文件——SQLCipher 4 的 salt 就是 db 文件开头 16 字节（未加密）。
"""

import glob
import json
import os
import shutil
import subprocess
import sys
import time

CONFIG_DIR = os.path.expanduser("~/.config")
KEYS_FILE = os.path.join(CONFIG_DIR, "wechat-keys.json")
CONFIG_FILE = os.path.join(CONFIG_DIR, "wechat-daily.json")

APP_ORIGINAL = "/Applications/WeChat.app"
APP_RESIGNED = os.path.expanduser("~/Desktop/WeChat.app")
CAPTURE_LOG = "/tmp/wechat_daily_pbkdf2.jsonl"

CONTAINER_ROOT = os.path.expanduser(
    "~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files"
)

# db 文件名 -> 是否是日报流程需要的密钥
TARGET_DBS = {
    "message_0": "message/message_0.db",
    "contact": "contact/contact.db",
    "session": "session/session.db",
}

HOOK_SCRIPT = r"""
'use strict';

function findExport(symbol) {
    var addr = null;
    Process.enumerateModules().forEach(function (mod) {
        if (addr) return;
        try {
            mod.enumerateExports().forEach(function (e) {
                if (e.name === symbol) addr = e.address;
            });
        } catch (err) { /* some system modules refuse enumeration */ }
    });
    return addr;
}

var target = findExport('CCKeyDerivationPBKDF');
if (!target) {
    send({ kind: 'fatal', text: 'CCKeyDerivationPBKDF export not found' });
} else {
    send({ kind: 'ready', text: 'hooked at ' + target });

    Interceptor.attach(target, {
        onEnter: function (args) {
            this.saltPtr = args[2];
            this.saltLen = args[3].toInt32();
            this.rounds = args[5].toInt32();
            this.outPtr = args[6];
            this.outLen = args[7].toInt32();
        },
        onLeave: function () {
            try {
                var toHex = function (buf) {
                    return Array.from(new Uint8Array(buf))
                        .map(function (b) { return ('0' + b.toString(16)).slice(-2); })
                        .join('');
                };
                var record = {
                    kind: 'derived_key',
                    salt: toHex(Memory.readByteArray(this.saltPtr, Math.min(this.saltLen, 32))),
                    rounds: this.rounds,
                    key: toHex(Memory.readByteArray(this.outPtr, Math.min(this.outLen, 64))),
                };
                send(record);
            } catch (err) {
                send({ kind: 'error', text: String(err) });
            }
        },
    });
}
"""


def log(msg):
    print(f"  {msg}")


def step(n, total, title):
    print(f"\n[{n}/{total}] {title}")


def sh(cmd, allow_fail=False):
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.returncode != 0 and not allow_fail:
        log(f"命令失败: {cmd}")
        log(result.stderr.strip())
        sys.exit(1)
    return result.stdout.strip()


def ensure_macos_wechat():
    if sys.platform != "darwin":
        log("此脚本仅支持 macOS")
        sys.exit(1)
    if not os.path.isdir(APP_ORIGINAL):
        log(f"未检测到微信: {APP_ORIGINAL}")
        sys.exit(1)
    log("环境检查通过")


def ensure_resigned_copy():
    """App Store 版微信开了 Hardened Runtime，frida 无法直接注入，
    需要一份去掉该保护的签名副本才能 attach。"""
    if not os.path.isdir(APP_RESIGNED):
        log(f"复制微信到 {APP_RESIGNED} ...")
        shutil.copytree(APP_ORIGINAL, APP_RESIGNED, symlinks=True)
    sh(f'codesign --force --deep --sign - "{APP_RESIGNED}"')
    log("已生成可注入的签名副本")


def ensure_frida():
    try:
        import frida  # noqa: F401
        log(f"frida 已就绪 ({frida.__version__})")
    except ImportError:
        log("安装 frida ...")
        sh(f"{sys.executable} -m pip install frida frida-tools")


def capture_pbkdf2_calls(wait_seconds=90):
    """启动签名副本、注入 hook，把用户登录期间触发的每一次 PBKDF2
    调用（salt + 派生密钥）追加写到 CAPTURE_LOG。"""
    import frida

    sh("killall WeChat", allow_fail=True)
    time.sleep(2)

    if os.path.exists(CAPTURE_LOG):
        os.remove(CAPTURE_LOG)

    binary = os.path.join(APP_RESIGNED, "Contents", "MacOS", "WeChat")
    device = frida.get_local_device()
    pid = device.spawn([binary])
    session = device.attach(pid)
    script = session.create_script(HOOK_SCRIPT)

    captured = []

    def on_message(message, _data):
        if message.get("type") != "send":
            if message.get("type") == "error":
                log(f"[frida] {message.get('description', message)}")
            return
        payload = message["payload"]
        kind = payload.get("kind")
        if kind == "derived_key":
            captured.append(payload)
            with open(CAPTURE_LOG, "a") as f:
                f.write(json.dumps(payload) + "\n")
        elif kind in ("ready", "fatal", "error"):
            log(f"[hook] {payload.get('text')}")

    script.on("message", on_message)
    script.load()
    device.resume(pid)

    log("微信已启动 — 请登录并保持前台，密钥会在建库/开库时自动被捕获")
    for remaining in range(wait_seconds, 0, -1):
        time.sleep(1)
        if remaining % 15 == 0:
            log(f"倒计时 {remaining}s，已捕获 {len(captured)} 次派生调用")

    session.detach()

    if not captured:
        log("没有捕获到任何 PBKDF2 调用，请确认微信已登录成功后重试")
        sys.exit(1)
    log(f"共捕获 {len(captured)} 次密钥派生")
    return captured


def file_salt(db_path):
    """SQLCipher 4：db 文件的前 16 字节就是明文 salt。"""
    with open(db_path, "rb") as f:
        return f.read(16).hex()


def match_keys_to_dbs(db_base, captured):
    """按 salt 精确匹配——不同数据库用不同 salt 派生密钥，
    不能只按 rounds/长度粗筛，否则会把密钥错配给别的库。"""
    by_salt = {entry["salt"]: entry for entry in captured if entry.get("rounds") == 256000}

    resolved = {}
    for name, rel_path in TARGET_DBS.items():
        db_path = os.path.join(db_base, rel_path)
        if not os.path.exists(db_path):
            log(f"跳过 {name}：文件不存在 ({db_path})")
            continue

        salt = file_salt(db_path)
        entry = by_salt.get(salt)
        if entry is None:
            log(f"{name}: salt {salt[:12]}... 未匹配到任何捕获的密钥")
            continue

        resolved[name] = entry["key"]
        log(f"{name}: 已匹配 (salt {salt[:12]}...)")

    return resolved


def locate_account():
    candidates = glob.glob(os.path.join(CONTAINER_ROOT, "*/db_storage"))
    if not candidates:
        log(f"未在 {CONTAINER_ROOT} 下找到任何账号的 db_storage 目录")
        sys.exit(1)
    db_base = candidates[0]
    wxid = os.path.basename(os.path.dirname(db_base))
    return wxid, db_base


def persist(wxid, db_base, keys):
    os.makedirs(CONFIG_DIR, exist_ok=True)

    with open(KEYS_FILE, "w") as f:
        json.dump(keys, f, indent=2)
    log(f"密钥写入 {KEYS_FILE}")

    config = {}
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            config = json.load(f)
    config["wxid"] = wxid
    config["db_base_path"] = db_base
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    log(f"配置写入 {CONFIG_FILE}")


def main():
    print("微信 Mac 4.x 密钥提取")
    total_steps = 5

    step(1, total_steps, "环境检查")
    ensure_macos_wechat()

    step(2, total_steps, "准备可注入的签名副本")
    ensure_resigned_copy()

    step(3, total_steps, "检查 frida")
    ensure_frida()

    step(4, total_steps, "捕获 PBKDF2 密钥派生")
    captured = capture_pbkdf2_calls()

    step(5, total_steps, "按 salt 匹配密钥并写入配置")
    wxid, db_base = locate_account()
    log(f"账号: {wxid}")
    keys = match_keys_to_dbs(db_base, captured)
    if not keys:
        log("未能匹配到任何目标数据库的密钥，请重试（确保完整登录流程走完）")
        sys.exit(1)
    persist(wxid, db_base, keys)

    print("\n完成。接下来在 Claude Code 里说「日报」即可继续配置监控群聊。")


if __name__ == "__main__":
    main()
