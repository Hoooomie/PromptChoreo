# PromptChoreo

按时序自动往流式视频生成网站投喂 prompt，并自动录制/裁剪成片。

支持站点：`odyssey`（默认）、`happy_oyster`、`pixverse`。

---

## 1. 安装

```bash
pip install -e .
playwright install chromium
```

> 建议在项目专用 venv 中运行。以下所有命令中 `python` 均指该 venv 的 Python。

---

## 2. 登录并保存用户态（只需做一次）

Happy Oyster 和 PixVerse 需要先手动登录，登录态会持久化保存到本地目录，之后 `promptchoreo run` 自动复用，不用每次登录。

**Happy Oyster**
```bash
python scripts/login_happyoyster.py
```
脚本打开浏览器 → 你在里面手动登录 → 回到终端按回车关闭。登录态存到
`~/.workbuddy/browser_data`。

**PixVerse**
```bash
python scripts/login_pixverse.py
```
同上，登录态存到 `~/.workbuddy/browser_data_pixverse`（与 Happy Oyster 分开，避免混用）。

**Odyssey**：无需登录，可跳过本步。

### 2.1 凭据配置（仅 PixVerse 需要）

PixVerse 在生成过程中如果未登录，会自动用邮箱和密码填写登录表单。
这些凭据存放在项目根目录的 **`.credentials.yaml`**（已通过 `.gitignore` 排除，不会提交到 Git）。

**首次配置：**

```bash
# 复制模板
copy .credentials.example.yaml .credentials.yaml
# 编辑 .credentials.yaml，把 your-email / your-password 换成真实凭据
```

模板内容（`copy` 后看到的 `.credentials.yaml`）：

```yaml
pixverse:
  email: "your-email@example.com"
  password: "your-password"
```

配置完成后，`run` / `batch` 会自动读取。如果文件不存在或字段为空，PixVerse 自动登录会失败（Happy Oyster / Odyssey 不受影响）。

> **说明**：这是 PixVerse 自动表单填写的凭据，与第 2 节通过 `login_*.py` 手动登录保存的**浏览器 cookie 登录态**是两套独立机制；两者通常都需要配置。

---

## 3. 写时间轴

时间轴是一个 YAML 文件，定义「用什么初始 prompt 开跑 + 在哪些时间点注入什么指令」。

```yaml
# timeline.yaml
initial_prompt: "一只橘猫坐在窗台上，夕阳洒在毛发上"
end_delay: 15            # 最后一条指令注入后，再等多少秒停止录制

# 外部录屏（EV录屏等）。不需要录制就整段删掉。
recorder:
  enabled: true
  start_hotkey: "ctrl+f1"   # EV 开始录制热键
  stop_hotkey: "ctrl+f2"    # EV 停止录制热键

events:
  - time: 10               # 第 10 秒注入（相对于生成开始 00:00）
    prompt: "猫站起身，伸了个懒腰"
    label: "动作"
  - time: 25
    prompt: "猫轻巧地跳上屋顶，月光下的剪影"
    label: "转场"
```

- `time`：注入时刻（秒），相对于生成真正开始的 00:00，控制误差 ≤ 2 秒。
- `time: 0` 不注入任何东西（初始 prompt 已在开跑时投出）。
- `recorder` 不写或 `enabled: false` 则不触外部录屏，但浏览器内置录制照常。

---

## 4. 运行

```bash
# 默认站点 odyssey
promptchoreo run timeline.yaml

# 指定站点
promptchoreo run timeline.yaml --site happy_oyster
promptchoreo run timeline.yaml --site pixverse

# 预览时间轴（只打印，不执行）
promptchoreo dry-run timeline.yaml
```

常用选项：

| 选项 | 说明 |
|------|------|
| `--site` / `-s` | 站点适配器，默认 `odyssey` |
| `--config` / `-c` | 站点配置文件（YAML），可放 `recorder` 等配置 |
| `--headless` | 无头模式（默认有头，能看到浏览器操作） |
| `--slow-mo N` | 每步操作间隔 N 毫秒，便于观察 |
| `--record-dir` | 视频保存目录（默认按站点：`outputs/video/ho` / `video/od` / `video/pv`） |
| `--no-record` | 禁用录制 |
| `--cdp` | 连接已手动打开的 Chrome for Testing（CDP 地址，如 `http://127.0.0.1:9222`）。见下方「连接模式」 |

---

### 4.1 连接模式：手动打开 Chrome for Testing（推荐追求分辨率 / 锁定窗口）

默认模式工具自己用 `--kiosk` 开全屏浏览器。如果你希望**自己控制窗口**（比如让 EV 录屏只锁定 Chrome for Testing 这一个窗口、手动按 F11 全屏），可以用连接模式：

工具不自己启动浏览器，而是连上你手动开好的 Chrome for Testing。流程：

```bash
# 1) 用脚本一键启动 Chrome for Testing（保持窗口打开，别关）
#    --site 决定用哪个 user-data-dir（登录态），默认 happy_oyster
python scripts/launch_chrome_for_testing.py --site happy_oyster
#    pixverse 用:  python scripts/launch_chrome_for_testing.py --site pixverse
#    odyssey 用:   python scripts/launch_chrome_for_testing.py --site odyssey

# 2) 手动按 F11 把窗口全屏（EV 录屏分辨率更高）

# 3) 在 EV 录屏里把录制目标设为「Google Chrome for Testing」这个窗口

# 4) 运行（加 --cdp 指向上面打印的 CDP 地址）
promptchoreo run examples/timeline_happy_oyster.yaml --site happy_oyster --cdp http://127.0.0.1:9222
```

连接模式要点：

- 全屏 / 窗口大小由你手动控制，工具不再强制 `--kiosk`，也不会在结束时退出你的全屏。
- **声音默认开启**（启动脚本默认**不带** `--mute-audio`）。工具只通过**点击生成界面的 🎵 音乐符号**关闭配乐（背景音乐），**保留视频原声**——这样成片能录到视频本身的声音、不含配乐。**如果你确实想要彻底静音**，手动启动时加 `--mute`（如 `python scripts/launch_chrome_for_testing.py --site xxx --mute`），或在 `run`/`batch` 命令后加 `--mute`；改了启动脚本后必须重启 Chrome for Testing 才会生效。
- **录屏只走外部 EV**（不再写 Playwright 内置原始视频，因为 CDP 模式下内置录制不可用）。EV 把成片存到你设的输出目录，裁剪时把 `trim_*.py` 的 `--input` 指过去即可（见第 5 节）。
- `launch_chrome_for_testing.py` 用的就是 Playwright 自带的 Chrome for Testing，窗口标题是「Google Chrome for Testing」，EV 才好锁定。
- 同一时间**只能有一个进程占用 user-data-dir**：连好之后就别再用普通 `promptchoreo run`（不带 `--cdp`）去开同一个站点，否则会报「目录被占用」。
- **可连续跑多个 yaml**：跑完一个 yaml 后，工具会自动点生成画面右上角的 X 把站点复位到输入框界面，标签页保持打开。下一个 `promptchoreo run --cdp ...` 会复用这同一个标签页直接重开会话，无需重启浏览器——对 Odyssey 这种有时长限制的站点尤其省事。

### 4.2 批量模式：一个清单文件 = 多个视频

不想一条条手敲 `run`，可以把多个视频写进**一个清单文件**（`.json` 或 `.yaml` 都行），用 `batch` 一次性顺序跑完。每个视频自带 `site`，所以一个清单里可以混跑三个站点。

清单结构（`examples/manifest.json` 即范例）：

```json
{
  "site": "odyssey",
  "videos": [
    {
      "name": "片段1",
      "site": "odyssey",
      "initial_prompt": "A cat on a rooftop at sunset",
      "end_delay": 10,
      "recorder": { "enabled": true, "start_hotkey": "ctrl+f1", "stop_hotkey": "ctrl+f2" },
      "events": [
        { "time": 10, "prompt": "The cat starts walking", "label": "walk" },
        { "time": 20, "prompt": "Add a bird flying by", "label": "bird" }
      ]
    },
    {
      "name": "片段2",
      "site": "happy_oyster",
      "initial_prompt": "A dog is running.",
      "events": [ { "time": 10, "prompt": "The dog jumps." } ]
    }
  ]
}
```

- `site`：清单顶层默认值，每个 video 也可用 `site` 字段覆盖（如上片段2 是 happy_oyster）。
- 每个 video 的字段与单个时间轴一致：`initial_prompt` / `end_delay` / `events` / `recorder` / `load_wait`（可选）/`max_load_wait`（可选，生成加载等待上限秒数，Happy Oyster 加载慢默认 600，不够再加大）。
- 清单顶层也可写 `max_load_wait` 作为所有 video 的默认值。
- `name`：日志标识，缺省自动编为 `video1` / `video2` …

跑法（推荐连接模式，所有视频共用一个浏览器标签页，跑完自动复位）：

```bash
# 先开浏览器（连模式只开一个即可，站点切换由工具自动导航）
python scripts/launch_chrome_for_testing.py --site odyssey
# F11 全屏 → EV 锁窗口
promptchoreo batch examples/manifest.json --cdp http://127.0.0.1:9222
```

不带 `--cdp` 也能跑（每个视频各自起/关浏览器，较慢）。`--site` / `--config` / `--resolution` / `--slow-mo` 等参数同样适用，作为清单里未指定项的兜底。

加载很慢（如 Happy Oyster 有时要 3 分钟以上）时，用 `--max-load-wait 900`（秒）放大上限，或在清单/video 里写 `max_load_wait`；工具会打印 `加载中... 89%（等待生成开始）` 让你确认它在加载而非卡死。

---

## 5. 录制与成片

运行时（默认模式）浏览器会自动 **kiosk 全屏**（最高清）。**默认不清浏览器层声音**：视频原声会保留，配乐由工具点击生成界面的 🎵 关闭。
想彻底静音就在 `run`/`batch` 后加 `--mute`。
**连接模式**（`--cdp`）下窗口由你手动全屏、内置录制关闭，只走外部 EV。

- **内置录制**（仅默认模式）：Playwright 录下整段时间轴的视频，按站点存到对应原始目录（都在 `outputs/video/` 下）：
  - Happy Oyster → `outputs/video/ho/`
  - Odyssey → `outputs/video/od/`
  - PixVerse → `outputs/video/pv/`
  - 可用 `--record-dir` 覆盖。
- **外部录制（EV录屏）**：在时间轴写 `recorder` 块即可，工具在视频真正开始播放的那一帧发开始热键、在点暂停后发停止热键。
  - 请先把 EV录屏设为「收到信号即录」，热键与 YAML 里的 `start_hotkey` / `stop_hotkey` 对应（默认 `ctrl+f1` / `ctrl+f2`）。
  - EV 的启动热键建议用 `keyboard` 库能发的组合（SendInput），不要用只在窗口有焦点才生效的方式。

**裁剪成片（独立脚本，按站点分开）**：内置录制是未裁剪的原片，裁剪交给三个独立脚本各自处理：

```bash
python scripts/trim_happyoyster.py   # 读 outputs/video/ho  ->  outputs/tvideo/ho
python scripts/trim_odyssey.py       # 读 outputs/video/od  ->  outputs/tvideo/od
python scripts/trim_pixverse.py      # 读 outputs/video/pv  ->  outputs/tvideo/pv
```

- 每个脚本自动用 `cropdetect` 检测内容包围盒并裁掉非视频区域；各站裁剪参数独立（Odyssey 画布充满时直接透传）。
- 测一次确认裁剪区正确后，可用 `--crop W:H:X:Y` 锁定固定裁剪区（最稳）；可用 `--ss` / `--to` 做时间裁剪（秒）。
- 原片保留在 `video/<site>` 目录，裁剪结果在 `tvideo/<site>` 目录。

---

## 小贴士

- 登录态过期 → 重跑对应 `login_*.py` 脚本重新登录即可。
- 手动登录必须走 `login_*.py` 脚本（kiosk 模式没有地址栏，没法手动输网址）。
- 想临时看慢动作排错：`promptchoreo run timeline.yaml --slow-mo 500`。
