# 怎么读失败结果

`browser_goal` 返回 `blocked`、`stopped`、`unconfirmed` 或 `turbo_unavailable`，或者某个控件在页面上可见、元素表里却没有它时，读这一页。

判断一次交接，要看 `verified: PASS`，以及最终 URL 是不是只在动作完成后才存在的页面；URL 只是变了，也可能还是编辑页。不要看 `status`。

## `status: blocked`，`steps: 0`

模型找不到可以操作的东西。按顺序检查：

1. 你的 `verify` 字符串真的在页面上吗？
2. 目标**在元素表里**吗？`observe` 会报告 `omitted N low-priority`，不在表里的东西模型看不见，看不见的目标只能得到 `blocked`。

这两种都是正确行为，不是故障。元素表按 role 优先排序，服务端也会等页面停止请求、元素表停止变化后才问模型
（`JEVMCP_SETTLE_TIMEOUT`，默认 4 秒）。如果第 0 步反复 `blocked`，先调大 `JEVMCP_SETTLE_TIMEOUT`，再改写目标或 `verify`。

如果最终视图是登录页、验证码或输入验证码的提示，目标没有写错，改写也过不去：那是需要人来做的一步，按[人工协助与恢复](help-and-recovery.md)处理。

## `status: stopped: no progress`

页面不再变化的次数达到了上限，所以运行结束，而不是把剩下的 `max_steps` 都花在 `WAIT` 上。这通常说明目标*已经成功*：
最后一个动作移除了它操作的东西。所以先看 `verified` 再看 `status`：断言通过会把它升级为 `done`。
如果没有 `verified` 或者它失败了，说明目标确实卡住了，需要换一个目标写法，而不是重试；
除非卡住它的是需要人来做的一步（登录墙、滑块、短信验证码），这是任何目标写法都过不去的，按[人工协助与恢复](help-and-recovery.md)处理。

## `status: unconfirmed: …`

模型报告了 `done`，但 `verify` 没有确认。以断言为准，把它当作没完成；但先检查你自己的字符串，因为写错的 `verify` 也会产生同样的结果。
在一次真实的每日签到中实测到：模型点了提交按钮，两次报告 `done`，积分一次都没变。
`done` 也可能在 `steps: 0` 时出现，而你指定的元素根本不在页面上。模型发现目标不存在时，报 `done` 和报 `blocked` 一样常见，
这正是 `verify` 不可省略的原因。

## `turbo_unavailable: … TYPE_TEXT …`

模型*已经*回答了：它给出了一个往字段里输入文字的计划，是**策略层**拒绝了它。可选的文本助手只影响往输入框里写值；
只需要点击的任务不需要 key。不要把这报告成「模型不可用」。

具体是哪条死路决定了怎么修，所以要读句子，而不是读错误码：

- **Jev 自己的 API**（`JEV_PROVIDER=typesafe`，默认）只回答结构化问题，从不生成文字，所以没有聊天接口可以让助手继承。
  要么给助手单独配一个聊天服务（`TEXT_MODEL_API_KEY` 加 `TEXT_MODEL`），要么把决策模型换到 `JEV_PROVIDER=openrouter`，
  并在那里用 `TEXT_MODEL` 指定一个聊天模型。
- **OpenRouter**（`JEV_PROVIDER=openrouter`）提供聊天接口，所以设置了 `OPENROUTER_API_KEY` 后，助手会继承 key *和* URL，
  唯一缺的是 `TEXT_MODEL`；`deepseek-chat` 不是 OpenRouter 的模型名。在 OpenRouter 上却提示要 `TEXT_MODEL_API_KEY`，
  只有两种可能：根本没有 key，或者设置了 `TEXT_MODEL_BASE_URL` 却没配它的 key，这会被当作显式指定的接口，不会借用决策模型的 key。
- 无论哪种，**key 和 URL 都来自同一个服务商。** 跨服务商借用 key 会得到一个 401，报错里点名的是你根本没调用的那家公司，
  这比原来的拒绝更难排查。`browser_doctor` 会报告解析出的服务商，以及助手是否有可用接口。

### 变通办法：把参数放进 URL

没配接口时，把参数放进 URL，让模型只负责等待和读取。很多网站都接受 URL 参数，包括自然语言查询，比如机票搜索可以写成
`…/travel/flights?q=One way flights from SIN to PQC on 2026-09-26`，这样就完全不需要输入文字。加上 `hl=en`
（或网站自己的语言参数）也会让页面更好读、更好断言。

## 控件不在元素表里：按坐标点击

图标、canvas，或者没有 `button`、也没有 role 的可点击区域，可能在页面上可见，元素表里却没有 ref。不要把截图交给另一个模型：

1. 用 `browser_act` 截一张视口截图（`{"op": "screenshot"}`），读这张图。
2. 调用 `browser_click_at(x, y)`。`x` 和 `y` 是图片上的比例：`0,0` 是左上角，`1,1` 是右下角。控件上能看到的文字填进 `label`。
3. 点完后重新观察。一旦出现普通的输入框或按钮，就回到元素表，不要继续按坐标点。

登录、验证码和支付确认仍然交给 `browser_request_help`，坐标点击不是绕过它们的办法。

## 运行中的其他现象

- **`browser_goal` 会跟随当前页面刚打开的新标签页**，被离开的页面保持打开。`browser_act` 只报告新标签页（`NEW TAB opened: …`），
  切换交给你：用 `browser_tabs`，`action=switch`，加上那个标签页的 `target_id`。
- **断开的连接会自愈。** 管理器在工具请求会话时检查连接是否存活；发现连接已断，就把它连同绑定在上面的会话一起丢掉，再开一个新连接。
  这时 `doctor` 显示 `connection: dropped`。目标返回 `transport closed … keepalive ping timeout` 就是这种情况：把同一个目标再交一次，
  不要自己把页面做完。如果第二次交接也以同样方式断开，说明服务端版本过旧，重启它。
