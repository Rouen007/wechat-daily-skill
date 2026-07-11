---
name: wechat-daily
description: |
  微信聊天日报生成器。从加密的微信 Mac 4.x 本地数据库提取聊天记录，生成 AI 精华日报。
  触发词：日报、微信日报、wechat-daily
  首次使用会引导配置（密钥提取 + 选择监控群聊）。
---

# 微信日报 Skill

## 总览

本 Skill 从微信 Mac 4.x 的本地加密数据库中提取聊天记录，生成每日 AI 精华摘要日报。

**适用条件：**
- macOS 系统
- 微信 Mac 4.x 已安装
- Python 3.9+

---

## 执行流程

### Phase 1: 环境检测（每次执行）

检查 `~/.config/wechat-daily.json` 是否存在且包含 `wxid` 或 `db_base_path`：
- 不存在或不完整 → 进入 Phase 2（首次配置）
- 存在且完整 → 进入 Phase 3（生成日报）

### Phase 2: 首次配置（新用户引导）

#### Step 2a: 安装 Python 依赖

```bash
pip3 install pycryptodome zstandard
```

#### Step 2b: 提取数据库密钥

运行密钥提取脚本：

```bash
python3 {{SKILL_DIR}}/scripts/extract_keys.py
```

脚本会自动：
1. 检查微信是否安装
2. 复制微信到 `~/Desktop/WeChat.app` 并 codesign 去掉 Hardened Runtime
3. 检查/安装 frida
4. Kill 微信 → frida spawn 启动 → 注入 hook 捕获密钥
5. 提示用户登录微信并等待 30 秒
6. 自动检测 wxid 和数据库路径
7. 输出 `~/.config/wechat-keys.json` 和更新 `~/.config/wechat-daily.json`

**重要：脚本运行后需要用户手动登录微信，密钥会在启动时自动捕获。**

#### Step 2c: 选择监控对象

运行列表脚本展示所有群聊和联系人：

```bash
python3 {{SKILL_DIR}}/scripts/list_contacts.py
```

然后向用户提问（使用 AskUserQuestion 工具）：

**问题 1：** "你想监控哪些群聊？请从上面的列表中选择，输入群聊名称（多个用逗号分隔）。"

**问题 2：** "你想监控哪些具体的个人？请输入联系人备注名或昵称（多个用逗号分隔），没有可以跳过。"

用户回答后，将选择保存到 `~/.config/wechat-daily.json` 的 `monitor_groups` 和 `monitor_contacts` 字段。

#### Step 2d: 配置输出路径

向用户提问：

**问题 3：** "日报保存到哪个目录？"（默认 `~/Documents/financial_report`）

保存到配置文件的 `report_dir` 字段。

#### Step 2e: 生成首份日报

配置完成后，自动执行 Phase 3。

### Phase 3: 生成日报

#### Step 3a: 运行提取脚本

默认取**昨天 08:00 到今天 08:00** 的完整数据：

```bash
python3 {{SKILL_DIR}}/scripts/wechat_daily.py
```

如果用户指定了具体日期（如"4月16日的日报"、"2026-04-16"），用日期模式：

```bash
python3 {{SKILL_DIR}}/scripts/wechat_daily.py YYYY-MM-DD
```

如果用户有自定义配置文件路径：

```bash
python3 {{SKILL_DIR}}/scripts/wechat_daily.py --config /path/to/config.json
```

#### Step 3b: 读取原始报告

读取 Step 3a 生成的原始报告文件。报告路径从脚本输出中获取，或在配置的 `report_dir` 下查找 `YYYY-MM-DD.md`。

#### Step 3c: 生成精华摘要

你是一个群聊精华提取专家。根据原始消息，按以下格式重写报告：

**格式要求：**
- 开头：1-2段自然语言概括当天最核心的内容（不使用 markdown 格式）
- 正文：按群分类，每个群提取有价值的讨论要点
- 不使用 markdown 加粗和标题语法，用 emoji + 列表
- 链接保持原样
- 保留关键人名
- 结尾固定格式"本简报由AI自动生成"

**分类参考（根据实际内容灵活选用）：**
- 🛠 工具技巧/实战经验
- 💰 资源推荐
- 📡 行业动态
- 💡 观点碰撞
- 🎭 群友趣事
- 🐟 群聊日常
- 📈 投资/加密
- 💬 话题讨论

**交易群专用分析规则（参考 Discord Trading CLI 方法论）：**

当分析交易/投资类群聊时，重点提取以下要素：
1. **Ticker 跟踪**：所有出现的股票代码（NVDA/TSM/SPX/TSLA/MSFT/GOOGL/AMZN/META/MU/VRT 等）及其对应价格水平、止盈点、支撑阻力位
2. **期权结构分析**：Gamma/Pin/Call Wall/Put Wall/Dealer 对冲方向/CTA 触发点等做市商机制讨论
3. **关键发言人观点**：保留群内核心 KOL（如 K割/杨金龙等）的完整逻辑链，包含推理过程和前提条件
4. **交易思路汇总**：做多/做空理由、入场/止盈/止损点位、仓位管理策略
5. **市场情绪**：多空比、FOMO/恐惧程度、持仓倾向、群内共识与分歧
6. **宏观/事件驱动**：财报日历、地缘政治、政策变动等影响交易决策的外部事件
7. **金句保留**：群友生动表达（如"不被套天天都是今天又吃了，被套了才干活"）体现交易心理

交易群摘要格式：
- 先概括核心讨论主题和市场情绪基调
- 按议题（而非时间线）组织内容：期权教学 → 个股观点 → 宏观事件
- 价格点位精确保留，不四舍五入
- 逻辑链保持完整，不清楚的地方标注为推测而非事实

**内容规范：**
- 重点突出，过滤不重要的闲聊
- 语言通俗，保留群友的生动表达和金句
- 图片、语音、表情等非文本消息不写入摘要
- 系统消息（撤回、加群等）不写入摘要
- 不写人名，只总结精华内容（除非是公众人物或用户特别指定）
- 不写私聊摘要（除非用户配置了 monitor_contacts）
- 不写"其他群聊速览"
- 每个群的消息数量标注在群名后面，如 `群名（123条）`
- 群与群之间用 `---` 分隔

#### Step 3d: 写入文件

将精华摘要覆盖写入：`{report_dir}/YYYY-MM-DD.md`

然后告诉用户日报已生成，简要概括今天最核心的1-2件事。

---

## 解析数据缓存

每次提取日报时，聊天记录会先解密再解析，解析结果（发言流、发言人统计、ticker 频率等）缓存到：

```
~/tmp/wechat_parse_cache/parsed_YYYY-MM-DD.json
```

**缓存策略：**
- **TTL**：15 天自动过期
- **命中**：直接从缓存加载，不重复解密 DB，速度快
- **未命中**：解密 DB → 解析 → 写入缓存
- **启动清理**：每次运行自动检查并删除超期缓存文件

缓存可直接被 Python 脚本读取用于后续分析（如跨日期搜索、主题聚合），无需重新解密数据库。

---

## 配置文件格式

`~/.config/wechat-daily.json`：
```json
{
  "wxid": "wxid_xxx",
  "db_base_path": "~/Library/Containers/.../db_storage",
  "monitor_groups": ["群名1", "群名2"],
  "monitor_contacts": [],
  "report_dir": "~/Documents/wechat-daily",
  "time_mode": "8am_to_8am"
}
```

`~/.config/wechat-keys.json`（自动生成，不要手动编辑）：
```json
{
  "message_0": "hex_key",
  "contact": "hex_key",
  "session": "hex_key"
}
```

---

## 依赖

- Python 3.9+
- pycryptodome
- zstandard
- frida + frida-tools（仅首次密钥提取时需要）
