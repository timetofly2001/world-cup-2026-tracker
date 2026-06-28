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
# Front-end: self-contained interactive app. Data injected at /*__DATA__*/.
# ---------------------------------------------------------------------------
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>World Cup 2026 — Live Tracker</title>
<style>
  :root{
    --bg:#0a1626; --bg2:#0f2038; --card:#13294a; --card2:#16335c;
    --line:#1f3e6b; --txt:#eaf2ff; --mut:#8fa8cc;
    --gold:#ffc935; --grn:#23c562; --red:#ff5468; --live:#ff3b5c;
    --acc:#3d8bff;
  }
  *{box-sizing:border-box}
  html,body{margin:0;padding:0}
  body{
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    background:radial-gradient(1200px 600px at 70% -10%,#16335c 0%,var(--bg) 55%) fixed;
    color:var(--txt); -webkit-font-smoothing:antialiased; min-height:100vh;
  }
  a{color:inherit}
  .wrap{max-width:1100px;margin:0 auto;padding:18px 16px 80px}
  header.top{display:flex;flex-wrap:wrap;align-items:flex-end;gap:12px 18px;margin-bottom:6px}
  .title{font-size:30px;font-weight:800;letter-spacing:-.5px;line-height:1.05}
  .title .em{color:var(--gold)}
  .sub{color:var(--mut);font-size:13px;margin-top:2px}
  .countdown{margin-left:auto;text-align:right}
  .countdown .cd{font-size:24px;font-weight:800;color:var(--gold);font-variant-numeric:tabular-nums}
  .countdown .cl{font-size:11px;color:var(--mut);text-transform:uppercase;letter-spacing:.08em}

  .stats{display:flex;gap:10px;flex-wrap:wrap;margin:16px 0 10px}
  .stat{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:10px 14px;min-width:96px}
  .stat .n{font-size:22px;font-weight:800}
  .stat .l{font-size:11px;color:var(--mut);text-transform:uppercase;letter-spacing:.06em}
  .stat.live .n{color:var(--live)}
  .stat.dec .n{color:var(--grn)}
  .stat.up .n{color:var(--acc)}

  .controls{position:sticky;top:0;z-index:20;background:linear-gradient(180deg,var(--bg) 70%,transparent);
    padding:10px 0 12px;margin-bottom:6px}
  .search{width:100%;padding:12px 14px;border-radius:12px;border:1px solid var(--line);
    background:var(--card);color:var(--txt);font-size:15px;outline:none}
  .search::placeholder{color:var(--mut)}
  .tabs{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
  .tab{border:1px solid var(--line);background:var(--card);color:var(--mut);
    padding:7px 13px;border-radius:999px;font-size:13px;font-weight:600;cursor:pointer;white-space:nowrap}
  .tab:hover{color:var(--txt)}
  .tab.on{background:var(--gold);color:#1a1300;border-color:var(--gold)}

  .roundhead{display:flex;align-items:center;gap:10px;margin:22px 2px 10px;font-size:14px;
    font-weight:700;color:var(--mut);text-transform:uppercase;letter-spacing:.1em}
  .roundhead::after{content:"";flex:1;height:1px;background:var(--line)}

  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}
  .match{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:14px;
    position:relative;transition:transform .12s ease,border-color .12s ease}
  .match:hover{transform:translateY(-2px);border-color:#2a5395}
  .match.fav{border-color:var(--gold);box-shadow:0 0 0 1px var(--gold) inset}
  .match.islive{border-color:var(--live);box-shadow:0 0 22px -8px var(--live)}
  .mtop{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px}
  .badge{font-size:10.5px;font-weight:800;letter-spacing:.06em;text-transform:uppercase;
    padding:3px 8px;border-radius:999px;background:var(--card2);color:var(--mut)}
  .badge.pre{color:var(--acc)}
  .badge.post{color:var(--grn)}
  .badge.in{color:#fff;background:var(--live);animation:pulse 1.4s infinite}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.45}}
  .star{cursor:pointer;font-size:16px;color:#3a567f;user-select:none}
  .star.on{color:var(--gold)}

  .team{display:flex;align-items:center;gap:10px;padding:5px 0}
  .team img{width:30px;height:22px;object-fit:contain;border-radius:3px;background:#fff1;flex:none}
  .team .nm{font-size:16px;font-weight:600;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .team .sc{font-size:20px;font-weight:800;font-variant-numeric:tabular-nums;min-width:24px;text-align:right}
  .team.win .nm{color:#fff}
  .team.lose{opacity:.55}
  .vs{height:1px;background:var(--line);margin:3px 0}

  .meta{margin-top:11px;border-top:1px solid var(--line);padding-top:10px;display:grid;gap:6px}
  .row{display:flex;align-items:center;gap:8px;font-size:12.5px;color:var(--mut)}
  .row .ic{width:15px;text-align:center;flex:none;opacity:.85}
  .row .tv b{color:var(--gold)}
  .when{color:var(--txt);font-weight:600}

  .empty{text-align:center;color:var(--mut);padding:50px 0;font-size:15px}
  footer{margin-top:40px;text-align:center;color:var(--mut);font-size:12px;line-height:1.6}
  .toast{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);background:var(--gold);
    color:#1a1300;font-weight:700;padding:10px 18px;border-radius:999px;font-size:13px;
    opacity:0;pointer-events:none;transition:opacity .25s;z-index:50}
  .toast.show{opacity:1}
  @media(max-width:520px){.title{font-size:24px}.countdown{margin-left:0;text-align:left}}
</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <div>
      <div class="title">🏆 <span class="em">2026 World Cup</span> Tracker</div>
      <div class="sub" id="sub">Loading…</div>
    </div>
    <div class="countdown">
      <div class="cd" id="cd">—</div>
      <div class="cl" id="cdlabel">to the Final</div>
    </div>
  </header>

  <div class="stats" id="stats"></div>

  <div class="controls">
    <input class="search" id="search" placeholder="🔎 Search a team, city, or channel…" autocomplete="off">
    <div class="tabs" id="tabs"></div>
  </div>

  <div id="list"></div>

  <footer>
    <div>Auto-updates twice daily · Times shown in <b id="tz"></b> · Data: ESPN</div>
    <div id="built"></div>
    <div>⭐ Tap a star to follow a team — their matches pin to the top and stay highlighted.</div>
  </footer>
</div>
<div class="toast" id="toast"></div>

<script>
const DATA = /*__DATA__*/;
const M = DATA.matches || [];
const favs = new Set(JSON.parse(localStorage.getItem("wc_favs")||"[]"));
let filter = "all", q = "";

const FINAL_DATE = new Date("2026-07-19T19:00:00Z");
const TZ = Intl.DateTimeFormat().resolvedOptions().timeZone || "local time";
document.getElementById("tz").textContent = TZ;

function saveFavs(){ localStorage.setItem("wc_favs", JSON.stringify([...favs])); }
function isFav(m){ return favs.has(m.home.name) || favs.has(m.away.name); }

function fmtWhen(iso){
  const d = new Date(iso);
  if(isNaN(d)) return "";
  return d.toLocaleString([], {weekday:"short", month:"short", day:"numeric", hour:"numeric", minute:"2-digit"});
}
function fmtDay(iso){
  const d = new Date(iso);
  return isNaN(d) ? "" : d.toLocaleDateString([], {weekday:"long", month:"long", day:"numeric"});
}

function statusLabel(m){
  if(m.state==="in") return {cls:"in", txt: m.statusShort||"Live"};
  if(m.state==="post") return {cls:"post", txt:"Full Time"};
  return {cls:"pre", txt:"Upcoming"};
}

function teamRow(t, other, m){
  const played = m.state!=="pre";
  let cls="team";
  if(played && m.state==="post"){ cls += t.winner ? " win" : (other.winner? " lose":""); }
  const score = played ? `<div class="sc">${t.score??""}</div>` : "";
  const logo = t.logo ? `<img src="${t.logo}" alt="" loading="lazy">` : `<div style="width:30px"></div>`;
  return `<div class="${cls}">${logo}<div class="nm">${t.name}</div>${score}</div>`;
}

function card(m){
  const st = statusLabel(m);
  const fav = isFav(m);
  const live = m.state==="in";
  const tv = (m.tv&&m.tv.length) ? `📺 <span class="tv"><b>${m.tv.join(" · ")}</b></span>` : "📺 TBD";
  const loc = [m.venue, m.city].filter(Boolean).join(" · ") || "Venue TBD";
  const whenTxt = m.state==="pre" ? fmtWhen(m.date) : (m.statusDetail || fmtWhen(m.date));
  return `<div class="match ${fav?'fav':''} ${live?'islive':''}" data-id="${m.id}">
    <div class="mtop">
      <span class="badge ${st.cls}">${st.txt}</span>
      <span class="star ${fav?'on':''}" data-fav="${m.id}" title="Follow a team">★</span>
    </div>
    ${teamRow(m.home, m.away, m)}
    <div class="vs"></div>
    ${teamRow(m.away, m.home, m)}
    <div class="meta">
      <div class="row"><span class="ic">🕑</span><span class="when">${whenTxt}</span></div>
      <div class="row"><span class="ic">📍</span><span>${loc}</span></div>
      <div class="row"><span class="ic"></span><span>${tv}</span></div>
    </div>
  </div>`;
}

function matchesFilter(m){
  if(q){
    const hay = (m.home.name+" "+m.away.name+" "+m.city+" "+m.venue+" "+(m.tv||[]).join(" ")+" "+m.round).toLowerCase();
    if(!hay.includes(q)) return false;
  }
  switch(filter){
    case "all": return true;
    case "live": return m.state==="in";
    case "today": {
      const d=new Date(m.date), n=new Date();
      return d.toDateString()===n.toDateString();
    }
    case "upcoming": return m.state==="pre";
    case "final": return m.state==="post";
    case "fav": return isFav(m);
    default: return m.round===filter; // round name
  }
}

function render(){
  const list = document.getElementById("list");
  let rows = M.filter(matchesFilter);

  // Favorites float to top, then chronological.
  rows.sort((a,b)=>{
    const fa=isFav(a)?0:1, fb=isFav(b)?0:1;
    if(fa!==fb) return fa-fb;
    return (a.date||"").localeCompare(b.date||"");
  });

  if(!rows.length){ list.innerHTML = `<div class="empty">No matches here yet. Try another filter.</div>`; return; }

  // Group by round (unless favorites are pinned & mixed — still group by round within).
  const groups = {};
  const order = [];
  rows.forEach(m=>{ if(!groups[m.round]){groups[m.round]=[];order.push(m.round);} groups[m.round].push(m); });
  order.sort((a,b)=>{
    const oa=(groups[a][0]||{}).roundOrder??99, ob=(groups[b][0]||{}).roundOrder??99;
    return oa-ob;
  });

  list.innerHTML = order.map(r=>
    `<div class="roundhead">${r}</div><div class="grid">${groups[r].map(card).join("")}</div>`
  ).join("");

  list.querySelectorAll("[data-fav]").forEach(el=>{
    el.addEventListener("click", e=>{
      e.stopPropagation();
      const m = M.find(x=>x.id===el.getAttribute("data-fav"));
      if(!m) return;
      // Toggle both teams in the match as a unit.
      const on = isFav(m);
      [m.home.name, m.away.name].forEach(n=> on?favs.delete(n):favs.add(n));
      saveFavs(); render();
      toast(on ? "Unfollowed" : `Following ${m.home.name} & ${m.away.name} ⭐`);
    });
  });
}

function buildStats(){
  const live = M.filter(m=>m.state==="in").length;
  const dec  = M.filter(m=>m.state==="post").length;
  const up   = M.filter(m=>m.state==="pre").length;
  const cells = [
    ["dec", dec, "Decided"],
    ["up", up, "Upcoming"],
    ["", M.length, "Total Matches"],
  ];
  if(live) cells.unshift(["live", live, "Live Now"]);
  document.getElementById("stats").innerHTML = cells.map(([c,n,l])=>
    `<div class="stat ${c}"><div class="n">${n}</div><div class="l">${l}</div></div>`).join("");
}

function buildTabs(){
  const rounds = [...new Set(M.map(m=>m.round))]
    .sort((a,b)=>{
      const oa=(M.find(m=>m.round===a)||{}).roundOrder??99, ob=(M.find(m=>m.round===b)||{}).roundOrder??99;
      return oa-ob;
    });
  const base = [["all","All"],["today","Today"],["live","● Live"],["upcoming","Upcoming"],["final","Decided"],["fav","⭐ My Teams"]];
  const all = base.concat(rounds.map(r=>[r,r]));
  document.getElementById("tabs").innerHTML = all.map(([k,l])=>
    `<div class="tab ${k===filter?'on':''}" data-tab="${k}">${l}</div>`).join("");
  document.querySelectorAll("[data-tab]").forEach(el=>{
    el.addEventListener("click", ()=>{ filter = el.getAttribute("data-tab");
      document.querySelectorAll(".tab").forEach(t=>t.classList.toggle("on", t===el)); render(); });
  });
}

function tickCountdown(){
  const now = new Date();
  // Find next upcoming kickoff; else count to the Final.
  const next = M.filter(m=>new Date(m.date)>now).sort((a,b)=>new Date(a.date)-new Date(b.date))[0];
  const target = next ? new Date(next.date) : FINAL_DATE;
  const label = next ? `to ${next.home.abbr||next.home.name} v ${next.away.abbr||next.away.name}` : "to the Final";
  let diff = Math.max(0, target - now);
  const d=Math.floor(diff/864e5); diff-=d*864e5;
  const h=Math.floor(diff/36e5); diff-=h*36e5;
  const mi=Math.floor(diff/6e4); diff-=mi*6e4;
  const s=Math.floor(diff/1e3);
  const txt = d>0 ? `${d}d ${h}h ${mi}m` : `${h}h ${mi}m ${String(s).padStart(2,"0")}s`;
  document.getElementById("cd").textContent = txt;
  document.getElementById("cdlabel").textContent = label;
}

let toastTimer;
function toast(msg){
  const t=document.getElementById("toast"); t.textContent=msg; t.classList.add("show");
  clearTimeout(toastTimer); toastTimer=setTimeout(()=>t.classList.remove("show"),1800);
}

document.getElementById("search").addEventListener("input", e=>{ q=e.target.value.trim().toLowerCase(); render(); });

// Init
(function(){
  const dec = M.filter(m=>m.state==="post").length;
  const tot = M.length;
  document.getElementById("sub").textContent =
    `${tot} matches • ${dec} decided • ${tot-dec} to play`;
  const b = new Date(DATA.builtAt);
  document.getElementById("built").textContent =
    "Last updated " + (isNaN(b)? "" : b.toLocaleString([], {month:"short",day:"numeric",hour:"numeric",minute:"2-digit"}));
  buildStats(); buildTabs(); render();
  tickCountdown(); setInterval(tickCountdown, 1000);
})();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    raise SystemExit(main())
