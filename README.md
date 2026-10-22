# fastintel · 云端版

把 AI 一手情报监控搬到 GitHub Actions 上跑，**电脑关机、WorkBuddy 不开，手机照样能收到 Bark 推送**。

和本地版的区别只有三点：谁按定时跑、去重状态存在哪、bark key 从哪来。抓取、关键词过滤、去重、推送格式的逻辑完全没改。

## 它怎么跑

```
GitHub Actions（每 10 分钟，海外节点）
        ↓ 抓 16 个源、关键词过滤、去重
   命中新条目 → POST api.day.app
        ↓
   苹果 APNs → 你的 iPhone（锁屏也弹）
```

去重状态（`data/seen.json`、`data/feed.json`、`data/schedule.json`）用 Actions 的 cache 在每轮之间传递，不提交进仓库，所以仓库里不会有几百个垃圾 commit。

## 部署（5 步，约 5 分钟）

### 1. 建仓库

打开 https://github.com/new

- Repository name：`fastintel`（随意）
- 可见性选 **Public**

> 必须 Public。私有仓库的 Actions 每月只有 2000 分钟免费额度，每 10 分钟一轮会跑超；Public 仓库的 Actions 分钟数不设上限。

- **不要**勾选 "Add a README file"，保持空仓库

### 2. 上传代码

在本地这个目录（`fastintel-cloud`）执行，把 `你的用户名` 换成实际用户名：

```bash
git init
git add .
git commit -m "feat: fastintel 云端定时监控"
git branch -M main
git remote add origin https://github.com/你的用户名/fastintel.git
git push -u origin main
```

第一次 push 会弹出浏览器让你登录 GitHub，登录一次以后就不用再管了。

### 3. 加 Bark key（关键，漏了这一步不会推送）

仓库页面 → **Settings** → 左侧 **Secrets and variables** → **Actions** → **New repository secret**

- Name：`BARK_KEY`
- Secret：你的 Bark key（就是 `https://api.day.app/` 后面那串 22 位字符，App 首页能看到）

代码里没有写死这个 key，只从 Secret 读，所以仓库公开也不会泄露。

### 4. 预热一次

仓库页面 → **Actions** → 左侧选 **fastintel 情报巡检** → 右侧 **Run workflow** → 点绿色的 Run。

这一轮是「首次运行」，按设计**不会推送**（避免开机把几十条历史消息一次性糊到你脸上），只是把去重基线建起来。等它跑完（约 1-2 分钟）变成绿勾。

### 5. 再跑一次验证推送

再点一次 **Run workflow**。这次如果抓到了新的命中条目，手机就会响。

想强制确认链路，可以在本地跑：

```bash
curl "https://api.day.app/你的key/云端部署完成/以后电脑关机也能收到"
```

## 之后会发生什么

- 每 10 分钟自动跑一轮，不用管任何东西
- 推送条件：命中关键词 + 来源等级 ≤ 2 + 不是历史条目。tier 3 的机器之心/量子位只进看板，不推手机
- 每轮的看板存成 Actions artifact，保留 3 天，在对应的 run 页面底部能下载

## 几个必须知道的限制

**延迟不是精确的 10 分钟。** GitHub 免费 runner 是共享池，cron 到点后要排队，实测通常延迟 2-15 分钟，早晚高峰偶尔到 30 分钟。情报类监控一般无所谓；如果你要求秒级，只能上自己的 VPS。

**海外节点反而修好了两个源。** 本地跑时 Reddit（r/LocalLLaMA、r/artificial）被墙一直失败，Actions 在美国跑，这两个源能通。HuggingFace 博客同理。

**60 天没有 commit 会被自动停掉。** 仓库里带了一个 `keepalive.yml`，每月 1 号自动提交一个时间戳，正常情况不会触发这个限制。如果收到 GitHub 的停用邮件，去 Actions 页面点一下 Enable 即可。

**改配置后要等下一轮。** 改了 `config.json`（比如加源、改关键词）push 上去，下一轮自动生效。

## 出问题怎么查

Actions → 点进失败的那次 run → 展开 **跑一轮** 这一步，日志里能看到：

- 每个源的抓取结果：`[ok] Hacker News 30 条 0.8s` 或 `[fail] Reddit ... 超时`
- 本轮命中了几条、具体标题
- `已推送: bark` 表示发出去了；`bark 拒收: {...}` 表示 key 不对或额度问题

常见的几种：

| 现象 | 原因 |
|---|---|
| 报错「没有 BARK_KEY 这个 Secret」 | 第 3 步没做，或者名字拼错了 |
| 每轮都跑但从不推送 | 关键词没命中，或命中的都是 tier 3 源。用 `Run workflow` 选 `dry` 模式看命中情况 |
| 有命中但手机没响 | iOS 设置 → 通知 → Bark，检查是否允许；再看 Bark App 里 key 是否和 Secret 一致 |
| 源大面积 `[fail]` | 该源临时挂了或改版了，看具体报错 |

## 文件说明

| 文件 | 作用 |
|---|---|
| `monitor.py` | 全部逻辑，零依赖只用标准库。改了 bark key 的注入方式（从环境变量 `BARK_KEY` 读）和推送返回值校验 |
| `config.json` | 源清单、关键词、推送渠道。bark key 是占位符，靠 Secret 注入 |
| `template.html` | 看板模板 |
| `.github/workflows/monitor.yml` | 定时任务本体 |
| `.github/workflows/keepalive.yml` | 每月一次空提交，防 60 天停用 |
