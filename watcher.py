#!/usr/bin/env python3
"""Nintendo Museum cancellation watcher.

Loads the public ticket calendar (logged out) at the top of the hour and posts
to a Discord webhook when a watched date shows the ○ mark (first-come tickets
on sale). It only reads the calendar: it never logs in, never opens a date,
and never buys anything. Buying is done by you, by hand.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
import traceback
from pathlib import Path

import requests
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

JST = dt.timezone(dt.timedelta(hours=9))
SITE = "https://museum-tickets.nintendo.com"
CALENDAR_URL = os.environ.get("CALENDAR_URL", f"{SITE}/calendar")
BUY_URL = os.environ.get("BUY_URL", f"{SITE}/calendar")
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
WATCH_MONTHS = sorted(
    {m.strip() for m in os.environ.get("WATCH_MONTHS", "2026-10,2026-11").split(",") if m.strip()}
)
MODE = (os.environ.get("MODE") or "scheduled").strip()  # "scheduled" or "test"
WAIT_FOR_HOUR = os.environ.get("WAIT_FOR_HOUR", "0") == "1"
HEARTBEAT_HOUR = int(os.environ.get("HEARTBEAT_HOUR_JST", "9"))
PING_EVERYONE = os.environ.get("PING_EVERYONE", "1") == "1"
STATE_FILE = Path(os.environ.get("STATE_FILE", "state/state.json"))
OUT_DIR = Path(os.environ.get("OUT_DIR", "out"))


def log(*args):
    print(f"[{dt.datetime.now(JST):%H:%M:%S} JST]", *args, flush=True)


# --------------------------------------------------------------------------
# In-page scripts
# --------------------------------------------------------------------------

# Finds every ○ / − / ◇ calendar icon (img, svg <use>, CSS background or mask),
# works out which day cell it sits in and which month that cell belongs to.
EXTRACT_JS = r"""
() => {
  const RE = { circle: /calendar[_-]?circle/i, hyphen: /calendar[_-]?hyphen/i, diamond: /calendar[_-]?diamond/i };
  const kindOf = (...vals) => {
    for (const v of vals) {
      if (!v || typeof v !== 'string') continue;
      for (const k in RE) if (RE[k].test(v)) return k;
    }
    return null;
  };
  const clean = (t) => (t || '').replace(/\s+/g, ' ').trim();
  const isVisible = (el) => el.getClientRects().length > 0;

  // 1) icons
  const hits = [];
  for (const el of document.querySelectorAll('body *')) {
    const tag = el.tagName.toLowerCase();
    if (tag === 'script' || tag === 'style' || tag === 'noscript') continue;
    let k = kindOf(el.getAttribute('src'), el.getAttribute('srcset'), el.getAttribute('data-src'),
                   el.getAttribute('href'), el.getAttribute('xlink:href'), el.currentSrc);
    if (!k) {
      for (const p of [null, '::before', '::after']) {
        const cs = getComputedStyle(el, p);
        k = kindOf(cs.backgroundImage, cs.maskImage, cs.webkitMaskImage, cs.content, cs.listStyleImage);
        if (k) break;
      }
    }
    if (k) hits.push([el, k]);
  }

  // 2) month headers ("2026年11月", "2026.11", "November 2026", "11月")
  const EN = ['january','february','march','april','may','june','july','august','september','october','november','december'];
  const monthFromText = (t) => {
    t = clean(t);
    let m = t.match(/(20\d\d)\s*年\s*(\d{1,2})\s*月(?!\s*(?:分|\d))/);
    if (m) return [+m[1], +m[2]];
    m = t.match(/(?:^|[^\d])(20\d\d)\s*[\/.]\s*(\d{1,2})(?![\d\/.])/);
    if (m) return [+m[1], +m[2]];
    m = t.toLowerCase().match(/(january|february|march|april|may|june|july|august|september|october|november|december)\s*,?\s*(20\d\d)/);
    if (m) return [+m[2], EN.indexOf(m[1]) + 1];
    m = t.match(/^(\d{1,2})\s*月$/);
    if (m) return [null, +m[1]];
    return null;
  };
  const headers = [];
  for (const el of document.querySelectorAll('body *')) {
    const tag = el.tagName.toLowerCase();
    if (tag === 'script' || tag === 'style' || tag === 'option') continue;
    const t = clean(el.innerText || el.textContent);
    if (!t || t.length > 20) continue;
    const ym = monthFromText(t);
    if (ym && ym[1] >= 1 && ym[1] <= 12) headers.push({ el, y: ym[0], m: ym[1], text: t });
  }
  const activeish = (el) => {
    for (let c = el, i = 0; c && i < 3; c = c.parentElement, i++) {
      const cls = c.getAttribute && (c.getAttribute('class') || '');
      if (/(^|[-_\s])(active|current|selected|is-on|on)([-_\s]|$)/i.test(cls) || c.getAttribute && c.getAttribute('aria-selected') === 'true') return true;
    }
    return false;
  };
  const monthFor = (cell) => {
    for (let cur = cell.parentElement; cur && cur !== document.documentElement; cur = cur.parentElement) {
      const inside = headers.filter(h => cur.contains(h.el) && !h.el.contains(cell));
      if (!inside.length) continue;
      const before = inside.filter(h => h.el.compareDocumentPosition(cell) & Node.DOCUMENT_POSITION_FOLLOWING);
      const pool = before.length ? before : inside;
      const act = pool.filter(h => activeish(h.el));
      const pick = act.length ? act[act.length - 1] : (before.length ? before[before.length - 1] : inside[0]);
      return pick;
    }
    return null;
  };

  // 3) day cell for each icon
  const findCell = (el) => {
    let cur = el;
    for (let i = 0; i < 7 && cur && cur !== document.body; i++, cur = cur.parentElement) {
      for (const a of (cur.attributes || [])) {
        if (['src', 'srcset', 'style', 'class', 'data-src'].includes(a.name)) continue;
        const m = a.value.match(/(20\d\d)[-\/.](\d{1,2})[-\/.](\d{1,2})(?!\d)/) ||
                  a.value.match(/(?:^|[^\d])(20\d\d)(\d\d)(\d\d)(?:[^\d]|$)/);
        if (m && +m[2] >= 1 && +m[2] <= 12 && +m[3] >= 1 && +m[3] <= 31)
          return { cell: cur, y: +m[1], m: +m[2], d: +m[3], how: 'attr:' + a.name };
      }
      const t = clean(cur.innerText || cur.textContent);
      if (t && t.length <= 16) {
        const m = t.match(/^(?:(?:[日月火水木金土]|mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?\s*)?(?:(\d{1,2})\s*[\/月]\s*)?(\d{1,2})(?!\d)/i);
        if (m && +m[2] >= 1 && +m[2] <= 31) return { cell: cur, m: m[1] ? +m[1] : null, d: +m[2], how: 'text' };
      }
    }
    return null;
  };

  const marks = [];
  const cells = [];
  let legend = 0;
  for (const [el, kind] of hits) {
    const c = findCell(el);
    if (!c) { legend++; continue; }
    let y = c.y || null, m = c.m || null, monthHow = c.y ? 'attr' : null;
    if (!y || !m) {
      const h = monthFor(c.cell);
      if (h) { if (!m) m = h.m; if (!y) y = h.y; monthHow = 'header:' + h.text; }
    }
    marks.push({ kind, y, m, d: c.d, how: c.how, monthHow, visible: isVisible(el),
                 cellText: clean(c.cell.innerText || c.cell.textContent).slice(0, 30) });
    cells.push(c.cell);
  }

  // 4) remember the calendar's area for the month-navigation step
  document.querySelectorAll('[data-watch-root]').forEach(e => e.removeAttribute('data-watch-root'));
  let root = null;
  if (cells.length) {
    root = cells[0];
    while (root && !cells.every(c => root.contains(c))) root = root.parentElement;
    if (root && root !== document.body) {
      let area = root;
      for (let i = 0; i < 2 && area.parentElement && area.parentElement !== document.body; i++) area = area.parentElement;
      area.setAttribute('data-watch-root', '1');
    }
  }
  return { marks, legend, iconCount: hits.length,
           headers: headers.map(h => h.text).slice(0, 30),
           title: document.title, bodyStart: clean(document.body ? document.body.innerText : '').slice(0, 400) };
}
"""

# Finds the tab for one month ("10月 販売中", "11月 販売中", ...) and tags it so
# Python can click it. Never picks a date cell or a link that leaves the calendar.
FIND_TAB_JS = r"""
(mo) => {
  document.querySelectorAll('[data-watch-tab]').forEach(e => e.removeAttribute('data-watch-tab'));
  const clean = (t) => (t || '').replace(/\s+/g, ' ').trim();
  const re = new RegExp('^' + mo + '\\s*月(?!\\s*(?:分|\\d))');
  const isActive = (el) => {
    for (let c = el, i = 0; c && i < 2; c = c.parentElement, i++) {
      const cls = c.getAttribute('class') || '';
      if (/(^|[-_\s])(active|current|selected|is-on|on)([-_\s]|$)/i.test(cls) ||
          c.getAttribute('aria-selected') === 'true' || c.getAttribute('aria-current')) return true;
    }
    return false;
  };
  const scopes = [document.querySelector('[data-watch-root]'), document.body].filter(Boolean);
  for (const scope of scopes) {
    let best = null;
    for (const el of scope.querySelectorAll('*')) {
      if (['script', 'style', 'option', 'select'].includes(el.tagName.toLowerCase())) continue;
      if (!el.getClientRects().length) continue;
      const t = clean(el.innerText);
      if (!t || t.length > 12 || !re.test(t)) continue;
      if (el.querySelector('img[src*="calendar_"]')) continue;        // a date cell
      const a = el.closest('a');
      if (a) {
        const h = a.getAttribute('href') || '';
        if (h && !/^(#|javascript:)/i.test(h) && !/calendar/i.test(h)) continue;
      }
      if (!best || el.contains(best)) best = el;                      // keep the whole tab
    }
    if (best) {
      best.setAttribute('data-watch-tab', '1');
      return { text: clean(best.innerText), active: isActive(best) };
    }
  }
  return null;
}
"""

# A fingerprint of the calendar's day cells, to confirm a tab click changed the month.
SIG_JS = r"""() => [...document.querySelectorAll('img[src*="calendar_"]')]
  .map(i => { const c = i.closest('td, li, button, a') || i.parentElement || i;
              return (c.innerText || '').trim() + ':' + (i.getAttribute('src') || '').split('/').pop(); })
  .join('|')"""

WAIT_ICONS_JS = """() => document.querySelectorAll(
  'img[src*="calendar_circle"], img[src*="calendar_hyphen"], img[src*="calendar_diamond"]').length > 3"""


# --------------------------------------------------------------------------
# Reading the calendar
# --------------------------------------------------------------------------

def settle(page, quick=False):
    try:
        page.wait_for_load_state("networkidle", timeout=6000 if quick else 15000)
    except PWTimeout:
        pass
    try:
        page.wait_for_function(WAIT_ICONS_JS, timeout=4000 if quick else 12000)
    except PWTimeout:
        pass
    # trigger lazy-loaded images, then go back to the top
    page.evaluate("window.scrollTo(0, document.body ? document.body.scrollHeight : 0)")
    page.wait_for_timeout(700)
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(500)


SHOT_AREA_JS = r"""() => {
  document.querySelectorAll('[data-watch-shot]').forEach(e => e.removeAttribute('data-watch-shot'));
  const a = document.querySelector('[data-watch-root]'), t = document.querySelector('[data-watch-tab]');
  if (!a) return false;
  let c = a;
  if (t) { while (c && !c.contains(t)) c = c.parentElement; }      // widen to include the month tabs
  if (!c || c === document.body || c === document.documentElement) c = a;
  c.setAttribute('data-watch-shot', '1');
  return true;
}"""


def snap(page, name, month=None):
    """Screenshot of the calendar (with its month tabs) when we know where it is, else the whole page."""
    path = OUT_DIR / f"{name}.jpg"
    try:
        if month:
            page.evaluate(FIND_TAB_JS, month)   # re-find the tab: the page may have redrawn it
        page.evaluate(SHOT_AREA_JS)
        area = page.locator("[data-watch-shot]").first
        if area.count():
            area.screenshot(path=str(path), type="jpeg", quality=75)
            return path
    except Exception:
        pass
    page.screenshot(path=str(path), full_page=True, type="jpeg", quality=70)
    return path


def wanted_months():
    """Watched months that aren't over yet, e.g. ['2026-10', '2026-11']."""
    cur = dt.datetime.now(JST).strftime("%Y-%m")
    return [m for m in WATCH_MONTHS if m >= cur]


def on_site(url):
    return any(h in url for h in ("museum-tickets.nintendo.com", "localhost", "127.0.0.1"))


def open_month(page, ym):
    """Click the month's tab and wait until the calendar shows it. Returns (ok, why_not)."""
    mo = int(ym[5:])
    tab = page.evaluate(FIND_TAB_JS, mo)
    if not tab:
        return False, f"couldn't find the {mo}月 tab"
    if tab["active"]:
        return True, None
    before = page.evaluate(SIG_JS)
    log(f"opening the {tab['text']} tab")
    page.click('[data-watch-tab="1"]', timeout=5000)
    changed = True
    try:
        page.wait_for_function(f"(prev) => ({SIG_JS})() !== prev", arg=before, timeout=10000)
    except PWTimeout:
        changed = False
    settle(page, quick=True)
    if not on_site(page.url):
        return False, f"clicking the {mo}月 tab left the calendar ({page.url[:80]})"
    if not changed:
        again = page.evaluate(FIND_TAB_JS, mo)
        if not (again and again["active"]):
            return False, f"the {mo}月 tab didn't open"
    return True, None


def read_calendar(wait_until_hour=False):
    """Returns dict(marks, shots, problem, warning, info)."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    responses = []
    with sync_playwright() as p:
        # Start the browser first so that at :00 the only thing left is loading the page.
        browser = p.chromium.launch()
        ctx = browser.new_context(locale="ja-JP", timezone_id="Asia/Tokyo",
                                  viewport={"width": 1280, "height": 1600})
        page = ctx.new_page()
        page.on("response", lambda r: responses.append(r))
        if wait_until_hour:
            wait_for_top_of_hour()
        try:
            log("loading the calendar")
            resp = page.goto(CALENDAR_URL, wait_until="domcontentloaded", timeout=45000)
            status = resp.status if resp else None
            settle(page)

            # Whatever month the page opens on (January, right now): only used to
            # locate the calendar and for debugging. It isn't reported.
            first = page.evaluate(EXTRACT_JS)
            (OUT_DIR / "page-default.html").write_text(page.content(), encoding="utf-8")
            default_shot = snap(page, "calendar-default")
            info = {"status": status, "bodyStart": first["bodyStart"], "title": first["title"],
                    "default": {"icons": first["iconCount"], "placed": len(first["marks"]),
                                "headers": first["headers"]},
                    "months": {}}

            shots, all_marks, missing = [], [], []
            for ym in wanted_months():
                ok, why = open_month(page, ym)
                if not ok:
                    log("problem:", why)
                    missing.append(why)
                    if not on_site(page.url):
                        break
                    continue
                y, mo = int(ym[:4]), int(ym[5:])
                data = page.evaluate(EXTRACT_JS)
                marks = []
                for m in data["marks"]:
                    if m["how"].startswith("attr"):          # the page gave a full date
                        if (m["y"], m["m"]) != (y, mo):
                            continue
                    else:                                     # day number only: it's this tab's month
                        m["y"], m["m"] = y, mo
                    m["month"] = ym
                    marks.append(m)
                (OUT_DIR / f"page-{ym}.html").write_text(page.content(), encoding="utf-8")
                shots.append(snap(page, f"calendar-{ym}", month=mo))
                info["months"][ym] = {"icons": data["iconCount"], "placed": len(marks)}
                if not marks:
                    missing.append(f"couldn't read any days in the {mo}月 calendar")
                all_marks.extend(marks)

            # keep JSON the page fetched, to help adjust the parser if needed
            for i, r in enumerate(responses[:80]):
                try:
                    if "json" in (r.headers.get("content-type") or ""):
                        (OUT_DIR / f"xhr-{i:02d}.json").write_text(
                            json.dumps({"url": r.url, "status": r.status, "body": r.text()[:200000]},
                                       ensure_ascii=False, indent=1), encoding="utf-8")
                except Exception:
                    pass
            final_url = page.url
        finally:
            browser.close()

    (OUT_DIR / "marks.json").write_text(json.dumps({"info": info, "marks": all_marks},
                                                   ensure_ascii=False, indent=1), encoding="utf-8")
    problem = warning = None
    if status and status >= 400:
        problem = f"the site answered HTTP {status} (it may be blocking automated visits)"
    elif "accounts.nintendo.com" in final_url:
        problem = "the calendar now redirects to the Nintendo Account login page"
    elif not all_marks:
        problem = "; ".join(missing) or "couldn't find any dated ○ / − marks on the calendar"
    elif missing:
        warning = "; ".join(missing)
    if problem and not shots:
        shots = [default_shot]
    return {"marks": all_marks, "shots": shots, "problem": problem, "warning": warning, "info": info}


# --------------------------------------------------------------------------
# Interpreting results
# --------------------------------------------------------------------------

def mark_key(m, today):
    """'2026-11-14', or '?-11-14' / '?-?-14' when the month/year couldn't be read."""
    y, mo, d = m.get("y"), m.get("m"), m.get("d")
    if mo and not y:  # e.g. header "11月" with no year
        y = today.year if mo >= today.month - 1 else today.year + 1
    if y and mo:
        try:
            return dt.date(y, mo, d).isoformat()
        except ValueError:
            return None
    return f"?-{mo:02d}-{d:02d}" if mo else f"?-?-{d:02d}"


def summarise(marks):
    today = dt.datetime.now(JST).date()
    open_dates, unclear, sold_out = set(), set(), set()
    for m in marks:
        k = mark_key(m, today)
        if not k:
            continue
        if k.startswith("?"):
            if m["kind"] == "circle":
                unclear.add(k)
            continue
        if k[:7] not in WATCH_MONTHS or k <= today.isoformat():
            continue
        (open_dates if m["kind"] == "circle" else sold_out if m["kind"] == "hyphen" else set()).add(k)
    return sorted(open_dates), sorted(unclear), sorted(sold_out - open_dates)


def label(key):
    if key.startswith("?"):
        _, mo, d = key.split("-")
        return f"day {int(d)}" + (f" of month {int(mo)}" if mo != "?" else "") + " (month unclear, see screenshot)"
    d = dt.date.fromisoformat(key)
    return d.strftime("%a %d %b").replace(" 0", " ")


# --------------------------------------------------------------------------
# Discord + state
# --------------------------------------------------------------------------

def discord(content, files=(), ping=False):
    if not WEBHOOK:
        log("DISCORD_WEBHOOK_URL is not set; message would have been:\n" + content)
        return
    payload = {"content": content[:1990], "allowed_mentions": {"parse": ["everyone"] if ping else []}}
    blobs = [(Path(f).name, Path(f).read_bytes()) for f in list(files)[:4] if f and Path(f).exists()]
    for attempt in range(4):
        parts = [("payload_json", (None, json.dumps(payload), "application/json"))]
        parts += [(f"files[{i}]", (name, data, "image/jpeg")) for i, (name, data) in enumerate(blobs)]
        r = requests.post(WEBHOOK, files=parts, timeout=30)
        if r.status_code == 429:
            time.sleep(float((r.json() or {}).get("retry_after", 2)) + 0.5)
            continue
        if r.status_code == 413 and blobs:  # attachments too big: send text only
            blobs = []
            continue
        r.raise_for_status()
        log("posted to Discord")
        return
    raise RuntimeError("Discord kept rate-limiting the webhook")


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"open": [], "error": None, "last_heartbeat": None, "last_ok": None}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=1))


def wait_for_top_of_hour():
    """Block until :00:01. If GitHub started us late (after :00), go straight away."""
    now = dt.datetime.now(dt.timezone.utc)
    if now.minute < 40:
        log("already past the hour; checking now")
        return
    target = (now + dt.timedelta(hours=1)).replace(minute=0, second=1, microsecond=0)
    log(f"browser ready; waiting {(target - now).total_seconds():.0f}s for {target.astimezone(JST):%H:%M:%S} JST")
    while True:
        left = (target - dt.datetime.now(dt.timezone.utc)).total_seconds()
        if left <= 0:
            break
        time.sleep(min(left, 30))


def bullet(keys):
    return "\n".join(f"• {label(k)}" for k in keys)


def month_lines(result, open_dates, sold_out):
    """One line per watched month: what's open, how many days are sold out."""
    lines = []
    for ym in wanted_months():
        name = dt.date(int(ym[:4]), int(ym[5:]), 1).strftime("%B")
        if ym not in result["info"].get("months", {}):
            lines.append(f"**{name}:** couldn't read this month")
            continue
        o = [k for k in open_dates if k.startswith(ym)]
        s = sum(1 for k in sold_out if k.startswith(ym))
        lines.append(f"**{name}:** " + (f"{len(o)} open — " + ", ".join(label(k) for k in o) if o else "nothing open")
                     + f" · {s} sold out")
    return lines


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    now = dt.datetime.now(JST)
    if not WEBHOOK and os.environ.get("GITHUB_ACTIONS"):
        print("::error::The DISCORD_WEBHOOK_URL secret is missing. Add it under Settings → Secrets and variables → Actions.")
        return 1
    if MODE != "test" and all(mk < now.strftime("%Y-%m") for mk in WATCH_MONTHS):
        log("all watched months are over; nothing to do (you can disable the workflow)")
        return 0
    state = load_state()
    result = None
    for attempt in range(2):
        try:
            # Waiting happens inside, after the browser is up. Once it's past :00 it returns at once.
            result = read_calendar(wait_until_hour=WAIT_FOR_HOUR and MODE != "test")
        except Exception as e:
            traceback.print_exc()
            result = {"marks": [], "shots": [], "problem": f"the page didn't load ({type(e).__name__}: {str(e)[:160]})",
                      "warning": None, "info": {}}
        if not result["problem"]:
            break
        log("problem:", result["problem"], "— retrying in 20s" if attempt == 0 else "")
        if attempt == 0:
            time.sleep(20)

    now = dt.datetime.now(JST)
    stamp = now.strftime("%a %d %b %H:%M:%S JST")
    problem = result["problem"]
    open_dates, unclear, sold_out = summarise(result["marks"]) if not problem else ([], [], [])
    current = open_dates + unclear
    log("info:", json.dumps(result["info"], ensure_ascii=False))
    log(f"open: {open_dates}  unclear: {unclear}  sold out: {len(sold_out)}  problem: {problem}  warning: {result.get('warning')}")

    if MODE == "test":
        lines = ["🧪 **Watcher test run** (" + stamp + ")"]
        if problem:
            lines.append(f"⚠️ Problem: {problem}")
        else:
            lines.append("Read the calendar ✅" + (f" (but {result['warning']})" if result.get("warning") else ""))
            lines += month_lines(result, open_dates, sold_out)
        lines.append("Compare with the screenshots and the real calendar. If they don't match, download the "
                     "`calendar-…` artifact from this GitHub run and send it to Claude.")
        discord("\n".join(lines), files=result["shots"])
        if not problem:
            state.update(open=current, error=None, warn=result.get("warning"), last_ok=stamp)
        save_state(state)
        return 0

    if problem:
        if not state.get("error"):
            discord(f"⚠️ **Museum watcher can't read the calendar** ({stamp})\n{problem}\n"
                    "It keeps trying every hour and will tell you when it's working again.",
                    files=result["shots"][:2])
            state["error"] = stamp
        elif now.hour == HEARTBEAT_HOUR and state.get("last_heartbeat") != now.date().isoformat():
            discord(f"⚠️ Daily check-in: the watcher still can't read the calendar (since {state['error']}).\n{problem}")
            state["last_heartbeat"] = now.date().isoformat()
        save_state(state)
        return 0

    if state.get("error"):
        discord(f"✅ Museum watcher is reading the calendar again ({stamp}).")
        state["error"] = None

    warning = result.get("warning")
    if warning and state.get("warn") != warning:
        discord(f"⚠️ Museum watcher: {warning} ({stamp}). It's still watching the other month(s).",
                files=result["shots"])
        state["warn"] = warning
    elif not warning and state.get("warn"):
        discord(f"✅ Museum watcher can read every watched month again ({stamp}).")
        state["warn"] = None

    new = [k for k in current if k not in set(state.get("open") or [])]
    if new:
        still = [k for k in current if k not in new]
        msg = [("@everyone " if PING_EVERYONE else "") + "🎟️ **Nintendo Museum tickets just opened up!**",
               bullet(new),
               f"Buy here (first-come, so be quick): {BUY_URL}"]
        if still:
            msg.append("Also still open:\n" + bullet(still))
        msg.append(f"-# checked {stamp}")
        discord("\n".join(msg), files=result["shots"], ping=PING_EVERYONE)

    today = now.date().isoformat()
    if now.hour == HEARTBEAT_HOUR and state.get("last_heartbeat") != today:
        body = ("Open right now:\n" + bullet(current)) if current else "Nothing open right now."
        discord(f"👋 Daily check-in: the museum watcher is running ({stamp}).\n{body}")
        state["last_heartbeat"] = today

    state.update(open=current, last_ok=stamp)
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
