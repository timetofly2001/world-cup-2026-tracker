#!/usr/bin/env python3
"""Build & publish Austin's interactive 2026 World Cup tracker.

Pulls the full FIFA World Cup schedule/results from ESPN's public
`fifa.world` scoreboard feed, renders a self-contained interactive
index.html, and uploads it to the austin-brief-audio S3 bucket via the
centralized upload_to_s3() helper (per global rules).

A STABLE, unguessable URL is achieved by pinning one random hex token
once (stored in config.json) and reusing it via upload_to_s3(token=...),
so each twice-daily run overwrites the same object in place.

Run:  /usr/bin/python3 build_app.py
"""
from __future__ import annotations

import datetime as dt
import json
import os
import secrets
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
# In AWS Lambda the task dir is read-only; outputs go to WC_OUT_DIR (e.g. /tmp).
OUT_DIR = os.environ.get("WC_OUT_DIR", HERE)
# s3_upload.py lives next to this file when bundled in Lambda; locally it's in gmail/.
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.expanduser("~/Documents/assistant/gmail"))
from s3_upload import upload_to_s3  # noqa: E402

CONFIG_PATH = os.path.join(HERE, "config.json")
STATE_PATH = os.path.join(OUT_DIR, "state.json")
URL_PATH = os.path.join(OUT_DIR, "latest_url.txt")
HTML_OUT = os.path.join(OUT_DIR, "index.html")

ESPN = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard?dates={d}"

# Tournament window (group stage start through final). Generous bounds.
TOURNEY_START = dt.date(2026, 6, 11)
TOURNEY_END = dt.date(2026, 7, 19)

ROUND_META = {
    "group-stage": ("Group Stage", 0),
    "round-of-32": ("Round of 32", 1),
    "round-of-16": ("Round of 16", 2),
    "quarterfinals": ("Quarterfinals", 3),
    "semifinals": ("Semifinals", 4),
    "third-place": ("Third-Place Match", 5),
    "final": ("Final", 6),
}


def load_config() -> dict:
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            return json.load(f)
    cfg = {
        "prefix": "worldcup",
        "stem": "tracker-2026",
        # Pinned once: stable but unguessable URL across rebuilds.
        "token": secrets.token_hex(4),
    }
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)
    return cfg


def fetch_day(day: dt.date) -> list:
    url = ESPN.format(d=day.strftime("%Y%m%d"))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 worldcup-tracker"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.load(r)
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[warn] {day}: {e}\n")
        return []
    return data.get("events", []) or []


def parse_event(ev: dict) -> dict | None:
    try:
        comp = ev["competitions"][0]
    except (KeyError, IndexError):
        return None

    slug = (ev.get("season") or {}).get("slug") or ""
    round_name, round_order = ROUND_META.get(slug, (slug.replace("-", " ").title() or "Match", 99))

    # Broadcasts (dedupe, preserve order, English-ish TV names)
    tv: list[str] = []
    for b in comp.get("geoBroadcasts", []) or []:
        name = (b.get("media") or {}).get("shortName")
        if name and name not in tv:
            tv.append(name)

    venue = comp.get("venue") or {}
    addr = venue.get("address") or {}

    status = (comp.get("status") or {}).get("type") or {}

    teams = {"home": None, "away": None}
    for c in comp.get("competitors", []) or []:
        side = c.get("homeAway")
        t = c.get("team") or {}
        if side in teams:
            teams[side] = {
                "name": t.get("displayName") or t.get("name") or "TBD",
                "abbr": t.get("abbreviation") or "",
                "logo": t.get("logo") or "",
                "score": c.get("score"),
                "winner": bool(c.get("winner")),
            }

    if not teams["home"] or not teams["away"]:
        return None

    return {
        "id": ev.get("id"),
        "date": comp.get("date") or ev.get("date"),
        "round": round_name,
        "roundOrder": round_order,
        "state": status.get("state", "pre"),       # pre | in | post
        "statusDetail": status.get("detail", ""),
        "statusShort": status.get("shortDetail", ""),
        "completed": bool(status.get("completed")),
        "venue": venue.get("fullName", "TBD"),
        "city": addr.get("city", ""),
        "country": addr.get("country", ""),
        "tv": tv,
        "home": teams["home"],
        "away": teams["away"],
    }


def collect() -> list:
    seen: dict[str, dict] = {}
    day = TOURNEY_START
    while day <= TOURNEY_END:
        for ev in fetch_day(day):
            m = parse_event(ev)
            if m and m["id"]:
                seen[m["id"]] = m
        day += dt.timedelta(days=1)
    matches = list(seen.values())
    matches.sort(key=lambda m: (m["date"] or "", m["roundOrder"]))
    return matches


def render_html(matches: list, built_at_iso: str) -> str:
    payload = json.dumps(
        {"matches": matches, "builtAt": built_at_iso},
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return HTML_TEMPLATE.replace("/*__DATA__*/", payload)


def main() -> int:
    cfg = load_config()
    matches = collect()
    built_at = dt.datetime.now(dt.timezone.utc).isoformat()

    html = render_html(matches, built_at)
    with open(HTML_OUT, "w", encoding="utf-8") as f:
        f.write(html)

    url = upload_to_s3(
        HTML_OUT,
        prefix=cfg["prefix"],
        stem=cfg["stem"],
        content_type="text/html; charset=utf-8",
        token=cfg["token"],
    )

    with open(URL_PATH, "w") as f:
        f.write(url + "\n")

    decided = sum(1 for m in matches if m["state"] == "post")
    upcoming = sum(1 for m in matches if m["state"] == "pre")
    state = {
        "url": url,
        "builtAt": built_at,
        "matchCount": len(matches),
        "decided": decided,
        "upcoming": upcoming,
    }
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)

    print(f"[ok] {len(matches)} matches ({decided} final, {upcoming} upcoming)")
    print(url)
    return 0


# ---------------------------------------------------------------------------
# Front-end: minimalist, self-contained app. Data injected at /*__DATA__*/.
# ---------------------------------------------------------------------------
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>World Cup 2026</title>
<style>
  :root{
    --bg:#f4f4f1; --card:#ffffff; --line:#e6e6e0;
    --txt:#1a1a18; --mut:#78786f; --acc:#0b7a44; --live:#cf3a3a;
  }
  *{box-sizing:border-box}
  html,body{margin:0;padding:0}
  body{
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    background:var(--bg); color:var(--txt);
    font-size:18px; line-height:1.45; -webkit-font-smoothing:antialiased;
  }
  .wrap{max-width:640px;margin:0 auto;padding:30px 18px 90px}

  h1{font-size:36px;font-weight:800;letter-spacing:-.8px;margin:0}
  .sub{color:var(--mut);font-size:15px;margin-top:4px}

  .pills{display:flex;gap:10px;margin:24px 0 8px;position:sticky;top:0;
    background:var(--bg);padding:10px 0;z-index:5}
  .pill{font-size:16px;font-weight:600;color:var(--mut);background:var(--card);
    border:1px solid var(--line);border-radius:999px;padding:10px 20px;cursor:pointer}
  .pill.on{background:var(--acc);color:#fff;border-color:var(--acc)}

  .rhead{font-size:14px;font-weight:700;letter-spacing:.14em;text-transform:uppercase;
    color:var(--mut);margin:34px 2px 14px}

  .match{background:var(--card);border:1px solid var(--line);border-radius:18px;
    padding:18px 20px;margin-bottom:14px}

  .team{display:flex;align-items:center;gap:14px;padding:6px 0}
  .team img,.team .noflag{width:36px;height:26px;object-fit:contain;border-radius:4px;
    background:#0000000a;flex:none}
  .team .nm{flex:1;font-size:22px;font-weight:600;overflow:hidden;
    text-overflow:ellipsis;white-space:nowrap}
  .team .sc{font-size:26px;font-weight:800;font-variant-numeric:tabular-nums;min-width:28px;text-align:right}
  .team.win .nm{font-weight:800;color:var(--acc)}
  .team.win .sc{color:var(--acc)}

  .meta{margin-top:14px;padding-top:14px;border-top:1px solid var(--line);
    display:grid;gap:9px}
  .when{font-size:18px;font-weight:700}
  .row{font-size:16.5px;color:var(--mut)}
  .live{color:var(--live);font-weight:800;letter-spacing:.04em}

  .empty{color:var(--mut);text-align:center;padding:50px 0;font-size:17px}
  .foot{margin-top:46px;text-align:center;color:var(--mut);font-size:14px}
</style>
</head>
<body>
<div class="wrap">
  <h1>World Cup 2026</h1>
  <div class="sub" id="upd"></div>

  <div class="pills">
    <button class="pill on" data-f="all">All</button>
    <button class="pill" data-f="upcoming">Upcoming</button>
    <button class="pill" data-f="completed">Results</button>
  </div>

  <div id="list"></div>

  <div class="foot">Dates &amp; times in your local zone &middot; Data: ESPN</div>
</div>

<script>
const DATA = /*__DATA__*/;
const M = DATA.matches || [];
let filter = "all";

const fmtD = iso => { const d=new Date(iso); return isNaN(d)?"":d.toLocaleDateString([], {weekday:"short", month:"long", day:"numeric"}); };
const fmtT = iso => { const d=new Date(iso); return isNaN(d)?"":d.toLocaleTimeString([], {hour:"numeric", minute:"2-digit"}); };

function whenLine(m){
  const d = fmtD(m.date);
  if(m.state==="in")   return `${d} &middot; <span class="live">LIVE</span>`;
  if(m.state==="post") return `${d} &middot; Final`;
  return `${d} &middot; ${fmtT(m.date)}`;
}

function teamRow(t, played, win){
  const logo = t.logo ? `<img src="${t.logo}" alt="" loading="lazy">` : `<span class="noflag"></span>`;
  const sc = played ? `<span class="sc">${t.score??""}</span>` : "";
  return `<div class="team ${win?"win":""}">${logo}<span class="nm">${t.name}</span>${sc}</div>`;
}

function card(m){
  const played = m.state!=="pre";
  const loc = [m.venue, m.city].filter(Boolean).join(" &middot; ") || "Venue TBD";
  const tv  = (m.tv && m.tv.length) ? m.tv.join(" &middot; ") : "TBD";
  return `<div class="match">
    ${teamRow(m.home, played, m.state==="post" && m.home.winner)}
    ${teamRow(m.away, played, m.state==="post" && m.away.winner)}
    <div class="meta">
      <div class="when">📅 ${whenLine(m)}</div>
      <div class="row">📍 ${loc}</div>
      <div class="row">📺 ${tv}</div>
    </div>
  </div>`;
}

function pass(m){
  if(filter==="upcoming")  return m.state!=="post";
  if(filter==="completed") return m.state==="post";
  return true;
}

function render(){
  const rows = M.filter(pass);
  const list = document.getElementById("list");
  if(!rows.length){ list.innerHTML = `<div class="empty">Nothing here yet.</div>`; return; }
  const groups = {}, order = [];
  rows.forEach(m=>{ if(!groups[m.round]){ groups[m.round]=[]; order.push(m.round); } groups[m.round].push(m); });
  order.sort((a,b)=> ((groups[a][0]||{}).roundOrder??99) - ((groups[b][0]||{}).roundOrder??99));
  list.innerHTML = order.map(r=>{
    const cards = groups[r].slice()
      .sort((a,b)=> (a.date||"").localeCompare(b.date||""))
      .map(card).join("");
    return `<div class="rhead">${r}</div>${cards}`;
  }).join("");
}

document.querySelectorAll("[data-f]").forEach(el=>{
  el.addEventListener("click", ()=>{
    filter = el.getAttribute("data-f");
    document.querySelectorAll(".pill").forEach(p=>p.classList.toggle("on", p===el));
    render();
  });
});

(function(){
  const b = new Date(DATA.builtAt);
  document.getElementById("upd").textContent =
    "Updated " + (isNaN(b) ? "" : b.toLocaleString([], {month:"short", day:"numeric", hour:"numeric", minute:"2-digit"}));
  render();
})();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    raise SystemExit(main())
