# Security and privacy / 安全与隐私

Do not attach real browser profiles, Cookies, `data/`, media downloads, or notification keys to issues. Remove account handles and private paths from logs. The scraper reads the active session inside the signed-in browser; it does not need an exported Cookie file. The web-client bearer identifier in `scrape.mjs` is shared X client metadata, not a user's login credential. Account session values are read at runtime inside the browser.

不要将真实浏览器资料、Cookie、`data/`、下载内容或通知 Key 上传到 Issue。日志需要先移除账号和私人路径。采集使用已登录浏览器中的会话，不需要导出 Cookie。`scrape.mjs` 中的网页客户端 bearer 标识属于共享客户端信息，个人会话值在浏览器内运行时读取。

Bark is optional and sends a failure summary to the configured server. Keep it disabled if those summaries should stay local. NAS credentials are managed by macOS, not this program. Neither a Git ignore rule nor the publication guard can prove that every secret has been removed.

Bark 为可选功能，会将失败摘要发送到配置的服务器。如果摘要需要留在本地，请保持关闭。NAS 凭据由 macOS 管理。Git 忽略规则和发布检查均不能保证识别所有敏感信息。

If you find a vulnerability, use GitHub private vulnerability reporting when available. Otherwise, open an issue describing only the affected feature and ask for a private reporting channel. Do not post working credentials or private data. If a credential was published, revoke it first; deleting the current file does not remove it from Git history.

发现漏洞时，如 GitHub 提供私密漏洞报告入口，请优先使用。否则仅在 Issue 中说明受影响的功能，询问私下反馈渠道，不要公开凭据或私人数据。凭据一旦公开，应先撤销；删除当前文件不会清除 Git 历史中的内容。
