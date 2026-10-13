# Nintendo Museum cancellation watcher

Checks the public ticket calendar at :00 every hour (Japan time) and posts to
Discord when an October or November 2026 date shows the ○ mark, meaning
first-come tickets are on sale. You then buy by hand.

It only reads the calendar while logged out. It never logs in, never opens a
date, and never buys anything. That's two page loads an hour, about what you'd
do refreshing it yourself.

Runs free on GitHub Actions (free for public repositories).

## Setup (about 10 minutes)

### 1. Discord webhook

1. In your Discord server, pick (or create) a channel for alerts.
2. Channel settings (gear icon) → **Integrations** → **Webhooks** → **New Webhook**.
3. Click the new webhook → **Copy Webhook URL**. Keep it private.
4. On your phone, set that channel's notifications to **All Messages** so alerts buzz.

### 2. GitHub repository

1. Sign in to GitHub → **New repository** → name it e.g. `museum-watcher` →
   **Public** → tick **Add a README** → **Create repository**.
   (Private works too, but it would use up most of the free 2,000 Actions
   minutes a month, because each run waits a few minutes for :00.)
2. **Add file → Upload files** → drag in `watcher.py` and `requirements.txt` →
   **Commit changes**.
3. **Add file → Create new file** → type the name
   `.github/workflows/watch.yml` (the slashes create the folders) → paste in the
   contents of `watch.yml` → **Commit changes**.

### 3. Give it the webhook

Repository **Settings → Secrets and variables → Actions → New repository
secret**

- Name: `DISCORD_WEBHOOK_URL`
- Secret: the webhook URL from step 1

### 4. Test it

**Actions** tab → (enable workflows if asked) → **Nintendo Museum watcher** →
**Run workflow** → leave "Test run" ticked → **Run workflow**.

Within about 2 minutes, Discord gets a test message with a screenshot of the
calendar and the dates it read as open. Compare it with the real calendar.

- **They match:** you're done. Hourly checks start on their own.
- **They don't, or it says it couldn't read the calendar:** open the run in the
  Actions tab, download the `calendar-…` file under **Artifacts**, and send it
  to Claude to adjust the parser.

## What you'll get in Discord

- **@everyone alert** when a date newly opens, with the link and a screenshot.
  Dates that stay open aren't repeated; if one sells out and opens again, you
  get a new alert.
- **Daily check-in at 9:00** listing what's open, so you know it's alive.
- **A warning** if it can't read the calendar (site change, maintenance,
  blocked), and a message when it recovers.

## Stopping it

**Actions → Nintendo Museum watcher → ⋯ → Disable workflow.** It also goes
quiet on its own after November.

## Settings

Edit these in `.github/workflows/watch.yml`, under `env:`:

| Setting | Default | Meaning |
|---|---|---|
| `WATCH_MONTHS` | `2026-10,2026-11` | Months to watch |
| `HEARTBEAT_HOUR_JST` | `9` | Hour of the daily check-in |

Set `PING_EVERYONE: "0"` there to drop the @everyone mention.

## Notes

- GitHub can start scheduled jobs late or occasionally skip one. The job
  starts at :47 and waits for :00 to absorb delays, and a backup run at :53
  covers skipped ones.
- GitHub turns off scheduled workflows in repositories with no activity for
  60 days. That won't matter for a few weeks.
