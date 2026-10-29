# Telegram AI 群聊伙伴

基于 Python 3.12、aiogram 3、OpenAI Python SDK 和 SQLite 的中文群聊机器人。通过 New API 的 OpenAI 兼容 Chat Completions 接口进行发言判断和回复，使用 Telegram 长轮询，单机 Docker Compose 即可运行，无须开放入站端口，也不修改 New API 或 Sub2API。

机器人先筛选候选消息，再按概率采样并请求决策模型；明确 @机器人或回复机器人时优先回答。群白名单、去重、每群冷却和滚动一小时配额由程序强制执行。模型调用失败或决策 JSON 无效时保持沉默。

支持在群内调整当前群的性格、发言模式和限频，识别同一用户的连续追问，并统计模型请求、Token 用量和处理结果。每日调用预算同时覆盖决策、回复、失败和重试，防止只限制发言次数却遗漏决策调用成本。

## 快速部署

需要一台能够访问 Telegram Bot API 和 New API 网关的 Linux 服务器，已安装 Docker Engine 与 Docker Compose 插件。以下命令在项目根目录执行。

### 1. 创建机器人并配置身份

1. 在官方 [@BotFather](https://t.me/BotFather) 使用 `/newbot` 创建机器人，保存 Token。
2. 使用 `/setprivacy` 选择机器人并设置 `Disable`，以接收普通群消息；已加入的群可移除后重新添加并验证。
3. 确保机器人允许加入群组，并具有发送消息的权限。通常无须给机器人管理员权限。

Telegram 的消息可见性由隐私模式和群权限共同决定，详见 [Bots FAQ](https://core.telegram.org/bots/faq#what-messages-will-my-bot-get) 与 [Privacy mode](https://core.telegram.org/bots/features#privacy-mode)。

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

至少填写真实的 `TELEGRAM_BOT_TOKEN`、`NEW_API_BASE_URL`、`NEW_API_KEY`、`NEW_API_MODEL`。网关地址通常以 `/v1` 结尾，但应以实际部署路径为准。模型必须填写网关可用的模型 ID。

管理身份使用 Telegram **数字用户 ID**。不知道自己的 ID 时，可以先保持群白名单为空启动机器人，在私聊发送 `/start` 获取 ID，然后填入 `TELEGRAM_OWNER_IDS`。所有者和指定管理员都可执行群管理命令；Telegram 群管理员不会自动获得这些权限。

### 2. 构建、检查和启动

```bash
# 只验证 Compose 配置，不把展开后的密钥输出到终端。
docker compose config --quiet
docker compose build

# 只检查配置，不发送 Telegram 消息、不请求模型。
docker compose run --rm --no-deps telegram-ai-bot python -m app.check_config

docker compose up -d
docker compose ps
docker compose logs --tail=100 telegram-ai-bot
```

如需验证网关兼容性，可主动运行以下探测；它会发起少量真实模型调用，可能产生费用：

```bash
docker compose run --rm --no-deps telegram-ai-bot python -m app.check_config --probe
```

`--probe` 是独立的连通性检查，不使用生产机器人的每日预算，也不写入生产调用统计；它仍会向网关发送真实请求，费用以网关记录为准。

如果网关或模型不支持 `max_tokens`，将 `AI_TOKEN_PARAMETER` 改为 `max_completion_tokens`。默认不发送 JSON 模式参数；确认决策模型支持 `response_format={"type":"json_object"}` 后才能开启 `DECISION_JSON_MODE`。无论是否开启，决策输出都会经过本地严格校验。

### 3. 配置允许工作的群

填好 `TELEGRAM_OWNER_IDS` 并重建容器后，将机器人加入测试群。由配置过的所有者或管理员发送 `/chatid` 获取群 ID，这个命令允许在尚未加入白名单的群中使用。编辑 `.env`：

```dotenv
TELEGRAM_OWNER_IDS=123456789
TELEGRAM_ALLOWED_CHAT_IDS=-1001234567890,-1009876543210
```

这里的 ID 只是格式示例，必须替换为自己的真实 ID。群白名单为空时，**所有群均不触发 AI，也不保存群聊上下文**。修改 `.env` 后执行：

```bash
docker compose up -d --force-recreate
```

`docker compose restart` 不会重新加载容器的环境变量。私聊仅用于说明和身份查询，不提供 AI 陪聊。

## 接话规则

1. 只接收允许群内的真人文本或媒体说明文字，过滤命令、机器人消息和无意义内容；不会下载或识别图片、语音、视频。
2. 持久化用户消息和去重标记，按群合并短时间内的消息。每群只有一个处理任务和一个有界待处理位置，优先选择最新的直接触发，否则选最新候选，其余消息进入上下文。忙碌或冷却期间的消息不会形成无限排队，也不保证稍后补答。
3. 读取当前群设置；安静时段内不调用模型，安静模式只处理明确 @或回复。其余情况下，本地规则筛选问题、求助、征求意见等候选消息。普通候选先按当前群的采样率筛选，通过后才调用决策模型。
4. 决策结果必须是含 `action`、`reason`、`confidence` 的 JSON；只在 `action=reply` 且置信度达到阈值时生成回复。置信度是模型自评，不代表真实概率。
5. 明确 @机器人或回复机器人消息，且 `DIRECT_MENTION_ALWAYS_REPLY=true` 时，跳过随机采样和 AI 决策；符合条件的连续追问也可免 @进入回复。所有触发仍须遵守暂停、安静时段、冷却、回复间隔、小时配额和每日调用预算。
6. 每次网关请求前持久化预占每日调用次数；回复前再次检查状态和限频，在调用 Telegram 发送前另行持久化占用小时发送配额。最终只发一条纯文本消息，超长内容截断，避免模型生成长文导致多条刷屏。

`DECISION_ENABLED=false` 只关闭第二级模型决策；本地规则、随机采样和硬性限频仍然有效。达到配额、接口失败或输出无效时不发送群内报错消息。

限频按整个群计算，论坛话题共享群配额；上下文按群和话题隔离。小时配额计算的是 **AI 发送尝试次数**：发送失败、Telegram 429 或网络结果不确定也会消耗该配额，以防崩溃后的重复发言突破上限；429 还会按 Telegram 返回的等待时间推迟后续尝试。每日调用预算单独统计网关请求尝试，两者不能互相替代。最近消息总量按每群 `CONTEXT_MAX_MESSAGES` 限制，某个话题可能不足该数量。管理命令本身不调用模型，不计入这些 AI 配额。

## 管理命令

| 命令 | 权限与作用 |
| --- | --- |
| `/start` | 私聊可用，显示说明和发送者的数字用户 ID |
| `/help` | 群内仅配置的所有者或管理员可用，显示使用说明 |
| `/chatid` | 群内仅配置的所有者或管理员可用，显示群 ID；未列入白名单的群也可用 |
| `/status` | 允许群内的所有者或管理员可用，查看暂停、模型和限频状态 |
| `/pause` | 暂停当前允许群的所有 AI 回复，同时停止保存新上下文 |
| `/resume` | 恢复当前允许群的处理 |
| `/context` | 显示当前允许群保存的消息数量，不输出完整聊天记录 |
| `/settings` | 查看当前允许群的设置、有效参数和模式按钮；附参数时修改当前群配置 |
| `/settings reset` | 清除当前群全部覆盖项，恢复普通模式和部署默认值 |
| `/mode quiet` / `/mode normal` / `/mode active` | 切换当前群的安静、普通或活跃模式 |
| `/stats` | 查看当前允许群今天的调用、Token、耗时、预算余量和处理结果 |
| `/stats global` | 仅 `TELEGRAM_OWNER_IDS` 中的所有者可查看全部群的今日统计 |

管理权限由 `TELEGRAM_OWNER_IDS` 和 `TELEGRAM_ADMIN_IDS` 显式指定，普通成员不能修改设置。同一管理员在同一群执行管理命令需间隔至少 2 秒。暂停状态持久化，重启后仍有效；暂停会使尚未发出的 AI 回复失效。暂停之前已经保存的上下文仍按保留策略清理，恢复后可继续使用。

### 每群动态设置

在已允许的群中发送 `/settings`，可以查看有效参数并点击模式按钮。按钮和命令都校验配置的管理者身份，设置只影响当前群；不能通过它们修改 Bot Token、网关凭据或群白名单。

| 模式 | 普通候选采样率 | 回复间隔 | 处理冷却 | 小时发送上限 | 接话范围 |
| --- | --- | --- | --- | --- | --- |
| `normal` 普通 | 部署配置，默认 `0.35` | 默认 `20` 秒 | 默认 `8` 秒 | 默认 `12` 次 | 普通候选、直接触发和连续追问 |
| `quiet` 安静 | 沿用部署配置 | 沿用部署配置 | 沿用部署配置 | 沿用部署配置 | 只响应明确 @或回复；免 @连续追问关闭 |
| `active` 活跃 | `0.7` | `10` 秒 | `4` 秒 | `24` 次 | 普通候选、直接触发和连续追问 |

普通、安静模式使用 `.env` 的默认频率；活跃模式使用表中的预设。**显式设置的单独覆盖项优先于模式预设**，切换模式不会清掉这些覆盖项，也不会清零已经消耗的配额。所有模式仍受每日群预算和全局预算约束。

例如，管理员可以在群内依次发送以下命令（管理操作间隔至少 2 秒）：

```text
/mode active
/settings persona 简洁、耐心的技术群伙伴，多给可操作的解释
/settings reply_probability 0.5
/settings min_reply_interval_seconds 30
/settings max_replies_per_hour 20
/settings quiet_hours 23:00-08:00
/settings daily_call_limit 200
```

可修改参数为 `mode`、`persona`、`reply_probability`、`min_reply_interval_seconds`、`chat_cooldown_seconds`、`max_replies_per_hour`、`quiet_hours`、`daily_call_limit`。参数名固定使用这些英文名称，性格文本可以包含空格。

- `/settings 参数 default` 清除该项覆盖，重新继承当前模式或部署默认值；`off` 也表示清除该项覆盖，不表示把数字设置为零。`/settings mode default` 或 `/settings mode off` 恢复 `normal`，但保留其他单独覆盖项。
- `/settings quiet_hours off` 关闭安静时段。时段使用 `BOT_TIMEZONE`，支持跨午夜，开始时间包含在时段内、结束时间不包含；开始与结束不能相同。
- 安静时段会停止所有 AI 回复，**包括明确 @和回复机器人**，但仍保存允许群内的新上下文。`/pause` 则同时停止 AI 回复和新上下文记录。
- `/settings daily_call_limit 0` 禁止当前群的所有模型调用；它不是无限额度。`/settings daily_call_limit default` 恢复 `CHAT_DAILY_CALL_LIMIT`，全局上限仍由部署配置控制。
- `/settings reset` 恢复当前群全部默认设置，不清空聊天上下文，也不重置限频或每日预算。

群设置保存在 SQLite 中，修改后立即生效，无须重启；暂停和设置变更都会使尚未发出的旧 AI 回复失效，并清除该群的连续追问会话。容器重启后群设置仍保留。修改 `.env` 的全局配置仍需重建容器；已有的群覆盖项继续优先。

### 连续追问

机器人成功发送回复后，默认在 **120 秒**内识别同一用户、同一群、同一话题的短追问，例如“还有呢”“为什么”“换一个”“展开讲讲”“举个例子”。符合条件时可免 @直接进入回复生成；仍遵守所有冷却、发送配额和每日调用预算。

只有成功发出的新回复才会延长这个窗口，普通消息和被限流的追问不会续期。机器人回复另一位成员后，该话题的活跃会话转给新成员；回复其他群友或明确 @其他人的消息不会被当作机器人续问。识别采用保守规则，不会把同一用户的所有后续发言都接走。

活跃会话中的用户说“先别说话”“不用回复了”“停止回答”等，可以结束自己的当前话题会话，并使自己尚未发出的当前话题回复失效；首次回答还在排队或生成时，同一用户也可以这样取消。其他用户或话题的停止消息不能取消该回答。暂停、设置变更、窗口过期或进程重启也会清除会话。设置 `FOLLOWUP_TTL_SECONDS=0` 可关闭免 @追问；安静模式下始终需要明确 @或回复。

### 调用统计与每日预算

`/stats` 显示当前群今天的决策/回复请求次数、成功/失败/未完成次数、已知输入输出 Token、缺少用量的请求数、平均请求耗时、预算余量、最近失败类型，以及跳过、限流或发送等处理结果。`/stats global` 仅供配置的机器人所有者在允许群中查看全局汇总；指定管理员没有全局查看权限。

每日预算按 `BOT_TIMEZONE` 的**自然日**统计，默认时区为 `Asia/Shanghai`，默认每群 `200` 次、全局 `1000` 次，跨午夜后开始新一天。每次请求必须同时满足群上限和全局上限，任一达到上限都会跳过后续模型请求；全局上限设为 `0` 会关闭所有群的 AI 调用。

预算计算的是网关 HTTP **请求尝试次数**：决策和回复分别计数，每次重试再次计数。一次普通接话可能先花 1 次决策请求，再花 1 次回复请求；决策后保持沉默仍然用掉决策调用次数。请求开始前先持久化占用预算，失败、取消、超时或进程崩溃都不退还，重启不会清零。应用统一管理重试，SDK 自带的自动重试已关闭，避免重试绕过统计；全部尝试和等待仍受单次调用总超时约束。

请求成功表示模型返回了可用的非空内容，不等于最终一定在群里发言。决策 JSON 无效、群设置改变、暂停或 Telegram 发送失败，都可能使成功的模型请求没有对应群回复。崩溃留下的预占记录可能显示为“未完成”。处理结果只保存固定原因和异常类型，不保存聊天全文或网关错误正文。

费用以每百万 Token 的**美元单价**估算，需要自行配置 `AI_INPUT_PRICE_PER_MILLION` 和 `AI_OUTPUT_PRICE_PER_MILLION`；单独的决策模型可以使用 `DECISION_INPUT_PRICE_PER_MILLION`、`DECISION_OUTPUT_PRICE_PER_MILLION`。两个决策单价变量未设置时继承通用单价。单价为 `0` 表示未配置估价，不代表免费。网关未返回完整用量或单价缺失时，相应调用不计入费用合计，已有估算可能只是部分请求的金额；它不是网关账单或余额，最终费用以网关为准。

调用和结果统计使用独立的 `USAGE_RETENTION_DAYS` 策略，默认 `30` 天，不随最近 30 条聊天上下文一起清理；清理不会删除当天用于预算判断的记录。

## 配置说明

`.env.example` 包含全部配置。布尔值使用 `true`/`false`，ID 列表使用英文逗号分隔，不填写群名称或用户名。

| 配置 | 默认值 / 说明 |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | 必填，BotFather 提供的 Token |
| `TELEGRAM_ALLOWED_CHAT_IDS` | 空，拒绝所有群；填写允许群的数字 ID |
| `TELEGRAM_OWNER_IDS` / `TELEGRAM_ADMIN_IDS` | 空，管理者数字用户 ID，须为正整数 |
| `NEW_API_BASE_URL` / `NEW_API_KEY` / `NEW_API_MODEL` | 必填，网关地址、令牌和模型 ID |
| `DECISION_MODEL` | 空，复用回复模型；可单独选择较便宜的决策模型 |
| `BOT_PERSONA` | 自然、友善、有幽默感的中文群聊伙伴 |
| `CONTEXT_MAX_MESSAGES` | `30`，每群保留最近消息数，包含用户和机器人消息 |
| `CONTEXT_MAX_CHARS` | `16000`，模型上下文字符预算 |
| `MESSAGE_MAX_CHARS` | `2000`，单条输入内容字符上限 |
| `HISTORY_RETENTION_DAYS` | `7`，历史内容和去重标记保留期限 |
| `DECISION_ENABLED` | `true`，启用 AI 发言判断 |
| `DECISION_CONFIDENCE_THRESHOLD` | `0.65`，决策回复置信度阈值 |
| `DECISION_JSON_MODE` | `false`，是否向网关发送 JSON 对象格式参数 |
| `REPLY_PROBABILITY` | `0.35`，普通候选进入模型判断前的采样率，范围 0–1 |
| `DIRECT_MENTION_ALWAYS_REPLY` | `true`，直接触发绕过概率和 AI 决策，不能绕过硬限频 |
| `MIN_REPLY_INTERVAL_SECONDS` | `20`，同群 AI 回复最短间隔 |
| `CHAT_COOLDOWN_SECONDS` | `8`，同群模型处理尝试的冷却间隔，跳过或失败也会冷却 |
| `MAX_REPLIES_PER_HOUR` | `12`，同群滚动一小时 AI 发送尝试上限，发送失败也计数 |
| `MESSAGE_BATCH_WINDOW_SECONDS` | `3`，连续消息合并窗口 |
| `MESSAGE_MAX_AGE_SECONDS` | `300`，忽略超过此年龄的积压消息，避免重启后回复旧讨论 |
| `REPLY_DELAY_MIN_SECONDS` / `REPLY_DELAY_MAX_SECONDS` | `1` / `3`，发言前随机等待区间 |
| `BOT_TIMEZONE` | `Asia/Shanghai`，群安静时段、自然日预算和统计使用的 IANA 时区 |
| `FOLLOWUP_TTL_SECONDS` | `120`，成功回复后同一成员可免 @连续追问的窗口；`0` 关闭 |
| `CHAT_DAILY_CALL_LIMIT` | `200`，默认每群每日网关请求尝试上限；`0` 禁止调用，可由当前群设置覆盖 |
| `GLOBAL_DAILY_CALL_LIMIT` | `1000`，全部群合计的每日请求尝试上限；`0` 禁止所有调用 |
| `USAGE_RETENTION_DAYS` | `30`，调用及处理结果统计的独立保留天数 |
| `AI_INPUT_PRICE_PER_MILLION` / `AI_OUTPUT_PRICE_PER_MILLION` | `0` / `0`，通用每百万输入/输出 Token 美元单价；`0` 表示未配置估价 |
| `DECISION_INPUT_PRICE_PER_MILLION` / `DECISION_OUTPUT_PRICE_PER_MILLION` | 不设置时继承通用单价；可单独设置决策模型价格 |
| `AI_TIMEOUT_SECONDS` | `30`，单次模型调用总超时，包含重试时间 |
| `AI_MAX_RETRIES` | `2`，应用最多额外重试次数，每次尝试独立计入每日预算 |
| `AI_MAX_OUTPUT_TOKENS` | `300`，回复输出 Token 上限 |
| `AI_MAX_CONCURRENT_REQUESTS` | `4`，整个进程的模型调用并发上限 |
| `AI_TOKEN_PARAMETER` | `max_tokens`，可改为 `max_completion_tokens` |
| `DATABASE_PATH` | `data/bot.db`；Compose 固定覆盖为 `/app/data/bot.db` |
| `LOG_LEVEL` | `INFO` |
| `HEARTBEAT_PATH` | 程序默认 `data/bot-heartbeat`；配置模板和 Compose 使用 `/tmp/bot-heartbeat`，Windows 本地应改回数据目录 |
| `HEALTHCHECK_MAX_AGE_SECONDS` | `180`，允许的心跳最大年龄 |

提示词位于 `app/prompts/decision.txt` 与 `app/prompts/persona.txt`。群消息作为不可信的对话数据传入，不可改写系统提示词或取得管理权限。模型不接收 API Key 或 Bot Token。

## 持久化、隐私和运行约束

- SQLite 使用持久化卷保存上下文、去重记录、暂停状态、群设置、限频和调用统计。上下文清理不会清零最近一小时的配额或当天调用预算，正常重启不会绕过限频。
- 默认使用 Docker 命名卷 `bot_data`，避免宿主机目录权限导致非 root 用户无法写数据库。容器以 UID/GID `10001:10001` 运行，根文件系统只读，只有数据卷和临时目录可写。
- 群里应说明机器人会将最近聊天内容交给配置的 AI 网关处理。保留必要的用户标识用于区分发言人，不做跨群用户画像。日志不应包含聊天全文、Token 或网关错误响应正文。
- 普通消息即使不值得接话，也可能作为后续候选的最近上下文发送给模型。暂停群和未授权群不会采集新上下文。
- 同一 Bot Token 只运行一个轮询进程。不要同时在本地和服务器启动，也不要增加 Compose 副本。长轮询与 Webhook 不能同时使用；若 Token 已配置 Webhook，需先确认并移除旧部署的 Webhook。参阅 [aiogram 长轮询文档](https://docs.aiogram.dev/en/latest/dispatcher/long_polling.html)。
- 去重保证受保留期限约束，不是无限期去重。进程崩溃、Telegram 更新确认与网络发送之间不能保证严格的“恰好一次”交付；消息可能遗漏或发送结果不确定，程序不会无限重试群消息。
- 健康检查仅在成功收到 Telegram `getUpdates` 响应时刷新心跳；群里没人说话仍可保持健康，Telegram 持续不可达会使心跳过期。它不验证 AI 网关、模型额度或每次回复的成功率。Docker 的 `restart` 策略重启退出的进程，不会仅因容器 `unhealthy` 就自动重启；应结合日志和外部监控处理持续故障。

如果确实需要将数据直接放在宿主机 `./data`，先停止服务，准备权限，再把 Compose 的卷挂载改成 `./data:/app/data`：

```bash
mkdir -p data
sudo chown 10001:10001 data
sudo chmod 700 data
```

命名卷与绑定目录是不同数据位置，切换前需要备份并迁移原数据库，不能把“看起来没有历史”当成数据被自动迁移。

## 验收和故障排查

建议先在测试群逐项验收：

1. 私聊 `/start` 有响应，能获得自己的用户 ID；未配置管理员不能执行群管理命令。
2. 白名单为空或群未授权时，普通消息和 @消息均不触发模型。
3. 在允许群内 @机器人或回复机器人消息，确认正常回复；普通成员互聊时不会每条响应。
4. 连续发多条消息，确认只产生合并后的处理，并且直接 @也不能突破间隔或配额。
5. `/pause` 后不再保存新上下文和发送 AI 回复，重启后仍暂停；`/resume` 恢复。
6. 重启容器，检查上下文和限频状态仍在；两个群及不同论坛话题不共享上下文。
7. 临时配置不存在的模型或不可访问网关，确认日志记录失败类型，群内不刷报错。
8. 用 `/settings` 和模式按钮调整某个群，确认其他群不受影响；重启后覆盖项仍在，`/settings reset` 能恢复默认值。
9. 验证同一成员在同一话题连续追问可免 @；其他成员、其他话题、回复其他人以及过期后不会误触发，结束信号能停止自己的当前会话。
10. 设置短暂安静时段并确认 @也不会调用模型，同时 `/context` 仍增长；时段结束后恢复正常。
11. 在测试群设置较小的 `daily_call_limit`，确认决策、回复和重试都计数，额度耗尽后不再请求模型；重启保留额度，普通管理员不能查看 `/stats global`。

| 现象 | 检查方向 |
| --- | --- |
| 启动即退出 | 必填配置、Token 格式、数据库写权限；检查容器日志 |
| 私聊正常但群不说话 | 群白名单、暂停状态、隐私模式、发送权限；被采样或模型判断跳过属于正常行为 |
| @也没有响应 | 暂停、安静时段、回复间隔、尝试冷却、每小时配额、每日群/全局预算及网关状态；用 `/settings`、`/stats` 查看 |
| 切换模式后频率没变化 | 查看 `/settings` 的单独覆盖项；用 `/settings 参数 default` 清掉该项，或 `/settings reset` 全部恢复 |
| 追问没有免 @回答 | 是否同一用户、同一话题、窗口内，是否安静模式、回复他人或命中冷却和预算 |
| 费用估算未提供或不完整 | 检查输入/输出单价和网关是否返回完整 Token 用量；它不代表网关实际账单 |
| `Conflict` / 轮询冲突 | 是否另有实例使用同一 Token，是否遗留 Webhook |
| 网关 401/403 | New API Key、令牌权限、额度或访问策略 |
| 网关 404/模型错误 | Base URL 是否正确包含兼容接口路径，模型 ID 是否实际可用 |
| 网关 400/参数错误 | `AI_TOKEN_PARAMETER`、JSON 模式、网关对 Chat Completions 的兼容性 |
| `unhealthy` | 查看心跳与日志；排查 Telegram 网络、事件循环或数据库故障 |
| `PermissionError` / SQLite 只读 | 检查数据卷权限和 UID/GID；不要通过 root 运行掩盖目录权限问题 |

New API 必须支持相应的 [Chat Completions 接口](https://developers.openai.com/api/reference/resources/chat)。兼容网关对参数、模型和返回格式的支持可能不同，部署前应使用自身实际配置完成探测。

## 更新、备份和停止

代码或提示词更新后：

```bash
docker compose build
docker compose up -d
docker compose logs --tail=100 telegram-ai-bot
```

本版启动时通过新增 SQLite 表保存群设置和调用统计，采用 `CREATE TABLE IF NOT EXISTS`，不会删除原有聊天或限频数据。旧 `.env` 未包含新字段时会使用上述默认值；升级前仍应按下面的步骤备份持久化数据。

查看实时日志、容器健康和重启次数：

```bash
docker compose logs -f telegram-ai-bot
docker inspect --format '{{json .State.Health}}' telegram-ai-companion
docker inspect --format '{{.RestartCount}}' telegram-ai-companion
```

备份时先停止写入，再复制整个数据目录，以保留 SQLite 数据库及相关文件。备份包含群聊内容，应限制权限：

```bash
umask 077
mkdir -p backups
docker compose stop
docker compose cp telegram-ai-bot:/app/data "./backups/data-$(date +%Y%m%d-%H%M%S)"
docker compose up -d
```

恢复备份时停止服务，将整个备份数据目录恢复到原卷，并确保所有权为 `10001:10001`。不要用旧数据库覆盖正在运行的 SQLite 文件。普通停止使用 `docker compose stop`；移除容器使用 `docker compose down`，命名卷仍保留。**`docker compose down -v` 会删除数据卷及聊天历史。**

## 本地开发与测试

Linux/macOS：

```bash
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install --require-hashes -r requirements-dev.txt
cp .env.example .env
# 填写配置；本地 DATABASE_PATH 使用 data/bot.db。
python -m app.check_config
python -m pytest
python -m ruff check .
python -m app.main
```

Windows PowerShell：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements-dev.txt
Copy-Item .env.example .env
# 编辑 .env，将 HEARTBEAT_PATH 设为 data/bot-heartbeat。
.\.venv\Scripts\python.exe -m app.check_config
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m app.main
```

自动测试使用临时数据库和模型/Telegram 测试替身，不需要真实 Token，不会发送真实群消息。运行真实机器人时仍需填写有效配置，且需先停止服务器上同 Token 的实例。

`requirements.txt` 和 `requirements-dev.txt` 是包含传递依赖及哈希的锁定文件。当前锁定文件由 `scripts/lock_tested_environment.py` 从通过测试的 Python 3.12 环境生成，哈希来自 PyPI 对应版本的发布元数据，包含可用的各平台分发文件。该脚本只锁定已安装版本，不负责升级依赖。修改 `requirements.in` 或 `requirements-dev.in` 后，也可用 pip-tools 重新解析，再运行测试：

```bash
python -m piptools compile --generate-hashes --allow-unsafe --no-emit-index-url --output-file=requirements.txt requirements.in
python -m piptools compile --generate-hashes --allow-unsafe --no-emit-index-url --output-file=requirements-dev.txt requirements-dev.in
python -m pip install --require-hashes -r requirements-dev.txt
python -m pytest
python -m ruff check .
```

仓库提供 `.github/workflows/ci.yml`，推送到 GitHub 后可在 Linux 上执行测试、静态检查和 Docker 镜像构建。部署前还应在 Linux Docker 中完成构建与启动检查。单元测试和配置检查不能替代真实网关、Telegram 群权限及服务器网络验证。

## 项目结构

```text
app/
  main.py                  # 启动、长轮询和退出清理
  config.py                # 环境变量、类型和范围校验
  check_config.py          # 配置检查与可选网关探测
  healthcheck.py           # 容器健康检查
  handlers/                # 消息接收和受控管理命令
  services/                # AI 调用、决策、连续追问、会话和限频
  storage/                 # SQLite、群设置、上下文、调用统计和持久状态
  prompts/                 # 决策与性格提示词
tests/                     # 离线单元与集成测试
.env.example               # 不含真实密钥的配置模板
requirements*.in           # 直接依赖约束
requirements*.txt          # 锁定依赖及哈希
Dockerfile
compose.yaml
```
