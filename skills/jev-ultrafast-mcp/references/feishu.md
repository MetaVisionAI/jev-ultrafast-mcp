# Feishu

Read this when the task is on Feishu (飞书), whatever the feature. The rules below
were measured on the calendar; they are properties of that site, not of forms in
general.

## Creating an event

The calendar opens in a new tab from the messenger sidebar. `browser_goal` follows
that tab. Do not click the calendar entry again.

A date field is set by clicking it and then the day. Typing into it opens the
picker on the wrong month.

The start-time dropdown lists clocks. Click the field, then the option whose name
is exactly that clock (`14:00`). An option whose name contains `小时` is a
duration counted from the start clock; do not pick it for the start time.

The end-time dropdown is not a list of clocks. Every option is a clock plus a
duration, such as `15:00 (1小时)`. Click the option whose name starts with the
end clock the goal asked for. A rule of "skip anything containing 小时" excludes
every end-time option, and the model then clicks the field again instead of
choosing one.

The editor's side grid repeats the name `时间`, one cell per hour. Those cells
are not how the time is set. Do not click them.

Saving is allowed only after the inputs show the start clock, the end clock, and
every person the goal named. Saving opens 「向参与者发送日程邀请？」 with
「继续编辑」, 「不发送」, and 「发送」. Click 「发送」 to deliver the invite.
「继续编辑」 leaves the event unsaved. The page then returns to the week view
(`/calendar/week`), not the editor (`/calendar/eventedit`).

## How to prove it

`url_contains` of `/calendar/` is true of the editor as well as the week view.
Require `/calendar/week`.

`text_contains` of the title alone passes on the unsaved draft: the editor paints
the title into the side panel before 保存. On the week view, after the editor has
closed, the event's own text includes the title and the clocks, for example
`研发会议 14:00 - 15:00`. Assert on that string. A wrong end time fails the check.

## Example

Tomorrow 14:00–15:00, title 研发会议, with 杨飞 and 张晋伟. `max_steps` 40.

```
browser_goal(
  goal="在飞书为明天预约研发会议。日期点击后再点选那一天，不要往日期框里打字。开始时间点选名称恰好是「14:00」的选项。结束时间点选名称以「15:00」开头的选项，例如「15:00 (1小时)」；结束时间下拉里每一项都带时长，不要因为带「小时」而跳过。不要点侧边网格里的「时间」。参与人框先输入杨飞并点建议，再输入张晋伟并点建议。只有开始时间是14:00、结束时间是15:00、杨飞和张晋伟都在参与人里时才点保存。保存后弹出「向参与者发送日程邀请？」就点「发送」。发送后留在周视图，不要再点刚创建的日程。",
  url="https://<tenant>.feishu.cn/next/messenger/",
  max_steps=40,
  verify=[
    {"type": "url_contains", "text": "/calendar/week"},
    {"type": "text_contains", "text": "研发会议 14:00 - 15:00"},
  ],
)
```
