## Human steps and recovery

Nothing in the server decides that a page needs a person. You do, from what the
observation shows, and this page is the rule for it.

With help enabled, request help for login, CAPTCHA, SMS or OTP codes, payment
confirmation, consent, or after two attempts make no progress:

```
browser_request_help(prompt="请在此窗口登录淘宝，完成后点击“完成并交还控制权”",
                     targets=["e3"])
```

What usually points here in an observation or a goal result:

- a login form, a password field, or a QR-code sign-in panel in the element table;
- `! dialog open: …` naming a sign-in or verification dialog;
- `! N cross-origin frame(s) not readable (…)` whose sources are a login or
  CAPTCHA host — `login.taobao.com`, `passport.…`, `turing.captcha.gtimg.com`,
  `geetest`, `recaptcha`, `hcaptcha`;
- `browser_goal` answering `blocked` or `stopped: no progress` twice on the same
  page, after the checks in SKILL.md's "Reading a failure" have been ruled out.

The call brings the session's tab to the front and shows a panel on the page
("Agent 需要你的帮助") with the prompt, a note field, and two buttons:
"完成并交还控制权" and "取消请求". It blocks until the user presses one, the
completion criteria hold, or the timeout (default 5 minutes, at most 30) runs out.
The panel is put back if the page navigates while the user works, and removed
before the call returns.

Use a precise prompt that says what to do and when to press the button. Observe
first and pass fresh refs, or CSS selectors, as `targets`; omit them when no
control fits. Refs are outlined by their position on the page, so a ref that is
covered or off-screen is reported unresolved rather than outlined.

Use completion criteria only for a clear, stable success signal:

```
completion={"any": [{"selector_missing": "#login-dialog"},
                    {"url_contains": "/my_taobao"}],
            "stable_for_ms": 1000}
```

`any` or `all`, with at most eight conditions in total, chosen from
`url_contains`, `url_matches`, `selector_exists`, `selector_missing`,
`text_exists` and `text_missing`. `url_matches` is a Python regular expression of
at most 128 characters; an invalid or oversized pattern is refused before help
starts. A condition must hold for `stable_for_ms` (default 1000) before it
completes the request, so a page that flickers through the success state does
not end it early. The user can always finish manually.

| Result | Next step |
| --- | --- |
| Help `continued` / `completed` | Observe again, then resume with fresh refs, or hand the task back to `browser_goal`. |
| Help `cancelled` / `timed_out` | Respect the rejection or the blocker; do not repeat the request. |
| Help `disabled` | No human action was confirmed. Re-observe and follow the disabled-help rules below. |
| Stale ref | Observe and retry the intended action once. |
| Unknown session or tab | List sessions or tabs; never guess IDs or use another task's session. |
| Timeout or unknown effect | Inspect current state before retrying; the action may already have happened. |

A note the user typed comes back with the result; read it, it may change the
plan. Navigation alone is not completion: a page that moved while the user
worked has not told you the step is done.

**Help disabled** (`JEVMCP_REQUEST_HELP=off`, or a headless browser that nobody
can see): do not request help, and do not change the setting or switch to a
headed browser to get round it. Use existing login state, authorized inputs and
viable alternatives — for example putting the query in the URL, as SKILL.md
describes. Phone-only QR scans, face verification, missing SMS codes or
image-only CAPTCHAs may remain blocked. Report a specific blocker only when
inputs or capabilities are missing or viable approaches are exhausted; continue
independent work. Do not loop on identical failures or repeat unknown effects.

Never type the user's password, read it back, or try to solve a CAPTCHA for
them. Those are the steps this tool exists to hand over.
