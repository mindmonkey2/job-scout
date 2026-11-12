#!/usr/bin/env python3
"""Job Scout: finds fresh Freelancer.com jobs that fit, drafts bids, and alerts you.

It never submits a bid or messages a client. You review each draft and submit it yourself.

  scout.py scan [--draft] [--notify] [-v]     one pass
  scout.py loop [--every 15] [--draft] [--notify]
  scout.py list [--all]                        recent matches
  scout.py open <project_id>                   open the job page, copy the proposal
  scout.py draft <project_id>                  (re)draft one job
  scout.py paste [--platform upwork] [--url URL]   draft from job text you copied (Upwork etc.)
"""
import argparse
import json
import re
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
CFG = json.loads((HERE / "config.json").read_text())
if (HERE / "config.local.json").exists():  # personal overrides, git-ignored
    CFG.update(json.loads((HERE / "config.local.json").read_text()))
DB_PATH = HERE / "scout.db"
FACTS_PATH = HERE / "my_profile.md"  # your verified facts, git-ignored


def _path(value):
    path = Path(value).expanduser()
    return path if path.is_absolute() else HERE / path


BIDS_DIR = _path(CFG["bids_dir"])
API = "https://www.freelancer.com/api/projects/0.1/projects/"
HEADERS = {"User-Agent": "job-scout (personal job alerts)"}


# ---------- fetching ----------

def _get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=30) as r:
        return json.load(r)["result"]["projects"]


def fetch_active(query, ptype):
    params = {"limit": 100, "query": query, "sort_field": "submitdate",
              "project_types[]": ptype, "full_description": "true", "job_details": "true"}
    return _get(API + "active/?" + urllib.parse.urlencode(params))


def fetch_by_id(pid):
    params = {"projects[]": pid, "full_description": "true", "job_details": "true"}
    found = _get(API + "?" + urllib.parse.urlencode(params))
    return found[0] if found else None


def fetch_all():
    jobs = {}
    for q in CFG["queries"]:
        for ptype in ("fixed", "hourly"):
            try:
                for p in fetch_active(q, ptype):
                    jobs[p["id"]] = p
            except Exception as e:  # one failed query shouldn't stop the scan
                print(f"warn: query '{q}' ({ptype}) failed: {e}", file=sys.stderr)
            time.sleep(CFG["request_pause_s"])
    return list(jobs.values())


# ---------- filtering and scoring ----------

def to_usd(p, amount):
    return (amount or 0) * (p["currency"].get("exchange_rate") or 1)


def evaluate(p, now):
    """Return (score, info) for a job that passes the filters, or (None, reason)."""
    title = p["title"].lower()
    desc = (p.get("description") or "").lower()
    if p.get("status") != "active":
        return None, "not active"
    if p.get("language", "en") not in CFG["languages"]:
        return None, f"language {p.get('language')}"
    for pat in CFG["title_reject"]:
        if re.search(pat, title):
            return None, f"title red flag /{pat}/"
    for pat in CFG["text_reject"]:
        if re.search(pat, title + " " + desc):
            return None, f"red flag /{pat}/"

    age = (now - p["submitdate"]) / 60
    bids = p["bid_stats"]["bid_count"]
    if age > CFG["max_age_min"]:
        return None, "too old"
    if bids > CFG["max_bids"]:
        return None, f"{bids} bids"

    skills = {j["name"].lower() for j in p.get("jobs") or []}
    strong = sorted(skills & set(CFG["strong_skills"]))
    weak = sorted(skills & set(CFG["weak_skills"]))
    if not strong and len(weak) < 2:
        return None, "skills don't match"

    budget = p["budget"]
    lo = to_usd(p, budget.get("minimum"))
    hi = to_usd(p, budget.get("maximum") or budget.get("minimum"))
    hourly = p["type"] == "hourly"
    if hourly and hi < CFG["min_hourly_usd"]:
        return None, "hourly rate too low"
    if not hourly and hi < CFG["min_fixed_usd"]:
        return None, "budget too small after fees"

    score = 30 if age <= 15 else 22 if age <= 30 else 12 if age <= 60 else 4
    score += 30 if bids <= 5 else 22 if bids <= 10 else 10 if bids <= 20 else 3
    score += min(24, 8 * len(strong) + 3 * len(weak))
    score += 10 if hourly else (12 if 50 <= hi <= 1000 else 6)
    score += 4 if len(desc) >= 400 else 0

    cur = p["currency"]["code"]
    unit = "/hr" if hourly else ""
    info = {
        "age_min": round(age), "bids": bids, "skills": strong + weak,
        "budget": f"{cur} {(budget.get('minimum') or 0):g}-{(budget.get('maximum') or budget.get('minimum') or 0):g}{unit} (~${lo:.0f}-{hi:.0f}{unit})",
        "url": f"https://www.freelancer.com/projects/{p['seo_url']}/details",
    }
    return score, info


# ---------- storage ----------

def db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY, found_at TEXT, score INTEGER, title TEXT, url TEXT,
        budget TEXT, bids_when_found INTEGER, age_when_found INTEGER,
        status TEXT DEFAULT 'new', draft_path TEXT)""")
    return con


# ---------- drafting ----------

def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:50] or "job"


def job_summary(p, info):
    return json.dumps({
        "title": p["title"], "type": p["type"], "budget": info["budget"],
        "bids_so_far": info["bids"], "minutes_since_posted": info["age_min"],
        "skills": [j["name"] for j in p.get("jobs") or []],
        "description": p.get("description") or "",
    }, ensure_ascii=False, indent=2)


PLATFORMS = {
    "freelancer": {
        "name": "Freelancer.com",
        "notes": "- Freelancer charges about 10% or a $5 minimum on fixed jobs; mention when the fee eats a big share.\n"
                 "- The proposal box shows the whole text, but clients skim: the first line must carry the pitch.",
        "bid_extra": "",
        "extra_sections": "",
        "check": 'Open the page: payment verified? client account age and history? any "$20 balance" or language lock?',
    },
    "upwork": {
        "name": "Upwork",
        "notes": "- This is a cover letter. Clients see only the first one or two lines in the proposal list before clicking, "
                 "so the first sentence must name their problem and your approach. No \"Hi, I read your job post\".\n"
                 "- Upwork deducts a service fee from earnings (the bid form shows the exact amount); price with that in mind.\n"
                 "- Each proposal costs Connects. Say whether the job is worth them for a first review, judged on clarity, "
                 "budget fit and client signals visible in the pasted text (payment verified, spend, hires, reviews).\n"
                 "- For fixed-price jobs, suggest milestones if the work has natural stages.\n"
                 "- If the pasted text includes screening questions, answer each one in the Screening answers section, "
                 "briefly and with the same honesty rules. If there are none, write \"- None in the pasted text\".",
        "bid_extra": "- Connects: worth spending on this job? (yes/no and one reason)",
        "extra_sections": "## Screening answers\n\n(one short answer per question, numbered as the client numbered them)\n\n",
        "check": "On the job page: payment verified? client spend, hire rate and reviews? Connects cost? Is the job still open?",
    },
}


def run_claude(prompt, label):
    """Draft with claude -p. All tools are disabled: job text is untrusted, so the model must not read files."""
    cmd = ["claude", "-p", "--output-format", "text", "--tools", "", "--strict-mcp-config",
           "--no-session-persistence"]
    if CFG.get("claude_model"):
        cmd += ["--model", CFG["claude_model"]]
    try:
        res = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                             timeout=CFG["draft_timeout_s"], cwd=HERE)
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"warn: drafting {label} failed: {e}", file=sys.stderr)
        return None
    body = res.stdout.strip()
    if res.returncode != 0 or "## Proposal" not in body:
        print(f"warn: drafting {label} returned no usable draft (exit {res.returncode}): "
              f"{(res.stderr or body)[:300]}", file=sys.stderr)
        return None
    return body


def build_prompt(job_text, platform):
    pf = PLATFORMS[platform]
    if not FACTS_PATH.exists():
        sys.exit("No my_profile.md yet. Copy profile_facts.example.md to my_profile.md and fill in "
                 "facts you can prove; the drafter may only claim what is in that file.")
    example_path = _path(CFG["example_bid"]) if CFG.get("example_bid") else None
    example = example_path.read_text() if example_path and example_path.exists() else "(none)"
    fills = {
        "{platform}": pf["name"], "{platform_notes}": pf["notes"], "{bid_extra}": pf["bid_extra"],
        "{extra_sections}": pf["extra_sections"], "{check_line}": pf["check"],
        "{example}": example, "{facts}": FACTS_PATH.read_text(),
    }
    prompt = (HERE / "draft_prompt.md").read_text()
    for key, value in fills.items():
        prompt = prompt.replace(key, value)
    return prompt.replace("{job}", job_text)  # last, so text inside the job can't trigger replacements


def draft(p, info, score):
    body = run_claude(build_prompt(job_summary(p, info), "freelancer"), p["id"])
    if not body:
        return None

    header = "\n".join([
        f"# {p['title']}", "",
        f"- Job: {info['url']} (ID {p['id']})",
        f"- Pay: {info['budget']}, {p['type']}",
        f"- Found: {datetime.now():%Y-%m-%d %H:%M}, {info['age_min']} min after posting, {info['bids']} bids",
        f"- Scout score: {score}",
        "- Status: draft by job-scout, not sent. Review every line before submitting.", "",
    ])
    path = BIDS_DIR / f"scout-{p['id']}-{slugify(p['title'])}.md"
    path.write_text(header + "\n" + body + "\n")
    return path


def proposal_text(path):
    text = Path(path).read_text()
    m = re.search(r"^## Proposal\s*\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    return m.group(1).strip() if m else None


# ---------- alerts ----------

def notify(title, body):
    try:
        subprocess.run(["notify-send", "-a", "Job Scout", "-u", "critical", title, body],
                       timeout=10, check=False)
    except OSError:
        pass


# ---------- commands ----------

def scan(args):
    now = time.time()
    jobs = fetch_all()
    con = db()
    seen = {r[0] for r in con.execute("SELECT id FROM jobs")}
    matches, rejected = [], {}
    for p in jobs:
        score, info = evaluate(p, now)
        if score is None:
            rejected[info] = rejected.get(info, 0) + 1
            continue
        if score >= CFG["score_threshold"]:
            matches.append((score, p, info))
    matches.sort(key=lambda m: -m[0])

    new = [m for m in matches if m[1]["id"] not in seen]
    print(f"{datetime.now():%H:%M} scanned {len(jobs)} jobs: {len(matches)} match, {len(new)} new")
    if args.verbose:
        for reason, n in sorted(rejected.items(), key=lambda r: -r[1])[:12]:
            print(f"   skipped {n:4d}  {reason}")

    for score, p, info in matches:
        tag = "NEW " if p["id"] not in seen else "    "
        print(f"{tag}[{score}] {p['title'][:70]}\n      {info['budget']} | {info['bids']} bids | "
              f"{info['age_min']} min old | {', '.join(info['skills'][:5])}\n      {info['url']}")

    for score, p, info in new:
        con.execute("INSERT INTO jobs (id, found_at, score, title, url, budget, bids_when_found, age_when_found)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (p["id"], datetime.now().isoformat(timespec="seconds"), score, p["title"],
                     info["url"], info["budget"], info["bids"], info["age_min"]))
        con.commit()
        path = None
        if args.draft:
            path = draft(p, info, score)
            if path:
                con.execute("UPDATE jobs SET status='drafted', draft_path=? WHERE id=?", (str(path), p["id"]))
                con.commit()
                print(f"      drafted: {path}")
        if args.notify:
            what = "Draft ready" if path else "New match"
            notify(f"{what} [{score}]: {p['title'][:60]}",
                   f"{info['budget']} · {info['bids']} bids · {info['age_min']} min old\n"
                   f"scout.py open {p['id']}")
    con.close()


def loop(args):
    print(f"Scanning every {args.every} min. Ctrl+C to stop.")
    while True:
        try:
            scan(args)
        except Exception as e:  # keep the loop alive through network errors
            print(f"warn: scan failed: {e}", file=sys.stderr)
        time.sleep(args.every * 60)


def list_jobs(args):
    con = db()
    q = "SELECT id, found_at, score, status, title, budget, bids_when_found, url, draft_path FROM jobs"
    q += " ORDER BY found_at DESC" + ("" if args.all else " LIMIT 20")
    for pid, found, score, status, title, budget, bids, url, path in con.execute(q):
        print(f"{found[5:16]} [{score}] {status:8} {pid} {title[:60]}\n      {budget} | {bids} bids when found | {url}"
              + (f"\n      {path}" if path else ""))


def open_job(args):
    con = db()
    row = con.execute("SELECT url, draft_path FROM jobs WHERE id=?", (args.id,)).fetchone()
    if not row:
        sys.exit(f"{args.id} is not in the scout database; run a scan first.")
    url, path = row
    live = fetch_by_id(args.id)
    if live:
        print(f"now: {live['status']} / {live.get('sub_status') or '-'}, {live['bid_stats']['bid_count']} bids")
        if live["status"] != "active":
            print("This job is no longer open.")
    text = proposal_text(path) if path else None
    if text:
        try:
            subprocess.run(["wl-copy"], input=text, text=True, check=True, timeout=10)
            print(f"Proposal copied to clipboard ({len(text)} characters). Review it before submitting.")
        except (OSError, subprocess.SubprocessError):
            print("Could not copy to clipboard. Proposal:\n\n" + text)
    else:
        print("No draft for this job yet; run: scout.py draft", args.id)
    subprocess.run(["xdg-open", url], check=False)
    con.execute("UPDATE jobs SET status='opened' WHERE id=?", (args.id,))
    con.commit()


def draft_one(args):
    p = fetch_by_id(args.id)
    if not p:
        sys.exit(f"Project {args.id} not found.")
    score, info = evaluate(p, time.time())
    if score is None:
        print(f"Note: this job fails the filters ({info}); drafting anyway.")
        score = 0
        b = p["budget"]
        info = {"age_min": round((time.time() - p["submitdate"]) / 60), "bids": p["bid_stats"]["bid_count"],
                "skills": [j["name"] for j in p.get("jobs") or []],
                "budget": f"{p['currency']['code']} {b.get('minimum')}-{b.get('maximum')}"
                          + ("/hr" if p["type"] == "hourly" else ""),
                "url": f"https://www.freelancer.com/projects/{p['seo_url']}/details"}
    path = draft(p, info, score)
    if not path:
        sys.exit("Drafting failed.")
    con = db()
    con.execute("INSERT OR IGNORE INTO jobs (id, found_at, score, title, url, budget, bids_when_found, age_when_found)"
                " VALUES (?,?,?,?,?,?,?,?)", (p["id"], datetime.now().isoformat(timespec="seconds"), score,
                                              p["title"], info["url"], info["budget"], info["bids"], info["age_min"]))
    con.execute("UPDATE jobs SET status='drafted', draft_path=? WHERE id=?", (str(path), p["id"]))
    con.commit()
    print(f"Drafted: {path}")


SKIP_TITLE = re.compile(r"^(posted|save job|flag as|worldwide|job details|apply now|\d+ (minutes?|hours?|days?) ago)", re.I)


def section(text, name):
    m = re.search(rf"^## {re.escape(name)}\s*\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    return m.group(1).strip() if m else None


def read_paste(args):
    if args.file:
        return Path(args.file).expanduser().read_text()
    if not sys.stdin.isatty():
        return sys.stdin.read()
    try:
        return subprocess.run(["wl-paste", "--no-newline"], capture_output=True, text=True,
                              check=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        sys.exit("Could not read the clipboard. Pipe the text in or use --file.")


def paste(args):
    """Draft a bid from job text copied out of Upwork (or anywhere else)."""
    text = read_paste(args).strip()
    if len(text) < 200:
        sys.exit(f"Only {len(text)} characters pasted. Copy the whole job post (title, description, "
                 "budget, client info and any questions), then run this again.")
    limit = 20000
    if len(text) > limit:
        print(f"Note: job text is {len(text)} characters; using the first {limit}.")
        text = text[:limit]

    title = args.title or next((ln.strip() for ln in text.splitlines()
                                if ln.strip() and not SKIP_TITLE.match(ln.strip())), "Pasted job")[:120]
    print(f"Drafting a bid for: {title} ({PLATFORMS[args.platform]['name']}, about 30 seconds)")
    body = run_claude(build_prompt(text, args.platform), title)
    if not body:
        sys.exit("Drafting failed.")

    path = BIDS_DIR / f"{args.platform}-{slugify(title)}.md"
    n = 2
    while path.exists():
        path = BIDS_DIR / f"{args.platform}-{slugify(title)}-{n}.md"
        n += 1
    header = "\n".join([
        f"# {title}", "",
        f"- Job: {args.url or '(pasted text; add the link here)'}",
        f"- Platform: {PLATFORMS[args.platform]['name']}",
        f"- Drafted: {datetime.now():%Y-%m-%d %H:%M} from pasted text",
        "- Status: draft by job-scout paste mode, not sent. Review every line before submitting.", "",
    ])
    original = "\n\n## Original job text\n\n````text\n" + text.replace("````", "```") + "\n````\n"
    path.write_text(header + "\n" + body + "\n" + original)

    for name in ("Suggested bid", "Risks", "Screening answers"):
        part = section(body, name)
        if part and part != "- None in the pasted text":
            print(f"\n{name}:\n{part}")
    proposal = section(body, "Proposal")
    if proposal and not args.no_copy:
        try:
            subprocess.run(["wl-copy"], input=proposal, text=True, check=True, timeout=10)
            print(f"\nProposal ({len(proposal)} characters) copied to the clipboard. Read it before pasting.")
        except (OSError, subprocess.SubprocessError):
            print("\nProposal:\n\n" + proposal)
    print(f"Full draft: {path}")


def main():
    BIDS_DIR.mkdir(parents=True, exist_ok=True)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("scan", "loop"):
        sp = sub.add_parser(name)
        sp.add_argument("--draft", action="store_true", help="draft a bid for each new match with claude -p")
        sp.add_argument("--notify", action="store_true", help="desktop alert for each new match")
        sp.add_argument("-v", "--verbose", action="store_true", help="show why jobs were skipped")
        if name == "loop":
            sp.add_argument("--every", type=int, default=15, help="minutes between scans")
    sp = sub.add_parser("list")
    sp.add_argument("--all", action="store_true")
    for name in ("open", "draft"):
        sp = sub.add_parser(name)
        sp.add_argument("id", type=int)
    sp = sub.add_parser("paste", help="draft from job text on the clipboard, a file, or stdin")
    sp.add_argument("--platform", choices=sorted(PLATFORMS), default="upwork")
    sp.add_argument("--file", help="read the job text from this file instead of the clipboard")
    sp.add_argument("--url", help="job link, saved in the draft header")
    sp.add_argument("--title", help="override the title taken from the first line")
    sp.add_argument("--no-copy", action="store_true", help="don't copy the proposal to the clipboard")
    args = ap.parse_args()
    {"scan": scan, "loop": loop, "list": list_jobs, "open": open_job, "draft": draft_one,
     "paste": paste}[args.cmd](args)


if __name__ == "__main__":
    main()
