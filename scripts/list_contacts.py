#!/usr/bin/env python3
"""
列出群聊与联系人，供配置监控名单时参考。
用法: python3 list_contacts.py [--config CONFIG_PATH] [--days N]

SQLCipher 4 分页解密算法致谢 zhuyansen/wx-favorites-report（MIT License），
详见 README 致谢。
"""
import argparse
import hashlib
import json
import os
import sqlite3
import struct
from datetime import datetime, timedelta

from Crypto.Cipher import AES

SQLCIPHER_PAGE_SIZE = 4096
SQLCIPHER_RESERVE = 80
SQLCIPHER_IV_LEN = 16

KEYS_FILE = os.path.expanduser("~/.config/wechat-keys.json")
DEFAULT_CONFIG_FILE = os.path.expanduser("~/.config/wechat-daily.json")
SCRATCH_DIR = os.path.expanduser("~/tmp/wechat_daily/contacts_scan")

# 只有这两个库跟"谁在跟我聊天"有关，不需要解密全部数据库
DBS_NEEDED = {
    "contact": "contact/contact.db",
    "message_0": "message/message_0.db",
}


def read_config(path):
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def resolve_db_base(config):
    if config.get("db_base_path"):
        return os.path.expanduser(config["db_base_path"])
    wxid = config.get("wxid")
    if wxid:
        return os.path.expanduser(
            f"~/Library/Containers/com.tencent.xinWeChat/Data/Documents/"
            f"xwechat_files/{wxid}/db_storage"
        )
    return None


def sqlcipher4_decrypt_pages(raw_bytes, key_hex):
    """微信 4.x 用 SQLCipher 4 默认参数：page_size=4096, reserve=80，
    每页尾部 reserve 区前 16 字节是 IV。第 0 页开头 16 字节是明文 salt，
    要单独跳过再解密。解密后把首页头替换回标准 SQLite header 即可当
    普通 sqlite3 文件打开。"""
    key = bytes.fromhex(key_hex)
    page_count = len(raw_bytes) // SQLCIPHER_PAGE_SIZE
    out = bytearray(len(raw_bytes))

    for page_idx in range(page_count):
        offset = page_idx * SQLCIPHER_PAGE_SIZE
        page = raw_bytes[offset:offset + SQLCIPHER_PAGE_SIZE]

        payload_start = SQLCIPHER_IV_LEN if page_idx == 0 else 0
        payload_len = SQLCIPHER_PAGE_SIZE - SQLCIPHER_RESERVE - payload_start
        iv = page[SQLCIPHER_PAGE_SIZE - SQLCIPHER_RESERVE:
                  SQLCIPHER_PAGE_SIZE - SQLCIPHER_RESERVE + SQLCIPHER_IV_LEN]

        plaintext = AES.new(key, AES.MODE_CBC, iv).decrypt(
            page[payload_start:payload_start + payload_len]
        )

        if page_idx == 0:
            out[offset:offset + 16] = page[:16]
            out[offset + 16:offset + 16 + len(plaintext)] = plaintext
        else:
            out[offset:offset + len(plaintext)] = plaintext

    out[0:16] = b"SQLite format 3\x00"
    out[16:18] = struct.pack(">H", SQLCIPHER_PAGE_SIZE)
    return bytes(out)


def decrypt_db_file(src_path, key_hex, dst_path):
    with open(src_path, "rb") as f:
        raw = f.read()
    with open(dst_path, "wb") as f:
        f.write(sqlcipher4_decrypt_pages(raw, key_hex))


class ChatIndex:
    """contact.db + message_0.db 解密后，把「谁」和「聊了多少条」拼起来。"""

    def __init__(self, scratch_dir):
        self.contact_db = os.path.join(scratch_dir, "contact.db")
        self.message_db = os.path.join(scratch_dir, "message_0.db")

    def display_names(self):
        names = {}
        conn = sqlite3.connect(self.contact_db)
        try:
            for user_id, remark, nickname in conn.execute(
                "SELECT userName, remark, nick_name FROM contact"
            ):
                names[user_id] = remark or nickname or user_id
        finally:
            conn.close()
        return names

    def id_by_hash(self):
        table_hash_to_id = {}
        conn = sqlite3.connect(self.contact_db)
        try:
            for (user_id,) in conn.execute("SELECT user_name FROM Name2Id"):
                table_hash_to_id[hashlib.md5(user_id.encode()).hexdigest()] = user_id
        finally:
            conn.close()
        return table_hash_to_id

    def recent_counts(self, since_ts):
        names = self.display_names()
        hash_to_id = self.id_by_hash()

        conn = sqlite3.connect(self.message_db)
        try:
            msg_tables = [
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
                )
            ]
            for table in msg_tables:
                table_hash = table[len("Msg_"):]
                user_id = hash_to_id.get(table_hash, table_hash)
                try:
                    count = conn.execute(
                        f"SELECT COUNT(*) FROM [{table}] WHERE create_time > ?", (since_ts,)
                    ).fetchone()[0]
                except sqlite3.Error:
                    count = 0
                yield {
                    "id": user_id,
                    "name": names.get(user_id, user_id),
                    "is_group": "@chatroom" in user_id,
                    "count": count,
                }
        finally:
            conn.close()


def print_ranked(title, entries, limit=None):
    print("=" * 60)
    print(title)
    print("=" * 60)
    shown = entries if limit is None else entries[:limit]
    for i, entry in enumerate(shown, 1):
        print(f"  {i}. {entry['name']} — {entry['count']}条")
    if limit is not None and len(entries) > limit:
        print(f"  ... 还有 {len(entries) - limit} 个")
    print(f"\n共 {len(entries)} 个\n")


def main():
    parser = argparse.ArgumentParser(description="列出微信群聊和联系人的近期活跃度")
    parser.add_argument("--config", default=DEFAULT_CONFIG_FILE, help="配置文件路径")
    parser.add_argument("--days", type=int, default=7, help="统计最近 N 天的消息量")
    args = parser.parse_args()

    config = read_config(args.config)
    db_base = resolve_db_base(config)
    if not db_base:
        print("[ERROR] 未配置 wxid 或 db_base_path")
        print(f"请先运行 extract_keys.py，或手动补全 {args.config}")
        return

    with open(KEYS_FILE) as f:
        keys = json.load(f)

    os.makedirs(SCRATCH_DIR, exist_ok=True)
    for name, rel_path in DBS_NEEDED.items():
        src = os.path.join(db_base, rel_path)
        if name in keys and os.path.exists(src):
            decrypt_db_file(src, keys[name], os.path.join(SCRATCH_DIR, f"{name}.db"))

    since_ts = int((datetime.now() - timedelta(days=args.days)).timestamp())
    index = ChatIndex(SCRATCH_DIR)

    groups, contacts = [], []
    for entry in index.recent_counts(since_ts):
        (groups if entry["is_group"] else contacts).append(entry)

    groups.sort(key=lambda e: e["count"], reverse=True)
    contacts.sort(key=lambda e: e["count"], reverse=True)

    print_ranked(f"群聊列表（最近{args.days}天消息数）", groups)
    print_ranked(f"联系人列表（最近{args.days}天消息数）", contacts, limit=50)


if __name__ == "__main__":
    main()
