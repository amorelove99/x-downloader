# X Downloader

[中文](README.md) | [English](README.en.md)

把 X 收藏中的图片和视频保存到自己的电脑，之后只补下载新增和失败的内容。

收藏夹适合随手保存，回头整理却要逐条打开、下载、重命名。X Downloader 面向已经积累了媒体收藏的人：用一次登录的浏览器会话采集收藏，批量保存原图和视频，并记住处理进度。文件默认留在本地；需要集中存储时，再开启 NAS 复制。

**目前面向 macOS，菜单和运行提示主要为中文。** 需要 ego lite 提供的 `ego-browser`，不支持直接使用 Safari 或普通 Chrome 会话。日常运行不需要大模型，也不需要配置 X 开发者 API Key。

## 适合哪些场景

- 离线回看收藏的教程、演示视频和图片资料。
- 定期整理新增收藏，避免每次手动重复下载。
- 先保存到笔记本，回到家再把文件补交到 SMB NAS。
- 在自己的电脑上按日或每隔几天运行，查看日志并重试失败项。

它保存媒体文件，不保存完整推文正文，也不是完整的 X 账号备份工具。没有下载成功的内容，无法在原帖删除后恢复。

## 已有功能

| 需求 | 程序行为 |
| --- | --- |
| 一条收藏含多张图或多个视频 | 保留采集到的多个媒体，图片请求原图，视频默认选择最高 1080p |
| 重复运行 | 根据本地状态跳过已处理项，失败项保留重试 |
| 下载中断 | 保留已完成文件；视频由 yt-dlp 续传，未完成图片重新下载 |
| 先看清单再下载 | 预览模式采集清单，经确认后执行 |
| 不想一直开着终端 | 可后台运行，也可使用 macOS launchd 定时 |
| 多个 X 账号 | 每次采集确认当前账号，失败队列按账号区分；同一媒体记录可跨账号复用 |
| 可选 NAS | 按月份复制，SHA-256 校验后才标记已交付；本地文件继续保留 |
| 可选手机提醒 | 通过 Bark 发送任务失败通知，默认关闭 |

新收藏的文件名带递增序号、作者和推文标识；已有文件保留原名。进度分别显示收藏数与媒体文件数。

## 安装与首次运行

准备好以下环境：

- macOS、Python 3.9 或更新版本。
- [ego lite](https://lite.ego.app/)，完成首次设置，确保终端能运行 `ego-browser`，并在该浏览器中手动登录 X。
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) 和 [FFmpeg](https://ffmpeg.org/)。使用 Homebrew 时可运行 `brew install python yt-dlp ffmpeg`。

浏览器登录、验证码由你手动完成。程序使用浏览器当前会话，不要求导出 Cookie 文件。

```bash
git clone https://github.com/amorelove99/x-downloader.git
cd x-downloader
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/xdl doctor
.venv/bin/xdl menu
```

也可以在 Finder 双击 `启动 X Download.command`。首次选择「1 安装并继续」，它会建立项目内的虚拟环境并安装 Python 依赖。启动器不会自动安装 ego lite、yt-dlp、FFmpeg 或 Homebrew。

菜单中选「1 开始下载新增收藏」。默认处理最近最多 40 条，图片与视频都保存到项目内的 `downloads/`，不要求 NAS。「8 下载设置」可以修改数量、媒体类型、视频质量和本地目录。

## 常用命令

以下命令均在已克隆的项目目录中运行：

```bash
# 先采集并查看待处理清单，不下载媒体
.venv/bin/xdl preview

# 下载最近最多 200 条收藏中的媒体
.venv/bin/xdl run --max 200

# 仅下载图片，保存到指定本地目录
.venv/bin/xdl run --media images --out "$HOME/Downloads/X-media"

# 后台运行、查看进度、跟踪日志、安全停止
.venv/bin/xdl run --background
.venv/bin/xdl status --watch
.venv/bin/xdl logs --follow
.venv/bin/xdl stop

# 重试当前账号的下载失败项
.venv/bin/xdl retry --download-only
```

`run --dry-run` 会采集并模拟处理，不下载、不交付、不更新业务完成状态。`preview` 会保存一份本地预览清单。两者都可能访问 X。

`run --skip-scrape` 使用上次已确认账号的本地清单，不检查浏览器是否切换了账号。普通下载会重新识别账号。切换图片或视频模式后，可以补齐同一收藏中尚未下载的媒体。

旧入口 `python3 -I xdl.py --max 40` 会转入新版下载命令。完整参数见 `.venv/bin/xdl --help` 和 `.venv/bin/xdl run --help`。

## 可选：复制到 NAS

**没有 NAS 可跳过本节。** 当前支持 macOS 已挂载的 SMB 共享，不负责挂载共享或保存 SMB 密码。

1. 在 Finder 挂载自己的 SMB 共享。
2. 将示例复制为本地配置，并修改为实际的 SMB 挂载点：

   ```bash
   mkdir -p data
   cp examples/nas.example.json data/nas.json
   ```

   ```json
   {
     "mount_root": "/Volumes/MyMediaShare",
     "delivery_folder": "Backups"
   }
   ```

   `mount_root` 必须是 SMB 共享的挂载点绝对路径。`delivery_folder` 是共享内的相对目录，不能包含 `..`。

3. 运行一次 NAS 模式：

   ```bash
   .venv/bin/xdl run --delivery nas
   ```

   要让后续运行也使用 NAS，在「8 下载设置 → 7 交付位置」中选择 NAS。命令行参数只影响当次运行。

文件复制到 `<mount_root>/<delivery_folder>/X download/YYYY-MM/`，月份取自本地文件修改时间。程序复制并校验 SHA-256，已有同名同内容文件可复用，同名不同内容会报错，不覆盖。NAS 未挂载时保留本地文件，之后可运行：

```bash
.venv/bin/xdl retry --delivery-only
```

该命令明确请求 NAS 补交，即使当前偏好为本地模式也会尝试。复制连续 120 秒无进展时会放弃等待，文件保留待补交。锁屏及真实 SMB 卡死场景尚未完成实测，不能据此保证所有系统调用都及时退出。

兼容已有 `~/.config/nas-delivery.json` 配置；项目内的 `data/nas.json` 优先。普通下载和新版 NAS 复制不依赖 `nas-deliver` 命令或任何 AI 技能。

## 可选：定时与失败通知

定时默认关闭。先完成一次手动运行，再启用：

```bash
.venv/bin/xdl schedule enable --time 09:30 --every-days 3
.venv/bin/xdl schedule status
.venv/bin/xdl schedule disable
```

按本机时区运行；每隔几天以最近一次成功日期为基准。重叠任务会跳过，电脑睡眠或用户登出时无法运行，锁屏运行尚未验收。定时配置引用项目及虚拟环境的绝对路径，移动目录后需重新设置。

Bark 默认关闭。可在「8 下载设置 → 8 Bark 通知」输入设备 Key，也可运行：

```bash
.venv/bin/xdl notify set --key YOUR_DEVICE_KEY
.venv/bin/xdl notify test
.venv/bin/xdl notify off
```

建议通过菜单输入，避免设备 Key 留在 shell 历史中。支持 `notify set --server https://your-bark-server.example` 指定自建服务器。Key 保存到仅本地的 `data/bark.json`，文件权限为 `600`。启用后会把失败摘要发送给所配置的 Bark 服务器，可能包含账号、文件名或错误信息。手动中断不通知；NAS 未挂载而回退本地保存也不算失败。

## 本地数据与隐私

- `downloads/`：下载文件，NAS 复制成功后也不会自动删除。
- `data/`：账号标识、收藏清单、状态、偏好、通知配置及运行日志。日志可能含帖子链接和本地路径。
- `data/state.json` 和 `data/archive.txt` 共同支撑去重与恢复。请一起备份，不要单独删除其中一个来强制重下。
- `data/runs/` 与 `data/task-output/` 各保留最近 30 次记录；累计 `runs.log` 不自动轮转。

从源码运行时使用项目目录存放数据；普通非 editable 安装默认使用 `~/.local/share/xdownload/`。可在启动前设置 `XDL_HOME` 使用独立数据根目录，其下包含 `data/` 和默认的 `downloads/`。配置优先级为命令行参数、已保存偏好、程序默认值。

仓库用允许列表限制提交范围，排除本地数据、下载文件、构建缓存、个人 launchd 文件及内部开发记录。发布前还应检查暂存区，见 [贡献说明](CONTRIBUTING.md)。这些规则防止误提交，不能代替人工检查。只下载自己有权保存的内容，并遵守相关服务的使用规则。

## 限制与验证范围

- 采集依赖 X 网页内部 GraphQL 和页面结构，X 更新后可能失效。不是官方 X 产品。
- 单次采集最多请求 200 页；连续遇到 20 条已处理收藏会提前结束，因此提高 `--max` 不代表完整导出所有历史。
- 删除、不可见、受限或特殊卡片媒体不保证可下载。NSFW、长视频和已删除帖子的难例尚未系统实测。
- 浏览器采集限时 300 秒，单次 yt-dlp 调用限时 900 秒。登录失效、验证码和限流需要人工处理。
- Linux、Windows 和英文交互界面尚未支持。依赖工具的版本、浏览器状态和网络会影响运行结果。
- 本仓库离线测试使用合成数据，不访问真实 X 账号或 NAS；测试通过不等于真实下载或锁屏验收。

```bash
.venv/bin/python -m unittest discover -v
```

退出码：`0` 成功，`1` 部分下载或交付失败，`2` 参数或配置错误，`3` 已有任务，`4` 登录、环境或运行条件不满足，`130` 用户中断。

## 开源

采用 [MIT 许可证](LICENSE)。欢迎提交能复现的问题和改进，提交前请移除账号、Cookie、设备 Key 和私人路径，参见 [安全说明](SECURITY.md)。
