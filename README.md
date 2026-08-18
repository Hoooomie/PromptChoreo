# PromptChoreo

按时序自动往流式视频生成网站投喂 prompt，并自动录制/裁剪成片。

支持站点：`odyssey` `happy_oyster` `pixverse`

---

## 1. 安装

```bash
pip install -e .
playwright install chromium
```

> 建议在项目专用 venv 中运行。以下命令中 `python` 均指该 venv 的 Python。

---

## 2. 登录

Happy Oyster 和 PixVerse 需要登录。**Odyssey 无需登录**，可跳过本步。

### 2.1 浏览器登录态

```bash
python scripts/login_happyoyster_global.py  # 仅用于国际站手工调试
python scripts/login_pixverse.py      # 登录态 → ~/.workbuddy/browser_data_pixverse
```

Happy Oyster 批处理不复用手工登录账号。请复制
`examples/happyoyster_accounts.example.json` 为 `happyoyster_accounts.json`，
按顺序填写邮箱和密码。每个账号最多成功生成一个视频；每生成
一个视频后脚本会退出登录，并切换到下一个账号。
账号文件和使用状态文件均已
加入 `.gitignore`。

国际站登录采用人工监督模式：用户负责从 Happy Oyster 页面进入 Google
登录页，脚本只负责填写当前账号池中的邮箱和密码。邮箱与密码步骤均执行
“等待 2 秒 → 输入 → 等待 2 秒 → 下一步”；Google 出现图片验证时由用户
手工完成，脚本会继续等待而不会消耗账号。登录失败的账号可在
`.happyoyster_account_usage.json` 中标记为耗尽，后续自动领取下一个账号。

PixVerse 登录脚本会打开浏览器；手动登录后回到终端按回车保存登录态。

PixVerse World 必须从 `world.pixverse.video/generate/` 页面自己的 **Log in**
入口登录；`app.pixverse.ai/login` 是另一套产品界面。

---

## 3. 写时间轴

```yaml
# timeline.yaml
initial_prompt: "一只橘猫坐在窗台上，夕阳洒在毛发上"
end_delay: 15               # 最后一条注入后继续录制多少秒

recorder:                   # 外部录屏（EV 等）。不用就整段删掉
  enabled: true
  start_hotkey: "ctrl+f1"
  stop_hotkey: "ctrl+f2"

events:
  - time: 10                # 相对录制起点的秒数
    prompt: "猫站起身，伸了个懒腰"
  - time: 25
    prompt: "猫跳上屋顶，月光下的剪影"
```

- `time`：注入时刻（秒），**相对于录制起点**，误差 ≤ 1 秒
- `recorder` 不写则不触发外部录屏
- 也支持 JSON 清单（见 4.2 批量模式）

---

## 4. 运行

```bash
promptchoreo run timeline.yaml                # 默认 odyssey
promptchoreo run timeline.yaml --site pixverse
promptchoreo dry-run timeline.yaml            # 预览不执行
```

| 选项 | 说明 |
|------|------|
| `--site` / `-s` | 站点适配器（`odyssey` / `happy_oyster` / `pixverse`） |
| `--cdp` | 连接手动打开的 Chrome，如 `--cdp http://127.0.0.1:9222` |
| `--mute` | 浏览器级彻底静音（默认不静音，配乐通过界面 🎵 关闭） |
| `--max-load-wait N` | 加载等待上限（秒），Happy Oyster 慢时调大，默认 600 |
| `--slow-mo N` | 操作间隔 N 毫秒，便于观察 |
| `--headless` | 无头模式 |
| `--config` / `-c` | 站点配置文件（YAML） |

### 4.1 连接模式（推荐）

手动开 Chrome for Testing → 工具 CDP 连上 → 复用标签页跑所有视频：

```bash
# 1) 开浏览器（保持窗口不关）
python scripts/launch_chrome_for_testing.py --site pixverse

# 2) F11 全屏 → EV 锁定窗口「Google Chrome for Testing」

# 3) 跑（同站清单无需 --site）
promptchoreo batch examples/manifest_pixverse.json --cdp http://127.0.0.1:9222
```

要点：
- 窗口全屏你手动控制，工具不强制 kiosk
- 声音默认开启，工具点界面 🎵 关配乐、保留视频原声；彻底静音加 `--mute`
- 连接模式只走外部 EV 录屏，不含 Playwright 内置录制
- 跑完自动复位到输入框，下一视频复用同一标签页

### 4.2 批量模式（一个清单 = 多个视频）

```json
{
  "site": "pixverse",
  "videos": [
    {
      "name": "猫",
      "initial_prompt": "A cat on a rooftop at sunset",
      "end_delay": 15,
      "max_load_wait": 900,
      "recorder": { "enabled": true, "start_hotkey": "ctrl+f1", "stop_hotkey": "ctrl+f2" },
      "events": [
        { "time": 10, "prompt": "The cat starts walking" },
        { "time": 20, "prompt": "A bird flies by" }
      ]
    }
  ]
}
```

- `site` 顶层默认值，每个 video 可单独覆盖（**连接模式下清单只能含一个站点**）
- 字段同单个时间轴，额外支持 `max_load_wait`
- 示例清单：`examples/manifest_pixverse.json` / `manifest.json`

---

## 5. StreamAVBench 批量运行

```bash
# Pilot（默认；Happy Oyster 统一使用国际站和 180s）
python scripts/bench_runner_happyoyster.py --phase pilot --180

# Test（从 pilot 取 3 条，Track B 优先）
python scripts/bench_runner_happyoyster.py --phase test --180
python scripts/bench_runner_odyssey.py --phase pilot
python scripts/bench_runner.py --phase pilot          # PixVerse

# 新数据集（new_bench/*.json 更新后，先重新生成 YAML）
python scripts/new_bench_prep.py

# Progressive：160 条，每条只提交 initial prompt，连续生成 180s
python scripts/bench_runner_happyoyster.py --phase progressive --180
python scripts/bench_runner.py --phase progressive --180       # PixVerse

# Interactive：160 条，每条生成 180s，在 30/60/90/120/150s 注入 Prompt
python scripts/bench_runner_happyoyster.py --phase interactive --180
python scripts/bench_runner.py --phase interactive --180       # PixVerse

# 一次运行上述两组新数据（共 320 条；默认按打乱后的同编号 I、P 配对运行）
python scripts/bench_runner_happyoyster.py --phase new --180
python scripts/bench_runner.py --phase new --180                # PixVerse

# 如需换一套可复现的乱序，只需指定另一个整数种子
python scripts/bench_runner_happyoyster.py --phase new --180 --shuffle-seed 42
python scripts/bench_runner.py --phase new --180 --shuffle-seed 42

# Remain
python scripts/bench_runner_happyoyster.py --phase remain --180
python scripts/bench_runner_odyssey.py --phase remain
python scripts/bench_runner.py --phase remain

# 其他站点仍可按旧数据集时长筛选
python scripts/bench_runner_odyssey.py --phase remain --120
python scripts/bench_runner_odyssey.py --phase remain --30+60  # Odyssey 同时运行 30s 和 60s
python scripts/bench_runner.py --phase remain --120

# 单个 job（使用 YAML 文件名格式）
python scripts/bench_runner_happyoyster.py --phase remain --180 --job EXAMPLE_JOB_SPLIT
python scripts/bench_runner.py --phase remain --job EXAMPLE_JOB_SPLIT

# 精确指定多条并强制重跑（旧结果自动归档到 attempt_*）
python scripts/bench_runner_happyoyster.py --phase new --180 --jobs I-0001_I-180 P-0001_P-180 --force-rerun
python scripts/bench_runner.py --phase new --180 --jobs I-0001_I-180 P-0001_P-180 --force-rerun
```

成功 job 会自动跳过。结果分别保存在：

```text
outputs/happyoyster_global/<phase>/
outputs/odyssey/<phase>/
outputs/pixverse_r1/<phase>/
```

旧任务来源为 `StreamAVBench_closed_source_web_package/.../*_jobs.json`。新任务来源为
`new_bench/progressive.json` 和 `new_bench/interactive.json`，由
`scripts/new_bench_prep.py` 转换；实际 prompt 配置统一位于 `bench_yamls/`。
新数据集使用固定随机种子打乱难度顺序。`new` phase 会按
`I-同编号、P-同编号` 交替运行，并将准确顺序写入
`outputs/<model>/run_lists/`。PixVerse 继续复用
`browser_data_pixverse` 的单一登录态，不使用 Happy Oyster 的账号轮换机制。

### 5.1 Happy Oyster 国际站运行规则

- 所有任务统一生成 180 秒视频。
- Progressive（原 Track A）只提交 initial prompt，中间不注入。
- Interactive（原 Track B）在录屏开始后的 30、60、90、120、150 秒注入。
- 外部录屏仅在屏幕中可见的 `REC` 计时器开始递增后启动；所有注入偏移均以
  外部录屏的 monotonic 零点为准，不使用页面计时器调度。
- 每个账号只用于一个成功视频；任务结束后自动退出，并换用下一个账号。
- 首页或 Directing 页面发生暂时网络断开时自动重试，不会因一次
  `ERR_CONNECTION_CLOSED` 立即终止。
- 检测到 `Oops: Something went wrong` 或
  `This scene can't be played right now` 时，立即停止当前录屏、写入失败原因和
  `skip_job.json`，并将失败画面保存为 `error_recording.mp4`。如果错误在正常录屏
  启动前出现，会额外短录制错误画面作为证据。退出当前账号后继续下一个任务，
  不中断整批。重启时会同时
  扫描当前 manifest 和历史 `attempt_*`，旧格式 Oops 失败也不会再次执行。
- 最终视频保留完整录屏画面，不做空间裁剪。允许浏览器客户区边框造成的少量
  分辨率差异（每条边最多 32 像素），例如 `2544x1432`。

每个任务的输出目录格式保持一致：

```text
outputs/happyoyster_global/<phase>/<job_id>/
├── final_video.mp4       # 成功结果
├── error_recording.mp4   # 失败时的完整/短录屏证据
├── run_manifest.json     # 状态、时间、分辨率和失败原因
├── prompt_events.jsonl   # initial prompt 与注入提交时间
├── chunk_events.jsonl    # 原生 chunk 可观察性记录
├── chunks/
├── attempt_*/            # 被重试前归档的历史结果
└── skip_job.json         # 不可重试失败；存在时后续直接跳过
```

---

## 6. 裁剪工具

Benchmark runner 会自行完成视频整理，不要对其 `final_video.mp4` 再次裁剪。
下面脚本只用于手工处理 `outputs/video/` 中的原始录屏：

```bash
python scripts/trim_happyoyster.py    # outputs/video/ho → outputs/tvideo/ho
python scripts/trim_odyssey.py        # outputs/video/od → outputs/tvideo/od
python scripts/trim_pixverse.py       # outputs/video/pv → outputs/tvideo/pv
```

Happy Oyster benchmark 保留完整 `2560x1440` 画面，不做空间裁剪。
Odyssey/PixVerse 的手工裁剪参数可在脚本顶部调整，或直接传入：

```bash
python scripts/trim_odyssey.py --crop 940:522:810:434
python scripts/trim_pixverse.py --ss 1 --to 120   # 时间裁剪
```

---

## 7. 下载网站生成的视频

各网站使用独立下载脚本。Happy Oyster 下载器读取“我的视频”的分页数据，
优先使用站点官方下载，失败时回退到作品的 CDN 成片：

```bash
python scripts/download_ho_videos.py --dry-run          # 只查看待下载文件
python scripts/download_ho_videos.py                    # 下载尚未下载的作品
python scripts/download_ho_videos.py --date 2026.07.26  # 按日期筛选
python scripts/download_ho_videos.py --source direct    # 跳过按钮，直接下载 CDN 成片
python scripts/download_ho_videos.py --site global      # 国际站历史账户
```

国内站默认输出到 `outputs/downloads/happyoyster/`，下载记录保存在
`.downloaded_ho.json`。登录态失效时运行 `python scripts/login_happyoyster.py`；
国际站使用 `python scripts/login_happyoyster_global.py`。

PixVerse 下载器读取 Mine 的作品列表数据，不依赖页面坐标或 Download 按钮：

```bash
python scripts/download_pv_videos.py --dry-run          # 只查看待下载文件
python scripts/download_pv_videos.py                    # 下载每个 World 的主视频
python scripts/download_pv_videos.py --date 2026.07.19  # 按日期筛选
python scripts/download_pv_videos.py --all-sessions     # 同时下载全部 session 视频
```

默认输出到 `outputs/downloads/pixverse/`，下载记录保存在
`.downloaded_pv.json`。如果提示登录态失效，先重新运行
`python scripts/login_pixverse.py`。

---

## 小贴士

- 登录态过期 → 重跑对应 `login_*.py`
- PixVerse 必须选 **Story 模式**（工具自动选，偶有模式错位看日志 `[DEBUG] 模式已确认为 Story`）
- 排错用 `--slow-mo 500` 慢放观察每一步
- 加载慢不卡死：工具会打 `加载中... 89%` 确认在跑，等满 `max_load_wait` 才报错
