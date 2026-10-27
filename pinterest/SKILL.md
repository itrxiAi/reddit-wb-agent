---
name: pinterest-agent
description: "Pinterest publishing agent driven by you (WorkBuddy). You are the brain: decide what to pin and write every pin yourself. The CLI is the hands: it opens pinterest.com/pin-creation-tool in a stealth browser, uploads image/video, fills title/description/link/board, and clicks Publish. Commands: pinterest-agent status / login / login-check / publish image|video / pins / digest / pause / resume."
argument-hint: "[status|login|login-check|publish|pins|digest|pause|resume]"
allowed-tools:
  - exec
  - read
  - write
  - edit
triggers:
  - user
  - model
---

# Pinterest Agent

You drive the Pinterest pin-creation tool via the `pinterest-agent` CLI in
**this skill's directory** (the folder containing this SKILL.md). **The tool
has no brain** — it uploads media, fills the form, and clicks Publish. YOU
write every pin yourself, following the style rules below.

Paths below are relative to this skill dir: `./pinterest-agent`. State lives
at the plugin root: `../.env`, `../data/pinterest/cookies.json`. Run commands
as `./pinterest-agent <cmd>` from this dir.

## First-run setup

The CLI auto-bootstraps on first run (creates `../.venv`, installs deps +
Chromium) — just run `./pinterest-agent status` once and wait. Requires the
plugin root layout: `../src/`, `../pyproject.toml`.

**Login — manual sign-in is the normal path.** Pinterest auth is
email/password or OAuth — only a human can do it:

1. Run `./pinterest-agent login` — a Chrome window opens on the login page.
2. The user signs in (email/password or "Continue with Google/Apple").
3. Cookies are saved to `../data/pinterest/cookies.json` automatically once
   the `_pinterest_sess` cookie appears.
4. Verify: `./pinterest-agent login-check` → `{"logged_in": true}`.

Cookie-export also works: user exports pinterest.com cookies from their own
browser via Cookie-Editor → paste JSON into
`../data/pinterest/cookies.json` (`chmod 600`). The `_pinterest_sess` cookie
lasts about a year; if `login-check` ever returns false, re-login.

Ask the user for their **objective** (e.g. "drive traffic to my shop",
"grow an audience for my design blog") and write it to
`PIN_AGENT_OBJECTIVE` in `../.env`. No API keys — you are the LLM.

There is no dry-run mode: `publish` posts for real. Before EVERY publish,
show the user the title, description, media file, link and board, and get an
explicit "发吧" — the approval is bound to that exact content; if anything
changes, confirm again. The final gate is physical: by default the CLI fills
the form and the user clicks Publish themselves in the Chrome window. If you
can't reach the user, draft it and report — do not publish (and do not pass
--auto unattended).

## Workflows

### Publishing an image pin (when user asks)

```
1. ./pinterest-agent status       → local, instant. paused:true? report
                                    reason and STOP. too_soon/quota errors
                                    are gates.
2. Prepare the media: one local file, jpg/png/webp. 推荐 2:3 竖图
   (e.g. 1000×1500) — vertical pins get more feed space.
3. Write the pin yourself (style rules below): title ≤100 chars,
   description ≤500 chars. Optional: --link destination URL,
   --board board name (or PIN_DEFAULT_BOARD in ../.env).
4. Show user: title / description / media / link / board → wait for
   explicit approval.
5. ./pinterest-agent publish image --title "Title" \
     --desc-file /tmp/desc.txt --image a.jpg \
     --link "https://..." --board "My Board" \
     --tags "travel, photography"
   → the CLI fills the form, then WAITS (default 15 min,
     --confirm-timeout): the USER clicks the red「Publish」button in the
     Chrome window as the final confirmation. The CLI detects the
     builder closing / published toast and returns {"success": true,...}.
6. Report: published or the exact failure (login_required,
   verification_required, upload timeout, manual_confirm_timeout, ...).

Manual-confirm is the default even though Publish is plain DOM — the human
click is the approval step. `--auto` makes the CLI click it itself
(reliable here; still requires the user's approval first).
```

`--description` works for short text; `--desc-file` is safer for long text
(no shell-quoting issues).

### Optional pin fields (with defaults)

| Flag | Field | Default |
|---|---|---|
| `--tags "a,b,c"` | Tagged topics — invisible interest tags that feed Pinterest's topic graph. Each is typed into the combobox and the suggestion picked. | none |
| `--alt-text "..."` | Alt text (accessibility + SEO) under 更多选项. | the pin title; `--alt-text ''` leaves it empty |
| `--comments` / `--no-comments` | 允许用户发布评论 toggle | on |
| `--similar-products` / `--no-similar-products` | 显示类似产品 visual-search toggle | on |
| `--ai-modified` | 标记为 AI 修改 disclosure — REQUIRED when the media is AI-generated/edited | off |
| `--ai-people` | 此 Pin 图包含 AI 生成的人物 (implies `--ai-modified`) | off |
| `--product URL-or-term` | 产品标签 — opens the picker, pastes the link or searches the term, takes the first result. Verify the sticker position in the Chrome window. | none |

The result JSON's `check` block echoes every field's final state (title,
description, link, board, topics count, alt_text, switches) — verify it
matches what the user approved before counting the publish as done.
For AI-generated media always pass `--ai-modified` — Pinterest requires
the disclosure and hides/de-ranks undisclosed AI content.

### Publishing a video pin

Same flow with `./pinterest-agent publish video --video file.mp4`.
Upload + transcode can take minutes (`--upload-timeout`, default 600s) —
the tool waits for the Publish button to enable.

### Checking the account

```
./pinterest-agent pins --limit 10   → pins published by this tool (local ledger)
./pinterest-agent digest            → today's posted pins
```

## Writing rules — READ CAREFULLY, this is how pins perform

- Title ≤100 chars: keyword-first — Pinterest is a search engine, the
  title IS the SEO. "5 small balcony garden ideas" beats "my garden".
- Description ≤500 chars: 2-4 sentences, natural keywords (no hashtag
  stuffing — Pinterest ignores/hates them). End with the payoff:
  what the user gets when they click.
- Link: only when it genuinely expands on the pin — Pinterest deprioritizes
  link-bait; a missing/wrong link is worse than none.
- Image decides everything: clean 2:3 vertical, readable text overlay if
  the user has it. Horizontal images die in the feed.
- Board: pick the most specific relevant board, not the catch-all.
- If a pin reads like AI wrote it ("Unlock the secrets of...", "In today's
  fast-paced world"), rewrite it.

## Hard gates (the tool enforces these — you can't override)

- **paused** → `data/pinterest/paused.flag` is set. `publish` refuses.
  Relay `paused_reason`; only the user runs `./pinterest-agent resume`.
- **daily quota** → publish refuses past `PIN_MAX_PINS_PER_DAY` (default 5).
- **min interval** → publish refuses inside `PIN_MIN_PIN_INTERVAL_MINUTES`
  (default 30) since the last pin.
- **platform limits** → title >100 chars, description >500 chars, missing
  media file, unsupported file type — all rejected locally.
- **verification_required** → Pinterest showed a security check; the user
  must solve it in the open Chrome window, then retry. Do NOT retry blindly.

## Command reference

| Command | What it returns |
|---|---|
| `status` | paused / quota / last pin time (local, instant) |
| `login` | open Chrome → human signs in → cookies saved |
| `login-check` | `{"logged_in": true/false}` |
| `publish image` | success / error JSON (+ per-field checks) |
| `publish video` | success / error JSON |
| `pins [--limit N]` | pins published by this tool |
| `digest` | today's stats |
| `pause` / `resume` | circuit breaker |

## User requests

| User says | Do this |
|---|---|
| "发一个 pin" | publishing workflow — draft, show, confirm, publish |
| "把这张图发到 Pinterest" | same, adapting their material to the style rules |
| "看看我的 pin 情况" | `pins` + `digest`, summarize |
| "Pinterest 登录一下" | `login` or guide cookie export |
| "少发点 / 别发了" | `PIN_MAX_PINS_PER_DAY` in `../.env` (0 = off) |
| "停下" | `./pinterest-agent pause` |
