# Telegram Keyword Bot（群关键词自动回复）

本项目使用ChatGPT5.2 Thinking制作，本人只负责复制粘贴，介意误用。以下皆由AI生成。

一个基于 **python-telegram-bot v22** 的 Telegram 群组关键词自动回复机器人。

群管理员在群里发送 `/rule`，即可一键跳转到私聊管理界面，完成 **新增 / 查看 / 删除** 关键词规则；机器人在群内根据规则自动回复。

> 适用场景：群 FAQ、关键词引导、自动客服、关键字触发指令提示等。
> 

---

## 功能特性

- **按群独立管理规则**：每个群拥有自己的规则集合，互不影响。
- **仅管理员可管理**：只有群管理员/群主能进入管理界面、增删规则。
- **私聊管理，不刷屏**：在群里 `/rule` 后点击按钮跳转私聊完成操作。
- **多种匹配模式**
    - `exact`：精确匹配（消息全文等于关键词）
    - `contains`：包含匹配（消息文本包含关键词）
    - `regex`：正则匹配（支持 Python `re`）
    - `fuzzy`：模糊匹配（基于 `rapidfuzz`，适合轻微错别字/空格差异）
- **规则列表增强**：查看规则时会显示回复内容预览（自动截断，避免太长刷屏）。
- **一键删除提示消息**：群里 `/rule` 的提示消息带「好的」按钮，管理员点一下即可删除该条提示。
- **缓存 & 立即生效**
    - 规则会做 TTL 缓存，减少 DB 查询
    - 新增/删除规则会自动 **invalidate cache**，群内立刻使用最新规则
- **防刷屏节流**：同一条规则在同一群内触发后，会有冷却时间（cooldown）避免被刷屏。
- **审计日志（Audit Log）**：增删规则会写入审计表，便于追踪是谁做了什么操作。

---

## 运行环境

- Python **3.11+**（推荐）
- MySQL / MariaDB（项目默认使用 `asyncmy` 驱动）
- Telegram Bot（需要在 BotFather 创建，并**设置 username** 以生成 deep link）

依赖见 `requirements.txt`（核心：python-telegram-bot、SQLAlchemy asyncio、asyncmy、rapidfuzz、regex、loguru、pydantic）。

---

## 项目结构

```
telegram-keyword-bot/
  run.py                     # 启动入口
  requirements.txt
  .env                       # 本地环境变量（⚠️不要提交真实 token）
  scripts/
    init_db.py               # 初始化数据库表
  app/
    bot.py                   # PTB Application/handlers 注册
    config.py                # 配置读取（pydantic-settings）
    db.py                    # SQLAlchemy async engine/session
    models.py                # 表结构：groups / rules / audit_log
    crud.py                  # CRUD：规则增删查 + 审计写入
    cache.py                 # RuleCache（TTL 缓存 + invalidate）
    matching.py              # 规则匹配（exact/contains/regex/fuzzy）
    handlers/
      admin.py               # 管理端：/rule + 私聊菜单 + 会话
      messages.py            # 群消息监听：取规则 → 匹配 → 回复
```

---

## 快速开始（本地运行）

### 1) 准备 .env

项目用 `app/config.py` 读取以下环境变量：

- `BOT_TOKEN`：你的机器人 token
- `DATABASE_URL`：SQLAlchemy URL（建议 MySQL/MariaDB）
- `username`：数据库用户名
- `password`：数据库密码
- `127.0.0.1`：数据库地址（若是1panel容器则为 1panel_mysql）
- `dbname`：数据库名
- `RULE_CACHE_TTL_SECONDS`：规则缓存 TTL（默认 10 秒）
- `RULE_COOLDOWN_SECONDS`：同规则冷却时间（默认 2 秒）

示例：

```
BOT_TOKEN=123456:ABCDEF_your_token_here
DATABASE_URL=mysql+asyncmy://username:password@127.0.0.1:3306/dbname?charset=utf8mb4
RULE_CACHE_TTL_SECONDS=10
RULE_COOLDOWN_SECONDS=2
```

> 如果你在 Docker 里连 MySQL，host 可能是 db 或容器网络别名。
> 

### 2) 安装依赖

```bash
python -m venv .venv
source .venv/bin/activate  # Windows 用 .venv\Scripts\activate
pip install -r requirements.txt
```

### 3) 初始化数据库表

```bash
python scripts/init_db.py
```

看到 `OK: tables created.` 即成功。

### 4) 启动机器人

```bash
python run.py
```

---

## 可选：Docker 部署示例 （我不知道AI写的，我也没验证过）

仓库本身不强制依赖 Docker 文件，但你可以用下面的示例快速部署（按需调整）：

### Dockerfile（示例）

```docker
FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
CMD ["python", "run.py"]
```

### docker-compose.yml（示例，MySQL + bot）

```yaml
services:
db:
image: mysql:8
environment:
MYSQL_ROOT_PASSWORD: root
MYSQL_DATABASE: telegram_keyword_bot
MYSQL_USER: bot
MYSQL_PASSWORD: botpass
command:["--character-set-server=utf8mb4","--collation-server=utf8mb4_unicode_ci"]
ports:
-"3306:3306"

bot:
build: .
environment:
BOT_TOKEN:"xxxx"
DATABASE_URL:"mysql+asyncmy://bot:botpass@db:3306/telegram_keyword_bot?charset=utf8mb4"
RULE_CACHE_TTL_SECONDS:"10"
RULE_COOLDOWN_SECONDS:"2"
depends_on:
- db
```

> 初始化表可以在容器启动时跑一次 python scripts/init_db.py，或在 bot 启动前由 entrypoint 脚本执行。
> 

---

## （推荐）使用1panel Python运行环境运行

名称：随意
项目目录：下载的文件目录
启动命令：`pip install --no-cache-dir -r requirements.txt && python scripts/init_db.py && python [run.py](http://run.py/)`

应用：Python 3.11.14

容器名称：随意

备注：随意

端口：无

环境变量：PYTHONPATH=/app

挂载：无

主机映射：无

## 使用方法（管理员）

### 1) 在群里进入管理

- 将 bot 拉进群，并赋予**管理员权限**
- 管理员发送 `/rule`
- bot 会发一条消息，包含：
    - 「🔧 去私聊管理本群规则」按钮（deep link）
    - 「好的」按钮（管理员点击后会删除这条提示）

### 2) 在私聊里管理规则

点击 deep link 后会进入私聊菜单：

- ➕ 新增规则
选择匹配模式 → 发送关键词/规则 → 发送回复 → 确认保存
- 📄 查看规则
展示前 20 条规则（含回复内容预览）并提供「删除」按钮
- 🔁 切换群（如果你最近管理过多个群）
无需回到群里再次 `/rule`，可直接切换当前管理目标

---

## 规则匹配与优先级

- 机器人会按 DB 查询结果顺序依次尝试匹配规则。
- 当前实现中规则排序大致为：
    1. `enabled`（启用的优先）
    2. `priority`（数值更小的优先）
    3. `id`（更小的更靠前）

因此你可以通过调小 `priority` 来让某条规则更先命中（默认创建为 100）。

---

## 多人/多群同时使用会不会冲突？

✅ **可以同时使用，不会互相影响。**

原因：

- 规则存储按 `group_id` 隔离，不同群的规则天然分开。
- 管理会话状态放在 `context.user_data`（按用户隔离），不同管理员互不覆盖。
- `ConversationHandler` 只在私聊场景处理管理流程，群内只负责 `/rule` 与自动回复。

唯一需要注意的点：

- **同一个管理员**如果同时管理多个群，需要通过「切换群」来选择当前管理目标（代码里已支持“最近管理群列表”）。

---

## 常见坑 & 排查

### 1) 群里关键词不触发

- **BotFather 隐私模式（privacy mode）**：
如果 bot 需要读取群内普通消息进行关键词匹配，通常要在 BotFather 关闭 privacy：`/setprivacy -> Disable`
（否则 bot 可能只能收到命令和被@的消息）

### 2) deep link 无法生成

- 需要 bot 在 BotFather 设置 **username**，否则无法生成 `https://t.me/<username>?start=...`。

### 3) 日志出现 `_is_admin Timed out`

- 这是 Telegram API 请求超时，和逻辑无关。
代码里已在 `bot.py` 对 `HTTPXRequest` 做了超时配置；如果仍频繁超时，可适当加大超时或增加重试次数。

### 4) 规则列表太长/回复太长显示不全

- 规则列表默认只显示前 20 条，并会对回复内容做预览截断（避免消息超过 Telegram 长度限制）。

---

## 开发建议

- 想增加「编辑规则」「启用/禁用规则」「调整优先级」等功能：
    - 在 `crud.py` 增加 update 接口
    - 在 `handlers/admin.py` 增加对应的会话步骤与按钮即可
- 想用 webhook：
    - `bot.py` 把 `run_polling()` 改为 webhook 启动方式，并配置公网 https

---

## License

This project is licensed under the MIT License - see the LICENSE file for details.