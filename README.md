# Job Scout

**A freelance job radar that finds fresh Freelancer.com jobs, drafts an honest bid for each with Claude, and alerts your desktop. You stay in control: it never submits a bid or messages a client.**

On freelance marketplaces, coding jobs often collect 20+ bids within minutes, and clients mostly read the first few. Job Scout checks the feed every few minutes, filters out spam and poor fits, scores what's left, and has a ready-to-review proposal waiting before most bidders have seen the post.

For Upwork and other sites with no public feed, **paste mode** turns a copied job post into a full draft in about 30 seconds.

```text
$ ./scout.py scan -v
14:02 scanned 52 fresh jobs: 3 match, 2 new
   skipped   31  skills don't match
   skipped    6  title red flag /\bonly\b/
   skipped    4  budget too small after fees
NEW [84] Python script to sync Google Sheets with a REST API
      USD 30-250 (~$30-250) | 4 bids | 9 min old | api, google sheets, python
      https://www.freelancer.com/projects/.../details
```

## Benefits

- **Be early without refreshing all day.** It scans every few minutes and alerts you the moment a job matches; the draft arrives about 30 seconds later in the same notification, with the live bid count. Click it to copy the proposal and open the job page.
- **Know when a job is swarmed.** Popular jobs can collect 100+ bids in minutes. Job Scout keeps watching what it alerted you about and sends "Crowded, probably skip" when a job fills up fast, so you spend your time where a proposal will be read.
- **Skip the junk automatically.** Built-in filters drop scams, "account rental" requests, crypto token spam, gambling, fake reviews, country-locked posts and non-development jobs.
- **Ranked by real winnability.** Each job gets a score out of 100: freshness (30), few bids so far (30), skill match (24), budget fit (12) and a clear description (4). Jobs likely to be swarmed (big budget plus several trending AI tags) lose a few points; jobs needing a specific stack gain a few.
- **Specific proposals, not templates.** Every draft restates the client's actual problem, gives a concrete plan, says how the work will be tested, and asks two useful questions. It also suggests a bid amount and lists the risks.
- **No invented experience.** The drafter may only claim facts you list in `my_profile.md`. Anything unproven appears as a `[placeholder]` for you to confirm or delete, so you never send a claim you can't back up.
- **Safe with untrusted job text.** Job posts can contain prompt injections aimed at AI tools. Drafting runs `claude -p` with **all tools and MCP servers disabled**, and the prompt tells the model to flag such instructions instead of following them.
- **Works beyond Freelancer.** Paste mode handles Upwork posts, including screening-question answers and advice on whether the job is worth spending Connects on.
- **Fast and lightweight.** A full scan takes about 2 seconds and downloads under 1 MB: the API filters by time on the server, and queries run in parallel. One Python file using only the standard library. No API keys, no login and no scraping: it reads Freelancer's public projects API. Your matches are kept in a local SQLite file.
- **Human in the loop by design.** You read every draft, check the job page, and submit by hand. Your account and reputation stay in your hands.

## Requirements

- Python 3.9+
- [Claude Code](https://claude.com/claude-code), signed in (`claude -p` does the drafting)
- For the desktop app: PySide6 (Fedora: `sudo dnf install python3-pyside6`; elsewhere: `pip install PySide6`). The terminal tool doesn't need it.
- Linux desktop tools for the extras: `notify-send` (alerts), `wl-copy`/`wl-paste` (Wayland clipboard), `xdg-open`. Without them, scanning and drafting still work; proposals are printed instead of copied.

## Setup

```bash
git clone https://github.com/mindmonkey2/job-scout.git
cd job-scout
cp profile_facts.example.md my_profile.md     # then fill in facts you can prove
```

Optional: create `config.local.json` to override settings without touching the tracked config:

```json
{
  "bids_dir": "~/freelance/bids",
  "example_bid": "~/freelance/bids/a-bid-i-liked.md",
  "score_threshold": 60
}
```

`example_bid` is one of your own past proposals. The drafter copies its tone, not its content.

## Usage

```bash
./scout.py scan -v                    # one pass; -v shows why jobs were skipped
./scout.py loop --draft --notify      # every scan_every_min (default 5): draft and alert for each new match
./scout.py list                       # recent matches and their drafts
./scout.py open 40814454              # live bid count, copy the proposal, open the job page
./scout.py draft 40814454             # draft (or redraft) one job by ID
./scout.py draft 40814454 --note "shorter, mention my Odoo work"
```

Drafts are saved as Markdown in `bids/` (or your `bids_dir`) with these sections: client requirements, suggested bid, risks, proposal, and a checklist to work through before sending.

### Desktop app

```bash
./scout_gui.py --install     # once: adds Job Scout to your app menu
./scout_gui.py               # or start it from the menu
```

A window and tray icon on top of the same database. The scanner keeps running as the background service, so alerts still arrive when the window is closed.

- **Job list** with score, budget, bid count (updated every scan), age, status and tags such as *crowded*, *likely crowded* and *niche*. Tabs for To review, Opened, Submitted and Skipped, a title filter, and an "Only last 24 h" switch.
- **Job panel**: the draft's requirements, suggested bid, risks and checklist, the live bid count, and the **proposal in an editable box** with a counter that turns red past Freelancer's 1,500 characters. Edits save back into the draft file.
- **Buttons**: Copy proposal & open job (Ctrl+Return or double-click a row), Copy proposal, Mark submitted, Skip, Redraft with an optional note, Open draft file.
- **Paste a job** window for Upwork and other sites.
- **Settings** for scan interval, thresholds, crowding limits and skill lists, saved to `config.local.json`, and an option to start in the tray at login.
- **Scanner controls**: running state and last scan in the status bar, **Stop scanning / Start scanning**, Scan now and the scanner log. The tray icon shows how many fresh matches are waiting.
- **Shut down** (File menu) stops the scanner and closes the app, optionally keeping the scanner from starting at login. Closing the window or Quit leaves the scanner running.
- Desktop alerts get a **Show in app** button that opens the window on that job.

### Paste mode (Upwork and other sites)

1. Open the job and copy the whole post: title, description, budget, client info and any screening questions.
2. Run:

   ```bash
   ./scout.py paste --url https://www.upwork.com/jobs/...      # reads the clipboard
   ```

3. The terminal shows the suggested bid, risks and screening answers, and the proposal is copied to your clipboard.

Options: `--file job.txt` or pipe text in instead of using the clipboard; `--platform freelancer`; `--title "..."` if the first line isn't the title; `--no-copy`.

## Before you submit a draft

The public API doesn't show everything, so check the job page while logged in:

- Client payment verification, account age and history. A client who joined today is a warning sign.
- Any minimum-balance or language lock on the job.
- Every sentence of the proposal, and every `[placeholder]`.

## Tuning (`config.json`)

| Key | What it does |
| --- | --- |
| `queries` | Search terms sent to the API on each scan (one request each, fixed and hourly together) |
| `fetch_workers` | Queries sent at the same time (default 4; keep it small to be polite to the API) |
| `draft_workers` | Drafts written at the same time (default 2) |
| `scan_every_min` | Minutes between scans for `loop` (default 5) |
| `max_age_min`, `max_bids` | Freshness limits |
| `min_fixed_usd`, `min_hourly_usd` | Budget floors, converted to USD |
| `score_threshold` | Higher means fewer, better alerts |
| `strong_skills` / `weak_skills` | A job needs one strong skill or two weak ones |
| `off_skills` | Non-development skills; a job with more of these than matching skills is skipped |
| `skip_local` | Skip on-site jobs that need you in a particular city (default `true`) |
| `crowd_bids`, `crowd_rate_per_min` | When to send "Crowded, probably skip": at this many bids, or when bids arrive this fast after the alert (defaults 30 and 4/min) |
| `hot_skills`, `hot_budget_usd`, `hot_penalty` | Jobs with 3+ of these tags and a budget at or above this lose `hot_penalty` points and are marked "likely crowded" |
| `niche_skills`, `niche_bonus` | Jobs needing any of these specific skills gain `niche_bonus` points |
| `title_reject` / `text_reject` | Regexes for spam and off-topic jobs |
| `claude_model` | Model for drafting (`null` uses your Claude Code default) |

## Run it in the background (systemd)

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/job-scout.service <<'UNIT'
[Unit]
Description=Freelance job scout

[Service]
WorkingDirectory=%h/job-scout
ExecStart=/usr/bin/python3 -I scout.py loop --draft --notify
Restart=on-failure
Environment=PATH=%h/.local/bin:/usr/bin

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload && systemctl --user enable --now job-scout
journalctl --user -u job-scout -f
```

A normal Claude Code sign-in eventually expires. For an unattended service, create a long-lived token with `claude setup-token`. If drafting fails, scans keep running and alerts say "New match" instead of "Draft ready".

Each draft is one `claude -p` call (about 25 seconds) and counts toward your Claude usage.

## How it works

1. **Fetch:** queries `freelancer.com/api/projects/0.1/projects/active` for each search term in parallel, asking only for jobs updated within `max_age_min`. Rate limits and server errors are retried with backoff.
2. **Filter:** drops jobs that are too old, have too many bids, use the wrong language, match a red-flag pattern, pay too little, or don't match your skills.
3. **Score:** ranks the rest, and anything above `score_threshold` counts as a match.
4. **Draft:** builds a prompt from `draft_prompt.md`, your verified facts and the job, then runs `claude -p` with no tools, two jobs at a time. If a draft fails, the next scan tries again while the job still matches.
5. **Alert:** stores the match in `scout.db` and alerts you at once. When the draft is written, the same notification is replaced with the live bid count and an "Open & copy" button.
6. **Watch:** on later scans, earlier matches that are filling fast get a "Crowded, probably skip" alert. This uses data the scan already fetched, so it costs no extra requests.

## License

MIT
