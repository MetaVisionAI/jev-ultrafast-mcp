# 浏览器的准备和连接

浏览器没连上、`browser_doctor` 显示异常，或者要在 attach 和 launch 之间做选择时，读这一页。

## 到底有没有浏览器？

`browser_doctor` 会报告 `connection`：`idle`、`attached` 或 `dropped`。**`idle` 是正常状态，不是故障**：服务端按需连接，
所以还没用到的浏览器显示为 `connected: false, connection: idle`。只有 `dropped` 表示曾经有连接、后来断了。
只有在本应有东西监听时，才把 `connected: false` 理解为「CDP 地址上没有东西在监听」：在 `attach` 模式下，服务端故意什么都不启动
（它自己的提示写着「nothing is started for you」），所以 `JEVMCP_CDP_URL=http://127.0.0.1:9222` 可能指向一个空端口。

自己启动一个：

```
nohup "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9222 \
  --user-data-dir="$HOME/.jev-ultrafast-mcp/chrome-profile" \
  --no-first-run --no-default-browser-check about:blank &
```

- **有界面，不要无头模式**：无头浏览器过不了 Cloudflare，会一直停在「Just a moment…」。
- **不要用默认 profile**：Chrome 136 及以上版本不允许在默认 profile 上打开调试端口。专门指定一个 `--user-data-dir` 才合规。
- 你需要的登录状态保存在那个 profile 目录里，不在服务端。在那里登录一次，以后每次交接都能用。
- **不要**替用户输入密码。如果目标卡在登录页，用 `browser_request_help` 把这一步交给用户，再把任务交回 `browser_goal`，
  见[人工协助与恢复](help-and-recovery.md)。

提供浏览器时有两个坑：

- **`/json/version` 返回 404 不代表没有东西在监听。** Chrome 144 及以上版本中，`chrome://inspect/#remote-debugging`
  开启的服务只支持 WebSocket，对所有 `/json/*` 路径都返回 404。端口和浏览器的 WebSocket 路径写在浏览器数据目录里的
  `DevToolsActivePort` 文件中，服务端会从那里读取，所以用这个开关启动的浏览器（包括用户自己的 Chrome，那里有他的登录状态）
  都能正常用。如果数据目录不常见，设置 `JEVMCP_ATTACH_PROFILE_DIR`。
- **一个端口只能有一个浏览器。** 两个 Chrome 不能共用 9222：谁绑定了 IPv4 谁响应，如果响应的是错的那个，你会收到 404，
  而正确的那个实例在 `[::1]` 上闲着。如果启动看起来静默失败了，重启之前先用 `lsof -nP -iTCP:9222 -sTCP:LISTEN`
  检查是否有*两*行。另外注意，`open -na "Google Chrome" --args …` 可能丢掉参数、启动默认 profile；
  要从进程自己打开的文件确认 profile，而不是相信你敲的命令。

## `attach` 每次连接要点一下；`launch` 只需登录一次

在 Chrome 144 及以上版本中，`chrome://inspect/#remote-debugging` 服务会对**每一次连接**弹出「Allow remote debugging?」对话框，
不是每次启动只弹一次。所以 attach 模式要求用户批准*每一次*连接，而重连**也是**一次连接，任何重复性任务每次都要点一下。

`launch` 模式配合非默认的 `--user-data-dir`，既不需要那个开关，也不弹任何对话框（实测：启动、连接、`Browser.getVersion`
共 4.6 秒，零弹窗）。代价是新 profile 一开始是未登录的。对重复性任务来说这更划算：在那个 profile 里登录一次，之后每次运行都没有弹窗。

只有当用户已有的登录状态正是关键、而且只运行一次时，才 attach 到用户自己的 Chrome。

## 环境配置

- **页面必须在前台标签页。** 后台标签页会被 Chrome 降速，机器人检查（Cloudflare 的「Just a moment…」）就永远过不去，
  会一直卡到超时，每次尝试的 Ray ID 都不一样。在服务端的环境变量里设置 `JEVMCP_FOREGROUND=1`。
- **服务端只在导入时读一次配置。** 修改 MCP 配置后必须重启客户端（在 WorkBuddy 中，如果增删了环境变量的*名字*，
  还要再点一次 **Trust**；只改值不会改变授权）。
