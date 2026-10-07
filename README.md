# reddit-wb-agent

A Reddit actuator packaged as a **WorkBuddy skill** — WorkBuddy is the brain,
this tool is the hands.

Fork of [reddit-agent](https://github.com/sohazur/reddit-agent), cut down to
the essentials: no internal LLM pipeline, no research mode, no integrations.
WorkBuddy reads threads and writes every comment itself; this tool browses
Reddit in a stealth browser and submits the text it's given.

## Structure — the project IS the skill

Drop this folder into `~/.workbuddy/skills/reddit-agent/`:

```
SKILL.md          agent manual: workflows + writing rules
reddit-agent      CLI wrapper — auto-creates .venv + Chromium on first run
src/              engine (browser, scanner, safety, db)
data/             subreddits.yaml + runtime state (cookies, reddit.db)
.env              Reddit credentials + limits (create from .env.example)
```

No install script needed — the first `./reddit-agent status` bootstraps the
Python environment (requires Python 3.12+).

## Commands

| Command | What it does |
|---|---|
| `status` | Paused / quota / dry-run snapshot (JSON) |
| `cycle [--threads N]` | One session: inbox + pending + karma + scan/browse/upvote all subs + candidate thread bodies |
| `login` | Open Chrome, wait for manual login, save cookies |
| `login-check` | Is the session logged in? |
| `inbox` | Ban & removal notices |
| `karma` | Account karma |
| `scan --sub NAME` | Threads in a subreddit |
| `thread --url URL` | Full thread + comments |
| `pending` | Replies to our comments + new DMs |
| `submit comment --thread-url U --text T` | Post a comment |
| `submit post --sub S --title T --body B` | Create a post |
| `submit dm --to U --subject S --text T` | Send a DM |
| `digest` | Today's stats |
| `pause` / `resume` | Circuit breaker |

Hard gates enforced by the tool: circuit breaker, daily quota
(`MAX_COMMENTS_PER_DAY`), thread dedup, DM dedup.

## Setup

```bash
cp -R . ~/.workbuddy/skills/reddit-agent
cd ~/.workbuddy/skills/reddit-agent
cp .env.example .env   # fill in REDDIT_USERNAME / REDDIT_PASSWORD / OBJECTIVE
./reddit-agent status  # first run installs .venv + Chromium automatically
```

There is no dry-run flag: `submit` always posts for real, so the agent
must show you the exact text + target and get your approval first.

## Login — use cookie export, not scripted login

Reddit's anti-bot wall rejects logins inside the automated browser
(fresh profile + remote control reads as "incorrect password" or a
network-security block page), even with correct credentials. Two paths:

**Cookie export (recommended — always works):**

1. Log in to reddit.com in your own everyday Chrome
2. Install the Cookie-Editor extension, open reddit.com, Export → JSON
3. Save the JSON as `data/cookies.json` in this folder, `chmod 600` it
4. Verify: `./reddit-agent login-check` → `{"logged_in": true}`

The `reddit_session` cookie lasts ~6 months. If `login-check` ever
returns false, re-export.

**Manual login window (fallback):** `./reddit-agent login` opens Chrome
on the login page and waits 5 min for you to log in, then saves cookies.
May still hit the network-security wall on flagged contexts.
