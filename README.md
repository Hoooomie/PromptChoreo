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

## 2. 登录（只需做一次）

Happy Oyster 和 PixVerse 需要登录。**Odyssey 无需登录**，可跳过本步。

### 2.1 浏览器登录态

```bash
python scripts/login_happyoyster.py   # 登录态 → ~/.workbuddy/browser_data
python scripts/login_pixverse.py      # 登录态 → ~/.workbuddy/browser_data_pixverse
```

脚本打开浏览器 → 手动登录 → 终端按回车关闭。之后 `run` / `batch` 自动复用。

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
# Pilot（默认）
python scripts/bench_runner_happyoyster.py --phase pilot
python scripts/bench_runner_odyssey.py --phase pilot
python scripts/bench_runner.py --phase pilot          # PixVerse

# Remain
python scripts/bench_runner_happyoyster.py --phase remain
python scripts/bench_runner_odyssey.py --phase remain
python scripts/bench_runner.py --phase remain

# Remain 中全部 120 秒任务（160 个，仍写入原 remain 目录）
python scripts/bench_runner_happyoyster.py --phase remain --120
python scripts/bench_runner_odyssey.py --phase remain --120
python scripts/bench_runner.py --phase remain --120

# 单个 job（使用 YAML 文件名格式）
python scripts/bench_runner.py --phase remain --job EXAMPLE_JOB_SPLIT
```

成功 job 会自动跳过。结果分别保存在：

```text
outputs/happyoyster/<phase>/
outputs/odyssey/<phase>/
outputs/pixverse_r1/<phase>/
```

任务来源为 `StreamAVBench_closed_source_web_package/.../*_jobs.json`，实际 prompt
配置位于 `bench_yamls/`。

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

## 小贴士

- 登录态过期 → 重跑对应 `login_*.py`
- PixVerse 必须选 **Story 模式**（工具自动选，偶有模式错位看日志 `[DEBUG] 模式已确认为 Story`）
- 排错用 `--slow-mo 500` 慢放观察每一步
- 加载慢不卡死：工具会打 `加载中... 89%` 确认在跑，等满 `max_load_wait` 才报错
