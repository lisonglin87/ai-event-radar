# AI 活动雷达

每天北京时间 18:00 自动扫一遍国内外科技公司的官方活动页 + 新闻检索，把**新出现的、现在还能报名的** AI / 计算机相关活动整理成一封邮件，发到 QQ 邮箱。

没有新活动、已截止、名额已满、报名尚未开启的，都不发邮件。

## 它做什么

```
官方源清单 (config/sources.yml, 40+ 个公司/机构官网)
        │  requests + bs4 取正文、抽链接
        ▼
   候选文本块（含「报名/大会/日期」信号的段落优先）
        │  DeepSeek 结构化抽取（JSON 模式）
        ▼
   标准化活动：主办 / 时间 / 地点 / 报名链接 / 报名状态 / 条件 / 费用
        │  指纹去重（主办+标题+月份）
        ▼
   过滤：已结束、已截止、名额已满、未开启 → 丢弃
        │  与 state/seen_events.json 比对
        ▼
   只有「全新」或「状态从非开放变为开放」的活动才发邮件
```

同时会把所有发现过的活动写进 `docs/events.md`（人可读的归档表）。

## 数据源

- **官方页**（`config/sources.yml`）：NVIDIA（GTC）/ Microsoft（Build、Ignite、Events）/ AWS（re:Invent）/ Google / OpenAI / Anthropic / Hugging Face / GitHub Universe / PyTorch / Linux Foundation / MLH，国内：阿里云云栖、华为 HDC 与全联接、百度 Create、火山引擎、小米、讯飞 1024、蚂蚁外滩大会、WAIC、CNCC、智源、QCon、SegmentFault、活动行；另有 NeurIPS / ICML / ICLR / CVPR 四个报名对外开放的学术会议。
- **纯前端渲染的官网**（华为 HDC、百度 Create、火山引擎、讯飞、外滩、WAIC、CNCC、智源、Build、Ignite…）标记 `render: js`，用 Playwright 起真实浏览器取 DOM；装不上浏览器时自动退回普通请求。
- **检索兜底**（`config/queries.yml`）：Google News 搜索 RSS，抓官网还没更新的“刚开放报名”线索（国内网络不可达，GitHub Actions 上正常）。

新增一个源 = 在 `config/sources.yml` 里加几行，不用改代码。

## 推送范围：只看国内

`config/settings.yml` 里 `scope: cn` 表示只推国内场次（中国大陆 + 港澳），改成 `all` 就恢复全球。判定顺序：

1. 标题/地点/摘要里出现中国地名（含 Shanghai / Beijing 这类英文写法）→ 国内
2. 有具体地点但没有中国字样（San Jose、Las Vegas…）→ 境外，只记录不推送
3. 地点为空或纯线上 → 看主办方是不是国内机构（阿里/华为/字节/CCF…），再看来源站是不是国内站

所以「PyTorch Day China」会推，「PyTorch Day Japan」不会；「NVIDIA GTC 中国站」会推，「GTC San Jose」不会。所有境外活动仍会写进 `docs/events.md`，只是不发邮件。

## 报名状态怎么判断

由模型根据页面原文判定，取值为 `open / upcoming / full / closed / unknown`，并要求给出原文依据（邮件里会带上“依据”一行，方便你人工核对）。

- `open`、`unknown` 且活动未过期 → 可能推送（`unknown` 会标“状态待确认”）
- `full`（名额已满）、`closed`（已截止/已结束）、`upcoming`（尚未开放）→ 不推送；`upcoming` 之后转为 `open` 时会补推
- 活动结束日期早于今天 → 永久不再推送

## 部署（GitHub Actions）

仓库 Secrets 需要 4 个：

| Secret | 说明 |
| --- | --- |
| `DEEPSEEK_API_KEY` | DeepSeek 开放平台 key |
| `SMTP_USER` | 发件 QQ 邮箱，如 `3261157251@qq.com` |
| `QQ_SMTP_AUTH_CODE` | QQ 邮箱「设置 → 账号 → 开启 SMTP」生成的 16 位授权码（不是登录密码） |
| `MAIL_TO` | 收件邮箱，可多个用英文逗号分隔 |

定时：`.github/workflows/daily.yml` 里 `cron: "0 10 * * *"`（UTC 10:00 = 北京时间 18:00）。

也可以在 Actions 页面手动 `Run workflow`，勾选 `dry_run` 只看结果不发信。

## 本地运行

```bash
cd ~/ai-event-radar
cp .env.example .env              # 填 key 和授权码
.venv/bin/playwright install chromium   # 首次：给纯前端渲染的官网用（可选）
bash run_local.sh --dry-run       # 只抓取不发信
bash run_local.sh                 # 正常跑，会写 state/ 并发信
```

常用参数：

```bash
python -m src.main --only nvidia-gtc,nvidia-events   # 只跑指定源
python -m src.main --no-search                       # 跳过检索兜底
python -m src.main --max-sources 5                   # 只跑前 5 个源，调试用
python -m src.main -v                                # 详细日志
```

## 目录结构

```
config/sources.yml      官方源清单
config/queries.yml      检索查询
src/fetcher.py          HTTP + 正文/链接抽取
src/extractor.py        LLM 结构化抽取（提示词在这里）
src/filtering.py        指纹、去重、可报名判定
src/store.py            状态存档 + Markdown 归档
src/mailer.py           QQ 邮箱发信 + HTML 模板
src/main.py             编排入口
state/seen_events.json  已发现活动与推送记录（由 Actions 自动提交）
docs/events.md          活动归档
tests/                  单元测试
```

## 已知限制（重要）

1. **GitHub Actions 的定时不准**：高峰期可能延迟 5~30 分钟；仓库连续 60 天无提交时 GitHub 会自动停用定时任务——本项目每天会提交状态文件，所以不会触发。
2. **纯 JS 渲染的页面抓不到正文**（如部分掘金/火山引擎页面），这类源会被跳过并记日志；需要时换成它的活动列表接口。
3. **“名额已满”只能从文字判断**：如果报名系统是在第三方表单里关掉的、页面上没有任何提示，本项目会把它标成 `unknown` 仍然提醒你，由你点进去确认——宁可多提醒一次，也不漏掉。
4. **不是全量活动库**：只覆盖配置里的源 + 搜索引擎能检索到的公开信息，小众社区活动、需要邀请码的闭门会不在范围内。
5. **成本**：每天约 40~60 次模型调用、10 万 tokens 量级，按 deepseek-chat 价格约 0.1~0.3 元/天。

## 改推送时间

改 `.github/workflows/daily.yml` 的 cron（UTC）。例如北京时间早上 9 点 = `0 1 * * *`。
