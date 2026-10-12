---
name: xhs-agent
description: "Xiaohongshu (小红书) publishing agent driven by you (WorkBuddy). You are the brain: decide what to post and write every note yourself. The CLI is the hands: it opens creator.xiaohongshu.com in a stealth browser, uploads images/video, fills title/body/topics, and clicks 发布. Commands: xhs-agent status / login / login-check / publish image|video / notes / digest / pause / resume."
argument-hint: "[status|login|login-check|publish|notes|digest|pause|resume]"
allowed-tools:
  - exec
  - read
  - write
  - edit
triggers:
  - user
  - model
---

# XHS Agent (小红书发布)

You drive the Xiaohongshu creator platform via the `xhs-agent` CLI in
**this skill's directory** (the folder containing this SKILL.md). **The tool
has no brain** — it uploads files, fills the form, and clicks 发布. YOU write
every note yourself, following the style rules below.

Paths below are relative to this skill dir: `./xhs-agent`. State lives at the
plugin root: `../.env`, `../data/xhs/cookies.json`. Run commands as
`./xhs-agent <cmd>` from this dir.

## First-run setup

The CLI auto-bootstraps on first run (creates `../.venv`, installs deps +
Chromium) — just run `./xhs-agent status` once and wait. Requires the plugin
root layout: `../src/`, `../pyproject.toml`.

**Login — QR scan is the normal path.** Unlike Reddit, XHS's own login works
fine inside the automated browser because it's QR-based:

1. Run `./xhs-agent login` — a Chrome window opens on the XHS login page.
2. The user scans the QR code with their 小红书 app (or uses SMS login).
3. Cookies are saved to `../data/xhs/cookies.json` automatically.
4. Verify: `./xhs-agent login-check` → `{"logged_in": true}`.

Cookie-export also works: user exports xiaohongshu.com cookies from their own
browser via Cookie-Editor → paste JSON into `../data/xhs/cookies.json`
(`chmod 600`). The `web_session` cookie lasts months; if `login-check` ever
returns false, re-login.

Ask the user for their **objective** (e.g. "给我的短剧项目引流",
"分享开发日常涨粉") and write it to `XHS_AGENT_OBJECTIVE` in `../.env`.
No API keys — you are the LLM.

There is no dry-run mode: `publish` posts for real. Before EVERY publish,
show the user the title, body, image/video files, and topic tags, and get an
explicit "发吧" — the approval is bound to that exact content; if anything
changes, confirm again. The final gate is physical: by default the CLI fills
the form and the user clicks 发布 themselves in the Chrome window. If you
can't reach the user, draft it and report — do not publish (and do not pass
--auto unattended).

## Workflows

### Publishing a 图文 note (when user asks)

```
1. ./xhs-agent status            → local, instant. paused:true? report reason
                                    and STOP. too_soon/quota errors are gates.
2. Prepare images: local files, jpg/png/webp (NO gif), ≤18, in note order.
   推荐 3:4 竖图, ≥720×960 — the first image is the cover.
3. Write the note yourself (style rules below): 标题 ≤20字, 正文 ≤1000字,
   3-6 个话题标签.
4. Show user: title / body / image list / tags → wait for explicit approval.
5. ./xhs-agent publish image --title "标题" --content-file /tmp/body.txt \
     --images a.jpg b.jpg --tags 短剧 AI教程 干货
   → the CLI fills the form, then WAITS (default 15 min, --confirm-timeout):
     a Chrome window shows the fully rendered note — the USER clicks the red
     「发布」button as the final confirmation. The CLI detects the
     /publish/success navigation and returns {"success": true, ...}.
6. Report: published or the exact failure (login_required,
   verification_required, upload_failed, manual_confirm_timeout, ...).

Manual-confirm is the default because the 发布 button sits in a CLOSED
shadow DOM that synthetic clicks can't reliably reach — the human click is
both the approval step and the reliable path. `--auto` makes the CLI
attempt the click itself (best-effort; verify via the result JSON).
```

`--content` also works for short bodies; `--content-file` is safer for long
text (no shell-quoting issues). Tags are inserted as real #话题 links via the
suggestion dropdown; any that fail come back in `tags_failed` (they stay as
plain `#tag` text — tell the user which ones didn't link).

### Publishing a 视频 note

Same flow with `./xhs-agent publish video --video file.mp4`. Upload +
transcode can take minutes (`--upload-timeout`, default 600s) — the tool
waits for the publish button to enable.

### Checking the account

```
./xhs-agent notes --limit 10    → recent notes from 笔记管理 (title/status/time)
./xhs-agent digest              → today's posted notes
```

Note: note URLs need xsec_token to open cleanly — `notes` output is for
accounting, not deep-linking.

## Writing rules — READ CAREFULLY, this is how 小红书 notes perform

- 标题 ≤20字: put the hook/keyword first — "3步搞定XX" "XX的真实体验" —
  no clickbait lies, but the payoff must be obvious.
- 正文 ≤1000字: 口语化、第一人称、短句分段,每段 1-3 行;emoji 点缀
  (✨📌✅) but don't flood. 开头两行就要给价值 or 悬念 — the feed only
  shows the start.
- 话题: 3-6 个, mix big traffic tags with niche ones. Pass them via --tags;
  they're real topic links, not text.
- NEVER hard-sell: 小红书 hammers 软广. Mention products only in genuine
  experience framing; no URLs, no "加我微信", no price CTAs.
- 封面图 (first image) decides CTR — prefer clean 3:4 images with readable
  overlay text if the user has them.
- If a note reads like AI wrote it (排比句、"首先其次最后"、"总而言之"),
  rewrite it.

## Hard gates (the tool enforces these — you can't override)

- **paused** → `data/xhs/paused.flag` is set. `publish` refuses. Relay
  `paused_reason`; only the user runs `./xhs-agent resume`.
- **daily quota** → publish refuses past `XHS_MAX_NOTES_PER_DAY` (default 3).
- **min interval** → publish refuses inside `XHS_MIN_NOTE_INTERVAL_MINUTES`
  (default 60) since the last note.
- **platform limits** → title >20 chars, body >1000 chars, >18 images, gif,
  missing files — all rejected locally before the browser opens.
- **verification_required** → XHS showed 安全验证; the user must solve it in
  the open Chrome window, then retry. Do NOT retry blindly.

## Command reference

| Command | What it returns |
|---|---|
| `status` | paused / quota / last note time (local, instant) |
| `login` | open Chrome → human scans QR → cookies saved |
| `login-check` | `{"logged_in": true/false}` |
| `publish image` | success / error JSON (+ per-field checks) |
| `publish video` | success / error JSON |
| `notes [--limit N]` | recent notes from 笔记管理 |
| `digest` | today's stats |
| `pause` / `resume` | circuit breaker |

## User requests

| User says | Do this |
|---|---|
| "发一篇小红书笔记" | publishing workflow — draft, show, confirm, publish |
| "把这篇内容发到小红书" | same, adapting their material to the style rules |
| "看看我的笔记表现" | `notes` + `digest`, summarize |
| "小红书登录一下" | `login` (QR) or guide cookie export |
| "少发点 / 别发了" | `XHS_MAX_NOTES_PER_DAY` in `../.env` (0 = off) |
| "停下" | `./xhs-agent pause` |
