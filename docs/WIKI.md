# Telegram AI 群聊伙伴 · Wiki

> 基于 Python 3.12、aiogram 3、OpenAI Python SDK 与 SQLite 的中文群聊机器人。
> 通过 New API 的 OpenAI 兼容 Chat Completions 接口进行发言判断与回复，使用 Telegram 长轮询，
> 单机 Docker Compose 即可运行，无须开放入站端口，也不修改 New API 或 Sub2API。

---

## 1. 项目概览

- **定位**：在中文 Telegram 群组里充当“会挑时机接话”的 AI 伙伴，而不是每条都回。
- **核心机制**：先本地筛选候选消息 → 按概率采样 → 调用决策模型判断是否回复 → 仅高置信度时发言。
- **运行方式**：Telegram 长轮询（long polling），无 Webhook、无需公网入站端口。
- **持久化**：SQLite，保存上下文、去重、暂停状态、群设置、限频与调用统计。
- **成本治理**：每日调用预算同时覆盖决策、回复、失败与重试，防止只限发言却漏算决策成本。

### 1.1 架构与模块

```text
app/
  main.py                  # 启动、长轮询、退出清理
  config.py                # 环境变量读取、类型与范围校验（pydantic-settings）
  check_config.py          # 配置检查与可选网关探测（--probe）
  healthcheck.py           # 容器健康检查（依赖心跳文件）
  handlers/                # 消息接收与受控管理命令
  services/                # AI 调用、决策、连续追问、会话、限频
  storage/                 # SQLite、群设置、上下文、调用统计、持久状态
  prompts/                 # 决策(decision.txt)与性格(persona.txt)提示词
tests/                     # 离线单元与集成测试（无真实 Token/网关）
.env.example               # 配置模板（每个字段均有用途注释）
requirements*.in           # 直接依赖约束
requirements*.txt          # 锁定依赖及哈希
Dockerfile / compose.yaml  # 容器化部署
```

### 1.2 安全与隐私边界

- 群消息作为**不可信对话数据**传入模型，不能改写系统提示词或取得管理权限。
- 模型**不接收** API Key 或 Bot Token。
- 日志不包含聊天全文、Token 或网关错误响应正文。
- 仅保留区分发言人所需的最小用户标识，不做跨群用户画像。
- 容器以 UID/GID `10001:10001` 运行，根文件系统只读，仅数据卷与 tmpfs 可写。

---

## 2. 工作原理（接话规则）

1. **接收**：只接收允许群内的真人文本或媒体说明文字，过滤命令、机器人消息与无意义内容；不下载/识别图片、语音、视频。
2. **合并与去重**：持久化消息与去重标记，按群合并短时间内的消息。每群单处理任务、有界待处理位，优先最新直接触发，否则最新候选，其余进上下文。
3. **判定**：读取当前群设置；安静时段不调模型（仅明确 @/回复仍处理由配置决定）；其余由本地规则筛选候选，普通候选先按采样率筛选，通过后再调决策模型。
4. **决策**：结果须为含 `action`/`reason`/`confidence` 的 JSON；仅 `action=reply` 且置信度达标才回复。置信度为模型自评，非真实概率。
5. **直接触发**：明确 @机器人 或 回复机器人消息，且 `DIRECT_MENTION_ALWAYS_REPLY=true` 时，跳过随机采样与 AI 决策；符合条件连续追问也可免 @。所有触发仍须遵守暂停、安静时段、冷却、间隔、小时配额与每日预算。
6. **发送**：每次网关请求前预占每日调用次数；回复前复查限频，发送前再占用小时发送配额；最终只发一条纯文本，超长截断。

> `DECISION_ENABLED=false` 仅关闭第二级模型决策；本地规则、随机采样与硬限频仍有效。
> 达到配额、接口失败或输出无效时**不发群内报错**。

---

## 3. 限频与配额模型

限频按**整个群**计算，论坛话题共享群配额；上下文按群与话题隔离。

| 维度 | 含义 | 备注 |
| --- | --- | --- |
| 小时配额（`MAX_REPLIES_PER_HOUR`） | AI **发送尝试**次数 | 发送失败、429 或结果不确定也消耗，防崩溃后重复发言；429 会按返回等待时间推迟后续尝试 |
| 每日调用预算（`CHAT_DAILY_CALL_LIMIT` / `GLOBAL_DAILY_CALL_LIMIT`） | 网关**请求尝试**次数 | 决策与回复分别计数，每次重试再次计数；任一组达到上限即跳过后续请求；全局 0 关闭所有调用 |
| 处理冷却（`CHAT_COOLDOWN_SECONDS`） | 群模型处理尝试间隔 | 跳过/失败也冷却 |
| 回复间隔（`MIN_REPLY_INTERVAL_SECONDS`） | 同群 AI 回复最短间隔 | 防刷屏 |
| 最近消息总量 | 每群 `CONTEXT_MAX_MESSAGES` | 某话题可能不足该数 |

最近消息总量、小时配额、每日预算三者**互不替代**。管理命令本身不调模型，不计入 AI 配额。

---

## 4. 管理命令

| 命令 | 权限与作用 |
| --- | --- |
| `/start` | 私聊可用，显示说明与发送者数字用户 ID |
| `/help` | 允许群内仅配置的所有者/管理员可用，显示使用说明 |
| `/chatid` | 群内仅配置的所有者/管理员可用，显示群 ID；未列入白名单的群也可用 |
| `/status` | 允许群内所有者/管理员可用，查看暂停、模型与限频状态 |
| `/pause` | 暂停当前允许群所有 AI 回复，同时停止保存新上下文 |
| `/resume` | 恢复当前允许群处理 |
| `/context` | 显示当前允许群保存的消息数量（不输出完整聊天记录） |
| `/settings` | 查看当前群设置、有效参数与模式按钮；附参数可修改当前群配置 |
| `/settings reset` | 清除当前群全部覆盖项，恢复普通模式与部署默认值 |
| `/mode quiet` / `/mode normal` / `/mode active` | 切换当前群安静/普通/活跃模式 |
| `/stats` | 查看当前允许群今日调用、Token、耗时、预算余量与处理结果 |
| `/stats global` | 仅 `TELEGRAM_OWNER_IDS` 中的所有者可查看全部群今日统计 |

> 管理权限由 `TELEGRAM_OWNER_IDS` 与 `TELEGRAM_ADMIN_IDS` 显式指定；同一管理员同群执行管理命令需间隔 ≥2 秒。
> 暂停状态持久化，重启仍有效；暂停使尚未发出的 AI 回复失效。

### 4.1 每群动态设置

在已允许的群发送 `/settings` 查看有效参数并点击模式按钮（设置只影响当前群，不能改 Token/网关/白名单）。

| 模式 | 普通候选采样率 | 回复间隔 | 处理冷却 | 小时发送上限 | 接话范围 |
| --- | --- | --- | --- | --- | --- |
| `normal` 普通 | 部署配置（默认 0.35） | 20 秒 | 8 秒 | 12 次 | 普通候选、直接触发、连续追问 |
| `quiet` 安静 | 沿用部署配置 | 沿用部署配置 | 沿用部署配置 | 沿用部署配置 | 只响应明确 @/回复；免 @ 连续追问关闭 |
| `active` 活跃 | 0.7 | 10 秒 | 4 秒 | 24 次 | 普通候选、直接触发、连续追问 |

显式设置的单独覆盖项优先于模式预设；切模式不清覆盖项，也不清零已消耗配额；所有模式仍受每日群/全局预算约束。

可修改参数（英文固定名）：`mode`、`persona`、`reply_probability`、`min_reply_interval_seconds`、
`chat_cooldown_seconds`、`max_replies_per_hour`、`quiet_hours`、`daily_call_limit`。

- `/settings 参数 default` 或 `off`：清除该项覆盖（非置零）。`/settings mode default|off` 恢复 `normal`，保留其他覆盖项。
- `/settings quiet_hours off`：关闭安静时段。时段用 `BOT_TIMEZONE`，支持跨午夜，开始含、结束不含，两端不可相同。
- 安静时段停止**所有** AI 回复（含明确 @/回复），仍保存允许群新上下文；`/pause` 同时停止回复与新上下文记录。
- `/settings daily_call_limit 0` 禁止当前群所有模型调用（非无限）；`default` 恢复 `CHAT_DAILY_CALL_LIMIT`。
- `/settings reset` 恢复当前群全部默认，不清空聊天上下文，也不重置限频或每日预算。

群设置存于 SQLite，修改即时生效，无须重启；`.env` 全局配置变更仍需重建容器。

### 4.2 连续追问

成功发送回复后默认 **120 秒**内识别同一用户、同群、同话题的短追问（如“还有呢”“为什么”“换一个”），
符合条件可免 @ 直接回复，仍遵守所有冷却、发送配额与每日预算。只有成功发出的新回复才延长窗口。
用户说“先别说话”“不用回复了”“停止回答”可结束自己的当前话题会话并使其尚未发出的回复失效。
设置 `FOLLOWUP_TTL_SECONDS=0` 关闭免 @ 追问；安静模式始终需明确 @/回复。

### 4.3 调用统计与每日预算

`/stats` 显示当前群今日决策/回复请求、成功/失败/未完成、已知输入输出 Token、缺用量请求数、平均耗时、预算余量、最近失败类型及处理结果；`/stats global` 仅供所有者查看全局汇总。

- 每日预算按 `BOT_TIMEZONE` 自然日统计，默认每群 200、全局 1000，跨午夜开新一天。
- 预算计的是网关 HTTP **请求尝试次数**：决策与回复分别计数，每次重试再次计数；请求开始前先预占，失败/取消/超时/崩溃不退，重启不清零。
- 请求成功 = 模型返回可用非空内容，不等于最终群里发言。
- 费用以每百万 Token 美元单价估算，需配置 `AI_INPUT_PRICE_PER_MILLION` / `AI_OUTPUT_PRICE_PER_MILLION`，决策模型可单独配置；单价 0 表示未配置估价。

---

## 5. 配置参考总表

> 完整字段注释见仓库根目录 [`.env.example`](../../.env.example)；下表汇总默认值与作用。

| 配置 | 默认值 | 作用 |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | 必填 | BotFather Token，连接 Telegram |
| `TELEGRAM_ALLOWED_CHAT_IDS` | 空 | 允许工作的群 ID（负整数列表） |
| `TELEGRAM_OWNER_IDS` | 空 | 所有者用户 ID（正整数），含 `/stats global` |
| `TELEGRAM_ADMIN_IDS` | 空 | 管理员用户 ID（正整数），群管理命令权限 |
| `NEW_API_BASE_URL` | 必填 | 网关地址（常以 /v1 结尾） |
| `NEW_API_KEY` | 必填 | 网关令牌 |
| `NEW_API_MODEL` | 必填 | 回复模型 ID |
| `DECISION_MODEL` | 空 | 决策模型 ID，留空复用回复模型 |
| `BOT_PERSONA` | 自然、友善、有幽默感的中文群聊伙伴 | 性格系统提示词 |
| `CONTEXT_MAX_MESSAGES` | 30 | 每群保留最近消息条数 |
| `CONTEXT_MAX_CHARS` | 16000 | 每群上下文字符预算 |
| `MESSAGE_MAX_CHARS` | 2000 | 单条输入字符上限 |
| `HISTORY_RETENTION_DAYS` | 7 | 历史与去重保留天数 |
| `DECISION_ENABLED` | true | 是否启用 AI 决策 |
| `DECISION_CONFIDENCE_THRESHOLD` | 0.65 | 决策回复置信度阈值 |
| `DECISION_JSON_MODE` | false | 是否发送 JSON 对象格式参数 |
| `REPLY_PROBABILITY` | 0.35 | 普通候选采样率（0–1） |
| `DIRECT_MENTION_ALWAYS_REPLY` | true | 直接触发跳过采样与决策 |
| `MIN_REPLY_INTERVAL_SECONDS` | 20 | 同群回复最短间隔 |
| `CHAT_COOLDOWN_SECONDS` | 8 | 同群处理冷却 |
| `MAX_REPLIES_PER_HOUR` | 12 | 同群滚动一小时发送上限 |
| `MESSAGE_BATCH_WINDOW_SECONDS` | 3 | 连续消息合并窗口 |
| `MESSAGE_MAX_AGE_SECONDS` | 300 | 忽略超龄积压消息 |
| `REPLY_DELAY_MIN_SECONDS` | 1 | 发言随机等待下限 |
| `REPLY_DELAY_MAX_SECONDS` | 3 | 发言随机等待上限 |
| `BOT_TIMEZONE` | Asia/Shanghai | 安静时段/预算/统计时区 |
| `FOLLOWUP_TTL_SECONDS` | 120 | 免 @ 连续追问窗口（0 关闭） |
| `CHAT_DAILY_CALL_LIMIT` | 200 | 每群每日请求尝试上限（0 禁用） |
| `GLOBAL_DAILY_CALL_LIMIT` | 1000 | 全局每日请求尝试上限（0 禁用全部） |
| `USAGE_RETENTION_DAYS` | 30 | 调用统计保留天数 |
| `AI_INPUT_PRICE_PER_MILLION` | 0 | 通用输入单价（美元/百万） |
| `AI_OUTPUT_PRICE_PER_MILLION` | 0 | 通用输出单价（美元/百万） |
| `DECISION_INPUT_PRICE_PER_MILLION` | 继承通用 | 决策输入单价 |
| `DECISION_OUTPUT_PRICE_PER_MILLION` | 继承通用 | 决策输出单价 |
| `AI_TIMEOUT_SECONDS` | 30 | 单次调用总超时（含重试） |
| `AI_MAX_RETRIES` | 2 | 应用额外重试次数 |
| `AI_MAX_OUTPUT_TOKENS` | 300 | 回复输出 Token 上限 |
| `AI_MAX_CONCURRENT_REQUESTS` | 4 | 进程并发请求上限 |
| `AI_TOKEN_PARAMETER` | max_tokens | Token 参数名 |
| `DATABASE_PATH` | data/bot.db（Compose 覆盖 /app/data/bot.db） | SQLite 路径 |
| `LOG_LEVEL` | INFO | 日志级别 |
| `HEARTBEAT_PATH` | data/bot-heartbeat（Compose 覆盖 /tmp/bot-heartbeat） | 心跳文件路径 |
| `HEALTHCHECK_MAX_AGE_SECONDS` | 180 | 心跳最大允许年龄 |

---

## 6. 故障排查（速查）

| 现象 | 检查方向 |
| --- | --- |
| 启动即退出 | 必填配置、Token 格式、数据库写权限、容器日志 |
| 私聊正常但群不说话 | 群白名单、暂停、隐私模式、发送权限；采样/决策跳过属正常 |
| @也没有响应 | 暂停、安静时段、回复间隔、冷却、小时配额、每日群/全局预算、网关状态 |
| 切换模式频率没变 | `/settings` 单独覆盖项；用 `default` 清除或 `reset` 全部恢复 |
| 追问没免 @ | 是否同用户/同话题/窗口内，是否安静模式/回复他人/命中冷却预算 |
| `Conflict` / 轮询冲突 | 另有实例用同一 Token，或遗留 Webhook |
| 网关 401/403 | Key、令牌权限、额度、访问策略 |
| 网关 404/模型错误 | Base URL 是否含兼容接口路径，模型 ID 是否可用 |
| 网关 400/参数错误 | `AI_TOKEN_PARAMETER`、JSON 模式、Chat Completions 兼容性 |
| `unhealthy` | 心跳与日志；Telegram 网络、事件循环、数据库故障 |
| `PermissionError` / SQLite 只读 | 数据卷权限与 UID/GID；勿用 root 掩盖目录权限问题 |

详见 [部署与配置操作说明](./部署与配置操作说明.md)。
