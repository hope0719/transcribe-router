# 批量录音转写 · 多 Provider 额度轮换

一个纯标准库、可直接运行的 Python 脚本项目：把一批录音丢进去，按**免费额度从多到少**自动挑一家云端 API 转写；
某家额度用完、鉴权失败、超出单文件上限或接口报错时，自动降级到下一家，并把原因打印出来。

- 只调用云端 API，**不含任何本地模型推理**
- 不涉及账号开通、实名、绑卡流程
- 7 家 provider 覆盖报告主表中「有公开 API 且免费额度可用于 API」的全部服务
- 只用 Python 标准库 + `ffmpeg`，`python3 -m transcribe_router` 即可运行

---

## 1. 7 家 Provider 一览

| # | provider key | 产品 | 默认额度（按报告） | 额度类型 | 单文件上限 | 是否只收公网 URL |
|---|---|---|---|---|---|---|
| 1 | `tencent` | 腾讯云 ASR（录音文件识别） | 10 小时/月 | `monthly` | 5 小时 / 1GB（本地直传 5MB） | 否（≤5MB 可直传） |
| 2 | `aliyun_isi` | 阿里云智能语音交互 ISI | 2 小时/天，3 个月窗口 | `daily` + 到期日 | 12 小时 / 512MB | 是 |
| 3 | `bailian` | 阿里云百炼 Paraformer（录音文件识别） | 10 小时/月（`paraformer-v2`） | `monthly` | 12 小时 / 2GB | 是 |
| 4 | `tingwu` | 通义听悟 API | 2 小时/天，90 天窗口 | `daily` + 到期日 | 12 小时（未验证） | 是 |
| 5 | `iflytek` | 讯飞开放平台 语音转写 | 个人 5 小时/年（企业 50 小时/年） | `fixed` | 5 小时 / 500MB | 否（本地上传） |
| 6 | `volcano` | 火山引擎豆包 录音文件识别 | 20 小时/半年 | `fixed` | 5 小时 / 512MB | 是 |
| 7 | `netease` | 网易见外（有道长语音转写） | 300 分钟 / 30 天 | `fixed` | 4 小时 / 500MB | 否（本地上传） |

> 阿里云三兄弟 `aliyun_isi` / `bailian` / `tingwu` 是**三条独立产品线、三个独立额度池**，
> 代码与账本里都按三个独立 provider 记账，不会合并。

**排序与权重**：先比 `priority`（大者先用），再比剩余额度（多者先用）。
默认 `priority`：5 家主力 = 10，讯飞 = 3，网易见外 = 1。
因此讯飞（额度小）和网易见外（约 24 元/小时）即便剩余额度看起来不少，也会被压到最后才用。

---

## 2. 快速开始

```bash
cd outputs
cp config.example.yaml config.yaml
cp .env.example .env        # 填你自己的 Key

python3 -m transcribe_router quota                       # 看每家剩余额度
python3 -m transcribe_router run ./audios -o ./out       # 批量转写
```

依赖：

- Python 3.9+（仅标准库；装了 PyYAML 则配置文件支持完整 YAML，否则用内置子集解析器）
- `ffmpeg` / `ffprobe`（切段与时长探测）：`brew install ffmpeg` 或 `apt install ffmpeg`

---

## 3. 每家要填哪些字段

配置文件里**永远不写明文密钥**，用 `${环境变量}` 占位，实际值放 `.env`：

```yaml
providers:
  tencent:
    credentials:
      secret_id: ${TENCENT_SECRET_ID}
      secret_key: ${TENCENT_SECRET_KEY}
```

| provider | credentials 字段（全部必填） | 说明 |
|---|---|---|
| `tencent` | `secret_id` / `secret_key` | 腾讯云访问密钥 SecretId / SecretKey |
| `aliyun_isi` | `access_key_id` / `access_key_secret` / `appkey` | 阿里云 AK + ISI 项目的 Appkey |
| `bailian` | `api_key` | 百炼 / DashScope API Key |
| `tingwu` | `access_key_id` / `access_key_secret` / `app_key` | 阿里云 AK + 听悟项目 AppKey |
| `iflytek` | `app_id` / `secret_key` | 讯飞控制台「语音转写」服务里的 AppID / SecretKey |
| `volcano` | `appid` / `token` / `cluster` | 火山控制台的 AppID、Access Token、Cluster ID |
| `netease` | `app_key` / `app_secret` | 有道智云应用 ID / 应用密钥 |

`.env.example` 已列好全部环境变量名。没填凭据的 provider 会在启动时打印「未配置凭据（…），跳过」，
不影响其它家。

### 额度与限制也要按你自己的账号填

`quota.hours` 填**你账号实际拿到的免费小时数**，脚本不会去猜：

```yaml
quota:
  mode: monthly          # monthly | daily | fixed
  hours: 10
  expires_at: ""         # 写法一：绝对到期日，如 2027-02-15
  expires_after_days: 90 # 写法二：从首次成功转写起算 N 天
```

- `monthly`：每月 1 日 0 点自动清零本期用量（腾讯、百炼 `paraformer-v2`）
- `daily`：每天 0 点清零（通义听悟、阿里云 ISI 的 2h/天）
- `fixed`：一次性总额度，用完为止（讯飞、火山、网易见外、百炼 `fun-asr`）

**到期规则有两种写法，按官方给的信息选一种：**

| 写法 | 适用场景 | 行为 |
|---|---|---|
| `expires_at: 2027-02-15` | 控制台/文档给了确切到期日 | 那天 23:59:59 之后失效；也可写完整 ISO 时间 |
| `expires_after_days: 90` | 官方只说「试用 90 天」没给日期 | **首次成功转写**时把时间写进账本的 `first_used_at`，之后按 `first_used_at + 90 天` 推算 |

- 没用过之前不计时（不会因为放着不用就过期）；用过之后才倒计时
- 两种都填时以 `expires_at` 为准
- 过期后账本把该家剩余额度记为 0，启动表打印「免费额度已于 … 过期，不会参与自动路由」，
  路由跳过原因是「免费额度已过期」；续期后改配置或 `reset` 即可
- 旧版本账本没有 `first_used_at`：如果该家已有用量，会按最后一次用量时间补记，避免试用期永不开始

单文件上限在 `limits.max_file_seconds` / `limits.max_file_mb`，QPS 在 `limits.qps`，都可改。

---

## 4. 查看剩余额度

```bash
python3 -m transcribe_router quota
```

```
provider    产品                          模式      总额度    本期已用    剩余    到期      状态
tencent     腾讯云 ASR（录音文件识别）        每月     10.00 小时   3.20 小时   6.80 小时   无       可用
aliyun_isi  阿里云智能语音交互 ISI（…）       每日      2.00 小时   0 小时     2.00 小时   首次用后90天   可用
iflytek     讯飞开放平台（语音转写）          一次性     5.00 小时   0 小时     5.00 小时   无       可用
netease     网易见外（有道长语音转写）         一次性     5.00 小时   0 小时     5.00 小时   无       可用
```

- `--json` 输出机器可读格式
- 账本位置：`<state_dir>/ledger.json`，进程重启不丢
- `python3 -m transcribe_router providers` 可以看到排序与凭据配置情况

**重要说明**：这些 API 大多**不提供「查询剩余免费额度」的接口**（腾讯/阿里云/火山均无公开的余额查询 API），
所以本项目的额度账本是**本地按音频时长记账**：每成功转写一段，就把该段时长累加进账本。
如果控制台显示的额度与账本不一致，用下面的手动命令校正。

---

## 5. 手动标记某家额度已用完 / 恢复

```bash
# 标记用完：之后路由会跳过这家，直到新周期或手动 reset
python3 -m transcribe_router exhaust tencent --reason "本月 10 小时已在控制台用尽"

# 恢复：清掉手动标记、鉴权失败冷却，并把本期用量清零
python3 -m transcribe_router reset tencent
```

也可以直接改 `ledger.json` 里的 `manual_exhausted` / `used_hours_period`，但更推荐用命令。

---

## 6. 自动选路与降级的规则

每一段音频都会重新排一次队：

1. 排除：手动标记用完、鉴权失败冷却中、免费额度过期、剩余额度低于 `min_remaining_minutes`、
   超出该家单文件时长/大小上限、该家只收公网 URL 但当前没配 `hosting`；
2. 剩下的按 `priority` 降序、剩余额度降序排序；
3. 依次尝试，遇到下列情况立即换下一家，并打印降级原因：

| 情况 | 处理 |
|---|---|
| 鉴权失败（401/签名错误/无效 Key） | 该家冷却 `auth_retry_minutes`（默认 30 分钟），换下一家 |
| 额度耗尽（欠费/额度不足/超出免费量） | 账本标记该家用完（新周期自动解除），换下一家 |
| 超出单文件上限 | 直接跳过，换下一家 |
| 网络错误 / 5xx / 限流 | 原地重试 `retries_per_provider` 次，仍失败再换下一家 |
| 同一家连续失败 | 累计达到 `max_consecutive_failures`（默认 3）后熔断 `failure_cooldown_minutes`（默认 15 分钟），期间不再被选中 |
| 其它接口报错 | 记录原因，换下一家 |

所有循环都是有界的：候选 provider 列表固定、每家重试次数固定、全部失败即抛出 `AllProvidersFailed`
并标记该段失败（保留断点），不会无限重试。

启动时和每次切换都会打印：

```
当前使用 腾讯云 ASR（录音文件识别），剩余 6.80 小时 / 额度到期 长期（无固定到期日）
[切换] 腾讯云 ASR（录音文件识别） 不可用（额度耗尽）→ 当前使用 讯飞开放平台（语音转写），剩余 5.00 小时 / 额度到期 2027-12-31 23:59
```

`--provider tencent` 可以强制只用某一家：此时额度检查被跳过（方便你强制消耗指定额度），
但单文件上限等硬性限制仍然生效；失败也不会自动降级。

---

## 7. 长音频切段与边界处理

超过某家单文件上限的音频会自动切段（`ffmpeg`），并按绝对时间偏移把时间戳拼回去。

- **段长**：`segmentation.max_seconds`；为 0 时取「当前还有额度的 provider 中最小的单文件上限」
- **切点**：`mode: silence` 时先跑 `silencedetect`，把切点吸附到目标位置附近最近的静音中点
  （搜索窗口 `search_window_seconds`，默认 30s），避免从词中间切断；找不到静音则退回固定切点
- **重叠区**：除第一段外，每段向前多带 `overlap_seconds`（默认 0.5s）。
  完全落在重叠区里的句子属于上一段，会被丢弃；**跨切点的句子不丢弃**，只把起点裁到切点，
  再由后处理把「上一段的半句 + 本段的整句」按文本包含关系合并成一条（保留完整文本、时间取并集）。
  这样既不会重复，也不会因为「上一段被截断、下一段又被丢弃」而丢内容。
- **最后一段**：结束时间统一裁剪到文件总时长，不会越过音频末尾
- **时间戳合并**：每段的句级时间戳都加上该段的起始偏移，得到全文件绝对时间；
  末尾统一裁剪到文件总时长
- **统一转码**：每段转成 16kHz / 单声道 / mp3 64k，规避各家采样率/格式要求差异

切出来的片段存放在 `<state_dir>/jobs/<hash>/seg_*.mp3`，任务成功后自动清理
（`output.keep_segments: true` 可保留用于排障）。

---

## 8. 输出

每个音频文件输出两个文件到 `-o` 目录：

**`<文件名>.json`**

```json
{
  "source": "/abs/path/meeting_a.wav",
  "duration_ms": 20000,
  "providers": ["iflytek", "tencent"],
  "segments": [
    {"index": 0, "start_ms": 0, "end_ms": 4600, "boundary_ms": 0,
     "provider": "tencent", "status": "done", "utterances": [...]}
  ],
  "utterances": [
    {"start_ms": 0, "end_ms": 1150, "text": "……", "speaker": "1"}
  ],
  "text": "纯文本，按句换行"
}
```

`utterances[].start_ms/end_ms` 是**全文件绝对时间**，`speaker` 只有该家返回说话人信息时才有。

**`<文件名>.srt`**：由句级时间戳转换而来。报告里 7 家 API 都不直接给 SRT，
`output.srt_max_chars > 0` 时还会按中文标点把过长句子拆成多条字幕。

---

## 9. 并发与限流

```yaml
concurrency:
  workers: 2          # 同时处理几个片段
  per_provider: 1     # 同一家最多几个任务（讯飞个人包只有 2 并发）

providers:
  tencent:
    limits:
      qps: 20         # 腾讯提交接口默认 20 次/秒
```

- `qps` 是每次真实 HTTP 请求前的最小间隔（进程内按 provider 共享限速器）
- 异步任务轮询间隔取 `routing.poll_interval_seconds`，阿里云 ISI 同一 TaskId 会强制 ≥1s
- 千万注意：`workers` 调大后，多段可能同时命中同一家，账本记录是线程安全的，但额度可能被轻微超出

### 多进程与并发安全

- **账本**：每次写入都在 `ledger.lock` 的跨进程排他锁内完成「重新读盘 → 修改 → 原子替换」，
  多个进程同时跑不会互相覆盖；落盘用临时文件 + `os.replace`，中途被杀不会留下半截 JSON
- **账本读取**：不加锁，依赖原子替换；文件被其它进程改过时会按 mtime 自动重新加载
- **同一音频文件**：`<job>/job.lock` 独占锁，第二个进程会直接跳过该文件并提示
  「该文件正在被另一个进程处理」，因此不会重复提交、重复扣额度
- **记账时机**：只有 provider 返回成功后才记账。如果进程在「接口成功」与「记账落盘」之间被杀，
  该段时间会被少记（偏向保护你的额度，不会多扣）

---

## 10. 失败重试与断点续跑

- 每个音频文件一个 job：`<state_dir>/jobs/<hash>/job.json`，记录每段的切点、状态、结果
- 每段完成立刻落盘；进程被杀 / Ctrl-C 后重跑同一条命令，会跳过已完成段，只补未完成的
- 只有整份文件全部成功才会标记 `complete` 并清理片段；有失败段时保留断点，命令退出码为 `2`
- 一个文件失败不会中断其它文件；最后一个都没成功时不写空 JSON

---

## 11. 公网 URL 托管（阿里云 ISI / 百炼 / 听悟 / 火山必需）

这四家只接受公网可下载的 URL，不支持本地上传。脚本不做内网穿透，只做一件可预期的事：
把切好的片段复制到一个静态目录，并按前缀拼出 URL。

```yaml
hosting:
  mode: url_prefix
  url_prefix: https://files.example.com/asr   # 公网前缀
  local_root: /var/www/asr                    # 对应的本地目录（或对象存储同步目录）
```

- 没配 `hosting` 时，这四家会被自动跳过（打印原因），只剩讯飞 / 网易见外可用
- `url_prefix` 指向的 Web 服务或对象存储需要你自己准备；脚本不负责让 URL 真的可访问
- 片段文件名是 `seg_0000.mp3` 这类固定格式，任务成功后会删除本地静态副本

---

## 12. 命令行速查

```bash
python3 -m transcribe_router run <文件或目录...> [-o 输出目录] [--provider 名称] [--dry-run] [--no-srt] [--no-json] [--workers N]
python3 -m transcribe_router quota [--json]
python3 -m transcribe_router providers
python3 -m transcribe_router exhaust <provider> [--reason 原因]
python3 -m transcribe_router reset <provider>
```

- 不写子命令时默认按 `run` 处理，例如 `python3 -m transcribe_router ./audios`
- 全局参数 `--config` / `-v` / `-q` 可放在子命令前后
- `--dry-run` 用 Mock provider 跑通整条链路（不发任何真实请求），用于验证额度轮换与降级

---

## 13. 干跑验证结果

验证脚本：`scripts/verify_rotation.py`，不填任何真实 Key。

```bash
python3 scripts/verify_rotation.py
```

它用 `ffmpeg` 合成 3 组带静音间隔的测试音频，然后构造如下场景：

| 场景 | 配置的故障 | 期望行为 |
|---|---|---|
| A | ISI 鉴权失败；腾讯额度只有 9 秒 | ISI 被跳过 → 腾讯跑 2 段后额度耗尽 → 讯飞接手 |
| A | 讯飞首次调用网络抖动 | 原地重试 1 次后成功 |
| A | 百炼额度为 0；听悟、火山禁用 | 百炼因额度为 0 被跳过 |
| A | 只收 URL 的四家 + `hosting.url_prefix` | 都拿到可访问 URL，正常参与排序 |
| A | 运行时把 `HTTP(S)_PROXY` 指向死端口 | 干跑仍成功，证明 Mock 完全不发网络请求 |
| B | `--provider aliyun_isi` 且该家一直鉴权失败 | 全部段失败，退出码 2，不产出空 JSON，保留断点 |
| B | 修好配置后同一命令重跑 | 从断点续跑成功，产出 JSON/SRT |
| C | 4 个进程各并发写账本 20 次 | 累计用量精确等于 0.08 小时，无丢失 |
| C | 单月 30 号记账后跨到 1 号 0 点 | 本月用量自动清零、恢复满额 |
| C | `expires_at` 已过期 / 未来 | 剩余分别为 0 / 正常 |
| C | `expires_after_days: 90`，首次使用后第 90 天 | 未用时不计时；到期后剩余归零、状态「额度已过期」 |
| C | `expires_at` 与 `expires_after_days` 同时填 | 绝对日期优先 |
| C | 切点两侧「半句 + 整句」 | 合并为一条，保留完整文本、时间取并集 |
| C | 同一 job 并发加锁 | 第二次立刻被拒绝（JobBusy） |
| D | 唯一 provider 一直返回不可重试错误（4 段） | 连续失败 2 次后熔断，只调用 2 次，日志提示熔断 |
| E | tencent 的 `expires_at` 设为已过期 | 启动打印过期日期，跳过该家，降级到未过期 provider 并产出结果 |

实测结果（macOS / Python 3.14.7 / ffmpeg 8.1.2）：

```
通过 54 项，失败 0 项
全部验证通过。
```

覆盖的断言：退出码、JSON/SRT 产出、切段数量（20s / 6s → 4 段）、时间戳单调递增且不超总时长、
重叠区无重复句子、跨切点句子不丢失、半句/整句合并、SRT 时间轴格式、鉴权失败降级、切换提示、
额度耗尽、瞬时错误重试、连续失败熔断、零额度 provider 被跳过、账本跨进程持久化与 4 进程并发写、
跨月重置、绝对到期日判断、首次使用后 N 天到期推算与绝对日期优先级、过期 provider 自动跳过并打印原因、
job 独占锁、ffmpeg 缺失提示、`exhaust`/`reset` 生效、断点续跑、干跑零网络。

关键日志摘录：

```
当前使用 腾讯云 ASR（录音文件识别），剩余 4.4 秒 / 额度到期 长期（无固定到期日）
[切换] 腾讯云 ASR（录音文件识别） 不可用（额度耗尽）→ 当前使用 讯飞开放平台（语音转写），剩余 30 秒 / 额度到期 长期
```

验证产物在 `verify_out/`（每次运行会重建），`verify_out/run1.log` 是完整日志。

---

## 14. 免费额度预算（按 `config.example.yaml`）

> 本节数字全部按仓库里的 `config.example.yaml` 当前取值计算，不是按「理论上限」拍脑袋。
> 官方口径核对时间：2026-11-18。

### 14.1 百炼的官方口径（2026-11 核对）

来源：[百炼模型调用价格](https://help.aliyun.com/zh/model-studio/model-pricing)、
[新人免费额度有效期调整通知](https://help.aliyun.com/zh/model-studio/new-free-quota-validity-adjustment)、
[ISI 计量与计费](https://help.aliyun.com/zh/isi/developer-reference/metering-and-billing)。

| 模型 | 产品线 | 免费额度 | 刷新方式 | 期限起算 |
|---|---|---|---|---|
| `paraformer-v2` | 百炼 · 录音文件识别 | 36,000 秒（10 小时） | **每月 1 日 0 点自动发放** | 有效期 1 个月，不结转 |
| `paraformer-8k-v2` | 百炼 · 录音文件识别 | 独立额度（同为 10 小时档） | 每月 1 日 | 有效期 1 个月 |
| `fun-asr` | 百炼 · 录音文件识别 | 36,000 秒（10 小时） | **一次性** | 自开通百炼 / 模型发布 / 申请通过之日起 90 天内（以较晚者为准） |
| `paraformer-1` | **阿里云 ISI**（不是百炼） | 36,000 秒（10 小时）/月 | 每月 1 日 | 每月重置 |

结论：

- 之前配置把百炼写成「`paraformer-v2` 一次性 10 小时/90 天」是**错的**；`paraformer-v2` 是**每月 10 小时**，
  `fun-asr` 才是一次性 10 小时 / 90 天。两者是不同模型、各自独立的免费额度。
- 百炼的额度按**模型**区分，不按「录音文件识别 / 实时识别」这两种调用方式统一发放：
  录音文件识别是 `paraformer-v2` / `paraformer-8k-v2` / `fun-asr`，实时识别是 `*-realtime` 系列，
  模型不同则额度池不同，互不共享。
- 免费额度**仅华北2（北京）地域**可用；其他地域调用没有免费额度。
- 官方页面没有出现 `paraformer-1` 这个百炼模型名（`paraformer-1` 是 ISI 的模型名），
  因此本项目不把它计入百炼额度。

### 14.2 配置最终怎么定

| provider | `mode` | `hours` | 到期规则 | 依据 |
|---|---|---|---|---|
| `bailian` | `monthly` | 10 | 无（每月重置） | `paraformer-v2` 官方每月 10h |
| `aliyun_isi` | `daily` | 2 | `expires_after_days: 90` | 试用 3 个月，官方未给绝对日期 |
| `tingwu` | `daily` | 2 | `expires_after_days: 90` | 90 天试用，官方未给绝对日期 |
| 其余 4 家 | 不变 | 不变 | 不变 | 腾讯 10h/月；讯飞 5h、火山 20h、见外 5h 一次性 |

保守处理：仓库只按 `paraformer-v2` 一个模型建账，不把 `fun-asr` 的 10h 一次性额度加进来
（要加就得切模型并单独记账）。所以下面的数字是**偏保守**的。

### 14.3 首年总量与月均

假设：第 1 个月第 1 天开始用；每天把 ISI / 听悟的 2 小时用满；腾讯 / 百炼每月 10 小时用满；
一次性额度全部用完；不计算重试重复扣费。

| provider | 首年可用 | 折算月均 | 说明 |
|---|---:|---:|---|
| 腾讯云 ASR | 120 h | 10 h | 10h × 12 个月 |
| 阿里云 ISI | 180 h | 15 h | 2h × 90 天（仅首年一次） |
| 阿里云百炼 | 120 h | 10 h | 10h × 12 个月 |
| 通义听悟 | 180 h | 15 h | 2h × 90 天（仅首年一次） |
| 讯飞 | 5 h | 0.42 h | 一次性 |
| 火山豆包 | 20 h | 1.67 h | 一次性 |
| 网易见外 | 5 h | 0.42 h | 一次性 |
| **合计** | **630 h** | **52.5 h** | |

三个数：

- **长期稳定月均（第 4 个月起）：20 小时/月**（腾讯 10 + 百炼 10）
- **首年月均：52.5 小时/月**（630 ÷ 12）
- **首年总量：630 小时**

### 14.4 月度波动

| 月份 | 可用额度 | 说明 |
|---|---:|---|
| 第 1 月 | 170 h | 腾讯 10 + ISI 60 + 百炼 10 + 听悟 60 + 一次性 30 |
| 第 2 月 | 140 h | 腾讯 10 + ISI 60 + 百炼 10 + 听悟 60 |
| 第 3 月 | 140 h | 同上（第 90 天结束） |
| 第 4–12 月 | 20 h/月 | 只剩腾讯 + 百炼的每月额度 |

前 3 个月合计 450 h；之后每月 20 h。第 90 天具体落在哪个月取决于你第一次成功转写的日期
（`expires_after_days` 从首次使用起算，不是自然月）。

### 14.5 实际可用性折扣

| 约束 | 影响 | 依据 |
|---|---|---|
| 只收公网 URL | 500 h / 630 h（**79.4%**）必须配 `hosting`；不配 `hosting` 时首年只剩 **130 h**（腾讯 120 + 讯飞 5 + 见外 5），约 20.6% | 代码里 ISI / 百炼 / 听悟 / 火山均 `requires_url: true` |
| 单文件上限 | 正常录音（≤4 小时）无损失；更长会切段，每段重叠 0.5 秒，额外计费可忽略 | 见 `limits.max_file_seconds` |
| 每天用满 2 小时 | 若只在工作日用，ISI / 听悟按 90 天里约 64 个工作日估算，两者各约 128 h，首年总量约 **530 h**（估算值，非官方数字） | 假设 5 天/8 天为工作日 |
| 并发 / QPS | 只影响跑完要多久，不减少额度；ISI 同 TaskId 1 QPS、百炼/听悟默认 1 | `limits.qps` |
| 重试重复消耗 | 失败重试是否重复计费**未验证**，本节不做折算 | 无公开说明 |
| 天级额度不累积 | ISI / 听悟当天没用完的 2 小时不结转到第二天 | 官方试用规则 |

> 如果改用 `fun-asr`：百炼从「120 h/年」变成「10 h 一次性」，首年总量降至 520 h，
> 月均降至 43.3 h；此时把 `bailian.quota.mode` 改成 `fixed` 并加 `expires_after_days: 90`。

---

## 15. 未验证事项（重要）

以下内容本次没有可用账号做真实联调，代码里对应位置有 `TODO(未验证)` 注释。
接入前请先用少量音频试跑，确认后再批量跑。

1. **通义听悟的 HTTP 签名**：官方页面明确写「自签名细节非常复杂，建议在专家指导下对接」，
   未公开完整签名示例。当前按阿里云 OpenAPI V3（`ACS3-HMAC-SHA256`）通用规则实现，
   该 provider 标记为实验性（`providers` 命令里会显示 `[实验性]`）。
   若签名不匹配会返回 4xx，路由层会自动降级，不会拖垮整批任务；建议先用一段短音频单独试跑
   `--provider tingwu` 确认可用后再纳入轮换。
2. **通义听悟转写结果 JSON 的内部字段**：官方只给到 `Data.Result.Transcription` 是一个 URL。
   代码兼容了 `Paragraphs[].Words[]`（Text/Start/End）与 `sentences[]`（begin_time/end_time）两种结构，
   但哪一种才是当前线上格式未实测。
3. **腾讯云「免费额度耗尽」的错误码**：官方错误码表没有稳定对应该场景的 Code，
   代码按 `额度/欠费/余额/资源包/免费` 等文案兜底识别。
4. **腾讯云 `DescribeTaskStatus` 的 `ResultDetail` 字段**：按官方示例用 `FinalSentence` / `StartMs` / `EndMs` / `SpeakerId` 解析。
5. **阿里云 ISI 的 QPS 具体表现**：文档给的是 `SubmitTask` 200 / `GetTaskResult` 500 / 同 TaskId 1，
   本地限速按 TaskId 场景取 1 QPS，实际高并发下是否触发 `Throttling.User` 未压测。
6. **百炼 / 火山的 QPS 上限**：官方未给固定数值，`limits.qps` 默认 1，需要按你的账号实测调整。
7. **百炼 `paraformer-v2` 的请求字段**：模型名、每月 10 小时免费额度已按官方定价页核对（见第 14 节），
   但录音文件识别的 HTTPS 请求/回包字段未做真实联调；免费额度仅华北2（北京）地域有效。
8. **通义听悟单文件时长/大小上限**：官方 API 页未写明，默认按 12 小时 / 2GB 配置。
9. **各家免费额度数值**：百炼与 ISI 已按官方页面核对；腾讯、听悟、讯飞、火山、见外的数值来自
   2026-11-17 的报告。平台随时可能调整，脚本不自动探测，请以控制台为准并修改 `quota.hours`。
10. **双声道计费**：阿里云 ISI 文档提到 16kHz 双声道按「声道数×时长」计费，
    本项目统一转单声道后切段，正常情况下不会触发；如你手工接入双声道音频，账本会按单倍时长记账。
11. **网易见外与有道智云的关系**：见外价格页写的是「API 长语音识别」，其公开接口文档实际在
    有道智云（`openapi.youdao.com/api/audio/*`）。本项目按有道智云文档实现，二者是否完全同一额度池未验证。

---

## 16. 安全与边界

- **密钥不落盘**：代码与配置里只有 `${环境变量}` 占位，真实值放 `.env`；`.gitignore` 已忽略 `.env`、`config.yaml`、`.tr_state/`（账本所在目录）、`verify_out/`
- 日志不打印密钥；`providers` 命令只显示「已配置 / 缺少 xxx」；干跑验证里有一条断言检查日志中不出现凭据字样
- **无命令注入**：调用 `ffmpeg/ffprobe` 一律使用参数数组（不经过 shell），输入路径先 `resolve()` 成绝对路径
- **临时文件**：账本用「同目录临时文件 + `os.replace`」原子落盘；切段音频、静态托管副本在任务成功后清理
- 只做云端 API 调用，**不实现本地模型推理**
- **不包含**账号开通、实名认证、绑卡、对象存储配置等操作指引

---

## 17. 目录结构

```
outputs/
├── config.example.yaml          # 配置模板（7 家全在里面）
├── .env.example                 # 环境变量模板
├── requirements.txt             # 可选依赖（PyYAML）
├── transcribe_router/
│   ├── cli.py                   # 命令行入口：run / quota / exhaust / reset / providers
│   ├── config.py                # 配置加载、内置 YAML 子集解析、环境变量展开
│   ├── ledger.py                # 额度账本（按月/按日/一次性，持久化）
│   ├── router.py                # 排序选路 + 降级 + 切换提示
│   ├── pipeline.py              # 切段 → 转写 → 合并 → 输出 → 断点续跑
│   ├── audio.py                 # ffmpeg 探测/静音检测/切段
│   ├── srt.py                   # 句级时间戳 → SRT
│   ├── store.py                 # job 持久化
│   ├── hosting.py               # 公网 URL 托管
│   ├── utils.py                 # 日志、时间格式、标准库 HTTP
│   └── providers/
│       ├── base.py              # Provider 基类、限流器、数据结构
│       ├── tencent.py           # TC3 签名 + CreateRecTask/DescribeTaskStatus
│       ├── aliyun_isi.py        # POP RPC 签名 + SubmitTask/GetTaskResult
│       ├── bailian.py           # DashScope 异步任务 + transcription_url
│       ├── tingwu.py            # 听悟 CreateTask/GetTaskInfo（实验性）
│       ├── iflytek.py           # prepare/upload/merge/getProgress/getResult
│       ├── volcano.py           # AUC submit/query
│       ├── netease.py           # 有道 prepare/upload/merge/get_progress/get_result
│       └── mock.py              # 干跑用 Mock
└── scripts/verify_rotation.py   # 干跑验证脚本
```
