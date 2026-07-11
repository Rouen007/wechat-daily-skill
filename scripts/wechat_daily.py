#!/usr/bin/env python3
"""
微信日报生成器 - 从加密数据库提取聊天记录生成日报
支持配置文件驱动，适配不同用户
v2: 交易群预处理（去噪/Ticker提取/价位提取/发言人统计）、DB缓存
"""
import sqlite3, struct, os, json, hashlib, argparse, re, time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from Crypto.Cipher import AES
import zstandard as zstd

# === 常量 ===
PAGE_SIZE = 4096
RESERVE = 80
IV_SIZE = 16
KEYS_FILE = os.path.expanduser("~/.config/wechat-keys.json")
CONFIG_FILE = os.path.expanduser("~/.config/wechat-daily.json")
CACHE_DIR = os.path.expanduser("~/tmp/wechat_daily")
PARSE_CACHE_DIR = os.path.expanduser("~/tmp/wechat_parse_cache")
PARSE_CACHE_TTL_DAYS = 15
NY_TZ = ZoneInfo("America/New_York")
BEIJING_TZ = ZoneInfo("Asia/Shanghai")


def ts_to_ny(ct):
    """将微信本地时间戳转换为美东时间字符串 (MM-DD HH:MM ET)"""
    # 微信 Mac 存的是本地时间（北京时间 UTC+8），先加上时区再转 NY
    local_dt = datetime.fromtimestamp(ct, tz=BEIJING_TZ)
    ny_dt = local_dt.astimezone(NY_TZ)
    return ny_dt.strftime("%m-%d %H:%M"), ny_dt

MSG_TYPE_LABELS = {
    1: "text",         # 文本
    3: "image",
    34: "voice",
    42: "card",
    43: "video",
    47: "sticker",
    48: "location",
    57: "reply",       # 引用回复（XML embed）
    10000: "system",   # 系统消息
}

# 跳过不输出的消息类型
SKIP_TYPES = {"image", "voice", "card", "video", "sticker", "location", "system"}

# XML reply 中提取文本
import xml.etree.ElementTree as ET
REPLY_TITLE_RE = re.compile(r'<title>([^<]+)</title>')

ZSTD_MAGIC = b'\x28\xb5\x2f\xfd'
_zstd_decompressor = zstd.ZstdDecompressor()

# 常见美股代码（避免把普通大写缩写当 ticker）
KNOWN_TICKERS = {
    "NVDA","TSM","SPX","SPY","NDX","QQQ","IWM","VXX","VIX","CTA",
    "AAPL","MSFT","GOOG","GOOGL","AMZN","META","TSLA","NFLX",
    "MU","VRT","QCOM","AMD","INTC","AVGO","MRVL","LITE","POET",
    "CRM","ORCL","ADBE","NOW","SNOW","CRWD","PANW","ZS","NET",
    "JPM","GS","BAC","C","WFC","MS","AXP","V","MA",
    "LLY","UNH","JNJ","PFE","MRK","ABBV","BMY","AMGN","GILD",
    "XOM","CVX","COP","EOG","SLB","OXY","DVN","HAL","PSX",
    "BA","CAT","GE","MMM","HON","UPS","FDX","NSC","UNP",
    "WMT","HD","MCD","NKE","SBUX","TGT","COST","LOW","DG",
    "GM","F","RIVN","LCID","NIO","XPEV","LI","TSEM","ARM",
    "SOFI","HOOD","RDDT","PLTR","COIN","MARA","RIOT","CLSK",
    "ENPH","FSLR","SEDG","RUN","SPWR","IOT","BE","STX","WDC",
    "SNDK","GLW","ACLS","RMBS","AMKR","AXTI","TER","KLAC",
    "VZ","T","TMUS","DASH","UBER","LYFT","ABNB","SNAP","PINS",
    "PYPL","SQ","SHOP","MELI","SE","BABA","JD","PDD","BIDU",
    "BTC","ETH","SOL","DOGE","XRP","USDT","USDC",
    "SOXS","SOXL","SQQQ","TQQQ","SPXU","UPRO","UVXY",
    "CRWV","UNH","TTWO","DIS","PYPL","PFE","TSEM","TSEM",
    "GLXY","APH","CAR","CMG","SPOT","LIN","PG",
}

# 噪声消息模式（这些消息信号量为零，直接合并计数）
NOISE_PATTERNS = [
    re.compile(r'^(感谢|谢谢)([a-zA-Z]|查理|龙|k|K).*'),
    re.compile(r'^([a-zA-Z]|查理|龙|k|K)(哥|割).*yyds'),
    re.compile(r'^(牛逼|牛啊|牛|666+|6|学到了|学习了|知道了|收到|好的老师|是的老师|对的老师)$'),
    re.compile(r'^(空它|空他|冲|冲啊|冲它)$'),
    re.compile(r'^(\[|\ud83d)[\udc00-\udfff☀-➿\[\]a-z]*$'),  # 纯 emoji/贴纸
]

TICKER_RE = re.compile(r'\b([A-Za-z]{2,5})\b', re.ASCII)


# === 配置加载 ===

def load_config(config_path=None):
    path = config_path or CONFIG_FILE
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {
        "wxid": None,
        "db_base_path": None,
        "monitor_groups": [],
        "monitor_contacts": [],
        "trading_groups": [],
        "report_dir": os.path.expanduser("~/Documents/financial_report"),
        "time_mode": "8am_to_8am",
    }


def get_db_base(config):
    if config.get("db_base_path"):
        return os.path.expanduser(config["db_base_path"])
    if config.get("wxid"):
        return os.path.expanduser(
            f"~/Library/Containers/com.tencent.xinWeChat/Data/Documents/"
            f"xwechat_files/{config['wxid']}/db_storage"
        )
    print("[ERROR] 未配置 wxid 或 db_base_path")
    return None


def get_report_dir(config):
    return os.path.expanduser(config.get("report_dir", "~/Documents/wechat-daily"))


# === 基础工具 ===

def load_keys():
    with open(KEYS_FILE) as f:
        return json.load(f)


def decrypt_sqlcipher_db(db_path, key_hex, out_path):
    """SQLCipher 4 分页解密算法致谢 zhuyansen/wx-favorites-report（MIT License），见 README 致谢。"""
    key = bytes.fromhex(key_hex)
    with open(db_path, "rb") as f:
        data = f.read()
    total_pages = len(data) // PAGE_SIZE
    result = bytearray()
    for pn in range(total_pages):
        page = data[pn * PAGE_SIZE:(pn + 1) * PAGE_SIZE]
        enc_start = 16 if pn == 0 else 0
        enc_size = PAGE_SIZE - RESERVE - enc_start
        iv = page[PAGE_SIZE - RESERVE:PAGE_SIZE - RESERVE + IV_SIZE]
        cipher = AES.new(key, AES.MODE_CBC, iv)
        dec = cipher.decrypt(page[enc_start:enc_start + enc_size])
        dp = bytearray(PAGE_SIZE)
        if pn == 0:
            dp[16:16 + len(dec)] = dec
            dp[:16] = b"SQLite format 3\x00"
            dp[16:18] = struct.pack(">H", PAGE_SIZE)
            dp[20] = RESERVE
        else:
            dp[:len(dec)] = dec
        result.extend(dp)
    with open(out_path, "wb") as f:
        f.write(result)


def load_display_names(db_path):
    db = sqlite3.connect(db_path)
    contacts = {}
    for row in db.execute("SELECT userName, remark, nick_name FROM contact"):
        contacts[row[0]] = row[1] or row[2] or row[0]
    db.close()
    return contacts


def load_table_hash_index(db_path):
    db = sqlite3.connect(db_path)
    mapping = {}
    for row in db.execute("SELECT user_name FROM Name2Id"):
        mapping[hashlib.md5(row[0].encode()).hexdigest()] = row[0]
    db.close()
    return mapping


def decode_content(content):
    """解码消息内容：zstd解压 + UTF-8解码。返回纯文本或 None。"""
    if isinstance(content, bytes):
        if content[:4] == ZSTD_MAGIC:
            try:
                content = _zstd_decompressor.decompress(content, max_output_size=100000)
            except:
                return "[压缩消息]"
        try:
            content = content.decode("utf-8", errors="replace")
        except:
            return "[二进制内容]"
    if not content or len(content.strip()) == 0:
        return None
    return content


def extract_reply_text(content):
    """从 XML 引用回复中提取可见文字。"""
    # WeChat reply ref messages contain the replied-to text in <title> tags
    titles = REPLY_TITLE_RE.findall(content)
    if titles:
        return " | ".join(t for t in titles if t.strip())
    return None


def is_xml_noise(content):
    """判断是否为系统 XML 噪声（撤回提醒、拍一拍、入群通知等）"""
    if not content:
        return True
    s = content.strip()
    if '<sysmsg type="revokemsg"' in s:
        return True
    if '<sysmsg type="pat"' in s:
        return True
    if s.startswith('<?xml') and '<appmsg' not in s:
        return True
    return False


def resolve_sender(content, contacts):
    if ':\n' in content:
        sender_id, text = content.split(':\n', 1)
        name = contacts.get(sender_id, sender_id)
        return f"{name}: {text}"
    return content


# === 解析数据缓存 ===

def parse_cache_path(date_str):
    return os.path.join(PARSE_CACHE_DIR, f"parsed_{date_str}.json")


def save_parse_cache(date_str, chat_stats):
    """将聊天统计数据(JSON序列化)缓存到 tmp 目录"""
    os.makedirs(PARSE_CACHE_DIR, exist_ok=True)
    path = parse_cache_path(date_str)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(chat_stats, f, ensure_ascii=False, default=str)
    print(f"  解析缓存已保存: {path}")


def load_parse_cache(date_str):
    """从 tmp 目录加载解析缓存，若缓存过期返回 None"""
    path = parse_cache_path(date_str)
    if not os.path.exists(path):
        return None
    cache_mtime = os.path.getmtime(path)
    age_days = (time.time() - cache_mtime) / 86400
    if age_days > PARSE_CACHE_TTL_DAYS:
        os.remove(path)
        print(f"  缓存已过期 ({age_days:.0f}天)，删除: {path}")
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return None


def cleanup_parse_cache():
    """清理过期的解析缓存（>15天）"""
    if not os.path.exists(PARSE_CACHE_DIR):
        return
    now = time.time()
    removed = 0
    for fname in os.listdir(PARSE_CACHE_DIR):
        if not fname.startswith("parsed_") or not fname.endswith(".json"):
            continue
        fpath = os.path.join(PARSE_CACHE_DIR, fname)
        age_days = (now - os.path.getmtime(fpath)) / 86400
        if age_days > PARSE_CACHE_TTL_DAYS:
            os.remove(fpath)
            removed += 1
    if removed:
        print(f"  清理过期解析缓存: {removed}个文件")


# === 数据库缓存 ===

def decrypt_databases_cached(db_base):
    """解密数据库，优先使用当天缓存"""
    keys = load_keys()
    os.makedirs(CACHE_DIR, exist_ok=True)

    paths = {
        "message_0": os.path.join(db_base, "message", "message_0.db"),
        "contact": os.path.join(db_base, "contact", "contact.db"),
        "session": os.path.join(db_base, "session", "session.db"),
    }

    today = datetime.now().strftime("%Y%m%d")

    for name, key_hex in keys.items():
        if name not in paths or not os.path.exists(paths[name]):
            continue

        src_path = paths[name]
        cache_path = os.path.join(CACHE_DIR, f"{name}.db")
        stamp_path = os.path.join(CACHE_DIR, f"{name}.stamp")

        # Check if cache from today exists and is valid
        if os.path.exists(cache_path) and os.path.exists(stamp_path):
            try:
                with open(stamp_path) as f:
                    cached_date = f.read().strip()
                src_mtime = os.path.getmtime(src_path)
                cache_mtime = os.path.getmtime(cache_path)
                if cached_date == today and cache_mtime >= src_mtime:
                    continue  # Cache valid, skip decryption
            except:
                pass

        print(f"  解密 {name}...")
        decrypt_sqlcipher_db(src_path, key_hex, cache_path)
        with open(stamp_path, "w") as f:
            f.write(today)


# === 交易数据预处理 ===

def parse_speaker(content):
    """从 'name: text' 格式（resolve_sender 输出）中分离发言人和内容"""
    if ': ' in content:
        name_part, text = content.split(': ', 1)
        # 名称不应过长，也不能是 URL 的一部分
        if 1 <= len(name_part) <= 30 and not name_part.startswith('http'):
            return name_part.strip(), text.strip()
    return None, content.strip()


def is_noise_message(content):
    """判断是否为噪声消息（感谢、跟风、纯表情等）"""
    text = content.strip()
    if len(text) <= 1:
        return True
    for pat in NOISE_PATTERNS:
        if pat.match(text):
            return True
    return False


def extract_tickers(text):
    """提取文本中的美股代码（大小写不敏感）"""
    found = set()
    for m in TICKER_RE.finditer(text):
        t = m.group(1).upper()
        if t in KNOWN_TICKERS:
            found.add(t)
    return found


def extract_price_levels(text):
    """提取价格/点位，返回 (ticker, price) 列表"""
    results = []
    # 常见模式：ticker 价格，价格 ticker，纯数字点位
    # NVDA 202.5, 6840, $420, 350-10%, etc.
    price_re = re.compile(r'\b(\d{2,5}(?:\.\d{1,2})?)\b')
    tickers = extract_tickers(text)
    prices = [m.group(1) for m in price_re.finditer(text)]
    for t in tickers:
        for p in prices[:3]:
            if 1 < len(p) <= 7:
                results.append((t, p))
    return results


def preprocess_messages(messages, is_trading_group=False):
    """预处理消息列表：去噪 + 发言人统计 + ticker/价位提取"""
    if not is_trading_group:
        return {"messages": messages, "preprocessed": False}

    speakers = {}
    ticker_mentions = {}
    price_levels = []
    clean_msgs = []
    noise_count = 0
    batch_noise = {}  # pattern -> count

    for msg in messages:
        content = msg["content"]
        # Skip non-text
        if content.startswith("[") and content.endswith("]"):
            continue

        speaker, text = parse_speaker(content)

        # Count speakers
        if speaker:
            speakers[speaker] = speakers.get(speaker, 0) + 1

        # Classify noise
        if is_noise_message(text if speaker else content):
            noise_count += 1
            # Batch similar noises
            keyword = text[:10] if speaker else content[:10]
            batch_noise[keyword] = batch_noise.get(keyword, 0) + 1
            continue

        # Extract tickers and price levels
        tickers = extract_tickers(text if speaker else content)
        for t in tickers:
            ticker_mentions[t] = ticker_mentions.get(t, 0) + 1

        prices = extract_price_levels(text if speaker else content)
        price_levels.extend(prices)

        clean_msgs.append(msg)

    # Sort speakers by count
    top_speakers = sorted(speakers.items(), key=lambda x: x[1], reverse=True)[:10]

    # Sort tickers by mentions
    top_tickers = sorted(ticker_mentions.items(), key=lambda x: x[1], reverse=True)[:20]

    # Deduplicate price levels
    seen_prices = set()
    unique_prices = []
    for t, p in price_levels:
        key = f"{t}={p}"
        if key not in seen_prices:
            seen_prices.add(key)
            unique_prices.append(f"{t} {p}")

    # Group noise for summary
    noise_summary = sorted(batch_noise.items(), key=lambda x: x[1], reverse=True)[:10]

    return {
        "preprocessed": True,
        "original_count": len(messages),
        "clean_count": len(clean_msgs),
        "noise_count": noise_count,
        "top_speakers": top_speakers,
        "top_tickers": top_tickers,
        "price_levels": unique_prices[:50],
        "noise_summary": noise_summary,
        "messages": clean_msgs,
    }


# === 消息收集 ===

def collect_messages(db, contacts, hash_map, since_ts=None, start_ts=None, end_ts=None):
    tables = [t[0] for t in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
    ).fetchall()]
    chat_stats = {}
    max_ts = since_ts or 0

    for t in tables:
        try:
            if since_ts is not None:
                rows = db.execute(
                    f"SELECT create_time, local_type, message_content, source FROM [{t}] WHERE create_time > ? ORDER BY create_time",
                    (since_ts,)
                ).fetchall()
            else:
                rows = db.execute(
                    f"SELECT create_time, local_type, message_content, source FROM [{t}] WHERE create_time BETWEEN ? AND ? ORDER BY create_time",
                    (start_ts, end_ts)
                ).fetchall()
        except:
            continue

        if not rows:
            continue

        hash_id = t.replace("Msg_", "")
        uname = hash_map.get(hash_id, hash_id)
        display = contacts.get(uname, uname)
        is_group = "@chatroom" in uname

        messages = []
        for ct, local_type, content, source in rows:
            if ct > max_ts:
                max_ts = ct
            mtype = MSG_TYPE_LABELS.get(local_type, "unknown")
            if mtype in SKIP_TYPES:
                continue
            if mtype == "unknown":
                continue  # 跳过未识别的消息类型

            decoded = decode_content(content)
            if decoded is None:
                continue

            # 过滤系统 XML 噪声
            if is_xml_noise(decoded):
                continue

            if mtype == "reply":
                # 引用回复：优先提取被引用文字，否则取 title
                reply_text = extract_reply_text(decoded)
                if reply_text and reply_text.strip():
                    content = reply_text
                else:
                    continue  # 无法提取有效内容的引用消息跳过

            if mtype == "text":
                content = decoded

            if is_group:
                content = resolve_sender(content, contacts)

            messages.append({
                "time": ts_to_ny(ct)[0],
                "ts": ct,
                "content": content,
            })

        if messages:
            chat_stats[uname] = {
                "count": len(rows),
                "text_count": len(messages),
                "display": display,
                "is_group": is_group,
                "messages": messages
            }

    return chat_stats, max_ts


# === 列表模式 ===

def list_all_chats(config):
    """列出所有群聊和联系人，供用户选择监控对象"""
    db_base = get_db_base(config)
    if not db_base:
        return

    decrypt_databases_cached(db_base)

    contacts = load_display_names(os.path.join(CACHE_DIR, "contact.db"))
    hash_map = load_table_hash_index(os.path.join(CACHE_DIR, "message_0.db"))
    db = sqlite3.connect(os.path.join(CACHE_DIR, "message_0.db"))

    week_ago = int((datetime.now() - timedelta(days=7)).timestamp())
    tables = [t[0] for t in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
    ).fetchall()]

    groups = []
    contacts_list = []

    for t in tables:
        hash_id = t.replace("Msg_", "")
        uname = hash_map.get(hash_id, hash_id)
        display = contacts.get(uname, uname)
        is_group = "@chatroom" in uname

        try:
            count = db.execute(
                f"SELECT COUNT(*) FROM [{t}] WHERE create_time > ?", (week_ago,)
            ).fetchone()[0]
        except:
            count = 0

        entry = {"name": display, "id": uname, "msg_count_7d": count}
        if is_group:
            groups.append(entry)
        else:
            contacts_list.append(entry)

    db.close()

    groups.sort(key=lambda x: x["msg_count_7d"], reverse=True)
    contacts_list.sort(key=lambda x: x["msg_count_7d"], reverse=True)

    print("=" * 50)
    print("群聊列表（最近7天消息数）")
    print("=" * 50)
    for i, g in enumerate(groups, 1):
        print(f"  {i}. {g['name']} — {g['msg_count_7d']}条")

    print(f"\n共 {len(groups)} 个群聊\n")

    print("=" * 50)
    print("联系人列表（最近7天消息数）")
    print("=" * 50)
    for i, c in enumerate(contacts_list[:50], 1):
        print(f"  {i}. {c['name']} — {c['msg_count_7d']}条")
    if len(contacts_list) > 50:
        print(f"  ... 还有 {len(contacts_list) - 50} 个联系人")

    print(f"\n共 {len(contacts_list)} 个联系人")


# === 报告生成 ===

def generate_report(chat_stats, config, target_date=None):
    """生成预处理后的报告（交易群输出结构化数据，普通群输出原始消息）"""
    if target_date is None:
        target_date = datetime.now()

    monitor_groups = set(config.get("monitor_groups", []))
    monitor_contacts = config.get("monitor_contacts")  # None = ignore all private chats
    trading_groups = set(config.get("trading_groups", []))

    # Filter by monitor list
    filtered = {}
    for uname, data in chat_stats.items():
        display = data["display"]
        is_group = data.get("is_group", "@chatroom" in uname)
        if is_group and display in monitor_groups:
            filtered[uname] = data
        elif not is_group and monitor_contacts and display in monitor_contacts:
            filtered[uname] = data
    chat_stats = filtered

    date_display = target_date.strftime("%Y-%m-%d %A")
    report = f"# 微信日报 {date_display}\n\n"
    report += f"> 生成时间: {datetime.now(NY_TZ).strftime('%Y-%m-%d %H:%M')} ET\n\n"

    total_msgs = sum(v["text_count"] for v in chat_stats.values())
    total_chats = len(chat_stats)
    report += f"## 概览\n\n"
    report += f"| 指标 | 数值 |\n|---|---|\n"
    report += f"| 活跃会话数 | {total_chats} |\n"
    report += f"| 文字消息总数 | {total_msgs} |\n\n"

    sorted_chats = sorted(chat_stats.items(), key=lambda x: x[1]["text_count"], reverse=True)

    report += f"## 活跃排行\n\n"
    for i, (uname, data) in enumerate(sorted_chats[:15]):
        is_group = data.get("is_group", "@chatroom" in uname)
        icon = "👥" if is_group else "👤"
        report += f"{i+1}. {icon} **{data['display']}** — {data['text_count']}条消息\n"
    report += "\n"

    report += f"## 聊天详情\n\n"
    for uname, data in sorted_chats:
        display = data["display"]
        is_trading = display in trading_groups

        if is_trading:
            result = preprocess_messages(data["messages"], is_trading_group=True)
            report += _format_trading_group(display, result)
        else:
            result = preprocess_messages(data["messages"], is_trading_group=False)
            report += _format_normal_group(display, result)

        report += "\n"

    return report


def _format_trading_group(display, result):
    """格式化交易群的结构化输出"""
    out = f"### {display} ({result['clean_count']}条有效消息, 过滤{result['noise_count']}条噪声)\n\n"

    if result["top_speakers"]:
        out += "#### 发言人活跃度\n\n"
        out += "| 发言人 | 消息数 |\n|---|---|\n"
        for name, count in result["top_speakers"]:
            out += f"| {name} | {count} |\n"
        out += "\n"

    if result["top_tickers"]:
        out += "#### Ticker 提及频次\n\n"
        out += "| 代码 | 次数 |\n|---|---|\n"
        for ticker, count in result["top_tickers"]:
            out += f"| {ticker} | {count} |\n"
        out += "\n"

    if result["price_levels"]:
        out += "#### 价格/点位参考\n\n"
        for p in result["price_levels"][:30]:
            out += f"- {p}\n"
        out += "\n"

    if result["noise_summary"]:
        out += "#### 噪声消息归并\n\n"
        for keyword, count in result["noise_summary"]:
            if count >= 3:
                out += f"- 「{keyword}...」x{count}\n"
        out += "\n"

    out += "#### 有效消息流\n\n"
    for msg in result["messages"]:
        out += f"- `{msg['time']}` {msg['content'][:200]}\n"

    return out


def _format_normal_group(display, result):
    """格式化普通群聊的消息输出"""
    msgs = result["messages"]
    out = f"### {display} ({len(msgs)}条)\n\n"
    for msg in msgs:
        out += f"- `{msg['time']}` {msg['content'][:200]}\n"
    return out


# === 主入口 ===

def _extract_and_report(config, start_ts, end_ts, target_date):
    """核心提取+生成逻辑，run_daily 和 run_date 共用"""
    target_str = target_date.strftime("%Y-%m-%d")

    # 启动时清理过期缓存
    cleanup_parse_cache()

    # 优先读解析缓存
    cached = load_parse_cache(target_str)
    if cached:
        print(f"解析缓存命中: {target_str}")
        chat_stats = cached
    else:
        print(f"解析缓存未命中，解密数据库...")
        decrypt_databases_cached(get_db_base(config))

        contacts = load_display_names(os.path.join(CACHE_DIR, "contact.db"))
        hash_map = load_table_hash_index(os.path.join(CACHE_DIR, "message_0.db"))
        db = sqlite3.connect(os.path.join(CACHE_DIR, "message_0.db"))

        chat_stats, _ = collect_messages(db, contacts, hash_map, start_ts=start_ts, end_ts=end_ts)
        db.close()

        if not chat_stats:
            print("没有新消息")
            return None

        # 写入解析缓存
        save_parse_cache(target_str, chat_stats)

    report = generate_report(chat_stats, config, target_date=target_date)

    report_dir = get_report_dir(config)
    os.makedirs(report_dir, exist_ok=True)
    report_path = os.path.join(report_dir, f"{target_date.strftime('%Y-%m-%d')}.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)

    total_msgs = sum(v["text_count"] for v in chat_stats.values())
    total_chats = len(chat_stats)
    print(f"日报已生成: {report_path}")
    print(f"  {total_chats} 个活跃会话, {total_msgs} 条文字消息")

    # Show trading group stats
    trading_groups = set(config.get("trading_groups", []))
    for uname, data in chat_stats.items():
        if data["display"] in trading_groups:
            result = preprocess_messages(data["messages"], is_trading_group=True)
            print(f"  [{data['display']}] {result['clean_count']}条有效 / {result['noise_count']}条噪声过滤")
            if result["top_tickers"]:
                tops = [f"{t}({c})" for t, c in result["top_tickers"][:8]]
                print(f"  Ticker: {', '.join(tops)}")

    return report_path


def run_daily(config_path=None):
    """默认模式：昨天 08:00 到今天 08:00"""
    config = load_config(config_path)
    db_base = get_db_base(config)
    if not db_base:
        return None

    today = datetime.now()
    start = today.replace(hour=8, minute=0, second=0, microsecond=0) - timedelta(days=1)
    end = today.replace(hour=8, minute=0, second=0, microsecond=0)

    print(f"日报模式（美东时间）：{start.strftime('%Y-%m-%d %H:%M')} → {end.strftime('%Y-%m-%d %H:%M')}")
    return _extract_and_report(config, int(start.timestamp()), int(end.timestamp()), start)


def run_date(date_str, config_path=None):
    """日期模式：生成指定日期 00:00 - 23:59 的报告"""
    config = load_config(config_path)
    db_base = get_db_base(config)
    if not db_base:
        return None

    target_date = datetime.strptime(date_str, "%Y-%m-%d")
    start_ts = int(target_date.replace(hour=0, minute=0, second=0).timestamp())
    end_ts = int(target_date.replace(hour=23, minute=59, second=59).timestamp())

    print(f"日期模式：{target_date.strftime('%Y-%m-%d')} 全天")
    return _extract_and_report(config, start_ts, end_ts, target_date)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="微信日报生成器 v2")
    parser.add_argument("date", nargs="?", help="指定日期 (YYYY-MM-DD)，默认昨天8点到今天8点")
    parser.add_argument("--config", help="配置文件路径", default=None)
    parser.add_argument("--list", action="store_true", help="列出所有群聊和联系人")
    args = parser.parse_args()

    config = load_config(args.config)

    if args.list:
        list_all_chats(config)
    elif args.date:
        run_date(args.date, args.config)
    else:
        run_daily(args.config)
