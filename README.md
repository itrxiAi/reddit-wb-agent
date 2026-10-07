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
(`MAX_COMMENTS_PER_DAY`), thread dedup, DM dedup, `DRY_RUN`.

## Setup

```bash
cp -R . ~/.workbuddy/skills/reddit-agent
cd ~/.workbuddy/skills/reddit-agent
cp .env.example .env   # fill in REDDIT_USERNAME / REDDIT_PASSWORD / OBJECTIVE
./reddit-agent status  # first run installs .venv + Chromium automatically
```

Keep `DRY_RUN=true` until the agent's writing has been reviewed.
