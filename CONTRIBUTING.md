# Contributing / 贡献说明

This project currently targets macOS. Keep changes small and document user-visible behavior in both READMEs. Do not include account data, captured GraphQL responses, real media, or personal configuration as fixtures. Use synthetic test data.

本项目目前面向 macOS。请保持修改范围清晰，在两份 README 中同步说明用户可见行为。测试使用合成数据，不提交真实账号、GraphQL 抓取结果、媒体文件或个人配置。

## Development / 开发

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest discover -v
```

The root discovery command must collect both `test_xdl.py` and `tests/`. The tests do not need X login, a NAS, Bark, ego-browser, yt-dlp, or FFmpeg. Real downloads and locked-screen checks require a separate manual run and must not be claimed from offline tests.

根目录测试命令应收集 `test_xdl.py` 和 `tests/`。离线测试不要求 X 登录、NAS、Bark、ego-browser、yt-dlp 或 FFmpeg。真实下载和锁屏行为需要单独手测，不能用离线测试代替。

## Before publishing / 发布前

```bash
git status --short --branch -uall
git diff --cached --stat
git diff --cached
python3 scripts/check_public_tree.py
```

The guard reads the Git index and reports filenames and rule names without printing matched values. It detects private runtime paths, unexpected files, common token formats, private keys, and personal home paths. It is deliberately limited and is not a full secret scanner. Review every staged file and any commit history before pushing. Root files use an allowlist in `.gitignore`; update that list and the guard together if you intentionally add a new public root file. `data/`, `downloads/`, personal launchd files, build outputs, and internal notes must stay untracked.

检查脚本读取暂存区，只输出文件名和规则名，不打印命中的值。它检查私有运行目录、未审核文件、常见 Token、私钥和个人主目录路径，能力有限，不能代替完整秘密扫描和人工检查。推送前检查每个暂存文件及已有历史。根目录采用允许列表；新增公开根文件时同步修改 `.gitignore` 与检查脚本。`data/`、`downloads/`、个人 launchd 文件、构建产物和内部记录必须保持未跟踪。

To inspect distributions without mixing in local state, build from a clean checkout:

```bash
.venv/bin/python -m pip install build
.venv/bin/python -m build
```

Inspect both the source archive and wheel. The installed package must contain `xdownload/scrape.mjs`, including when built from the source archive. Install the wheel into a fresh virtual environment, run `xdl --help` outside the checkout, and set `XDL_HOME` to a temporary directory for smoke checks. Never use real account data in CI. Source pushes do not imply a PyPI release or a completed live-download test.

从干净检出构建并检查源码包与 wheel。安装包必须含 `xdownload/scrape.mjs`，从源码包二次构建时也要保留。使用新虚拟环境安装 wheel，在项目外运行 `xdl --help`，冒烟测试将 `XDL_HOME` 指向临时目录。CI 不使用真实账号。源码推送不等于 PyPI 发布或真实下载验收。
