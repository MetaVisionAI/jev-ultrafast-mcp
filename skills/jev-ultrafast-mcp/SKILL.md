---
name: jev-ultrafast-mcp
description: >
  把浏览器任务整个交给 jev-ultrafast-mcp 服务，而不是自己逐步操作页面。凡是涉及网页或浏览器的任务都用它：
  填写或提交表单、登录、每日签到、点完一个流程、从页面提取内容、证明页面到达了某个状态。
  用户给出网址并要求做某件事时也用它，或者说了 "打开这个网页"、"帮我填一下"、"签到"、
  "在浏览器里点"、"预约"、"browser"、"fill this form"、"log in to"、"check in on"、
  "scrape"、"click through"。
agent_created: true
---

# 浏览器任务交给 `browser_goal`，一次调用完成

浏览器任务要**整个**交出去。循环在服务端运行，由决策模型驱动，页面内容不会进入你的上下文，
整件事只花一轮，而不是每点一下花一轮。

```
browser_goal(
  goal="…",                                             # 用文字说明怎样算完成
  url="https://…",                                      # 省略则在当前页面上继续
  verify=[{"type": "text_contains", "text": "…"}],      # 页面上用什么证明完成了
)
```

`url` + `goal` + `verify` 就是一次完整的交接。**不要**先自己打开页面走一遍，那正是这个服务要替代的事。

## 一定要传 `verify`

- `status:` 是模型自己的总结，`verify` 是事后由代码核对的，两者不一致时以断言为准。成功的任务最常见的样子是，
  最后一个动作把它操作的东西*移除*了，模型找不到事可做，就会对一个已经成功的任务报 `BLOCKED`。
- 可用的检查：`text_contains`、`element_gone`、`url_contains`、`count_at_least`。选一个**只在动作完成后才出现**的字符串，
  不要用导航栏或页头里本来就有的文字。
- `text_contains` 只匹配页面正文，不匹配元素表。下拉框的选中值、按钮文字不能用它断言；价格、结果数量、标题可以。
- 判断成功要同时看 `verified: PASS` 和最终 URL：URL 必须是只在动作完成后才存在的页面。

多字段表单，或者拿不准 `verify` 怎么写时，读[怎么写 goal 和 verify](references/writing-goals.md)。

## 读页面不是任务

`browser_open`、`browser_observe` 和 `browser_assert` 是直接、确定、免费的，不需要 key。用户只想知道页面上写了什么，
或者你需要看一个目标为什么失败时，用它们。把「看一眼」交给模型，是花钱去回答元素表本来就能回答的问题。

## 什么时候退回手动循环

只有三种情况：

1. `browser_goal` 返回 `turbo_unavailable:`，即没有配置模型 key。这时走 `browser_open` → 读元素表 → `browser_act` → `browser_assert`。
2. 你只想看（见上一节）。
3. `browser_goal` 返回 `blocked`，或者控件在页面上可见、元素表里却没有它，这时按坐标点击，见[怎么读失败结果](references/troubleshooting.md)。

## 接下来读什么

| 情况 | 读这里 |
| --- | --- |
| 登录、验证码、短信或一次性验证码、授权同意、支付确认；两次尝试都没有进展；或者操作报错 | [人工协助与恢复](references/help-and-recovery.md) |
| 多字段表单，或者不确定 `verify` 该怎么写 | [怎么写 goal 和 verify](references/writing-goals.md) |
| 浏览器没连上、`browser_doctor` 显示异常，或者要选 attach 还是 launch | [浏览器的准备和连接](references/browser-setup.md) |
| 结果是 `blocked`、`stopped`、`unconfirmed`、`turbo_unavailable`，或者控件不在元素表里 | [怎么读失败结果](references/troubleshooting.md) |
| 同一件事每天都要做 | [录制和回放宏](references/macros.md) |
| 操作飞书（任何功能） | [飞书](references/feishu.md) |

以后其他网站的特有规则也按一站一个文件放：`references/<站点>.md`。

## 汇报

每次运行都会报告自己的开销（`turbo: N decisions · N tokens · …`）。引用 `verified: PASS/FAIL` 这一行和断言细节，
而不是模型的总结；如果 `verify` 失败了，要直接说出来，不要报告成功。
