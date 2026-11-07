#!/usr/bin/env python3
"""Render one GitHub profile card (SVG) from the GitHub GraphQL API.

    GITHUB_TOKEN=$(gh auth token) python3 stats/generate.py --out stats/card.svg

The card answers questions GitHub's own profile page does not: how consistent
the year was, how much of the work is private, how the work splits across
commits / pull requests / reviews / issues, and which languages the projects
are built in. No repository name is ever printed, so a private-heavy year
still gets a complete card.

Both palettes are baked into the file, so one embed is correct in light and
dark mode. No dependencies beyond the Python standard library.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import urllib.request

# --- look -----------------------------------------------------------------

WIDTH, HEIGHT = 880, 500
PAD = 40
ACCENT_LIGHT = "#C25436"
ACCENT_DARK = "#E4704B"

LIGHT = {
    "bg": "#FAF8F5",
    "border": "#E8E2D8",
    "ink": "#17150F",
    "muted": "#6F675C",  # 5.25:1 on bg — safe for 8-10px labels
    "rule": "#E4DED3",
    "axis": "#D8D1C4",
    "empty": "#EFECE5",
}
DARK = {
    "bg": "#0F1319",
    "border": "#232A33",
    "ink": "#ECEFEE",
    "muted": "#8B949E",
    "rule": "#242B35",
    "axis": "#2E3641",
    "empty": "#1A2028",
}

MONO_FAMILY = "ui-monospace,SFMono-Regular,'SF Mono',Menlo,Consolas,monospace"
SANS_FAMILY = (
    "ui-sans-serif,-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif"
)

# Kept out of the repositories/language stats: rendered artefacts, vendored
# bundles, anything whose size says nothing about what you work in. Contribution
# counts still include these repos — only the repo-level numbers skip them.
EXCLUDE = {"master-thesis"}

GRAPHQL = """
query($login:String!, $from:DateTime!, $to:DateTime!, $after:String) {
  user(login:$login) {
    createdAt
    contributionsCollection(from:$from, to:$to) {
      totalCommitContributions
      totalIssueContributions
      totalPullRequestContributions
      totalPullRequestReviewContributions
      restrictedContributionsCount
      contributionCalendar {
        totalContributions
        weeks { contributionDays { date contributionCount } }
      }
      commitContributionsByRepository(maxRepositories:100) { repository { nameWithOwner } }
      pullRequestContributionsByRepository(maxRepositories:100) { repository { nameWithOwner } }
      issueContributionsByRepository(maxRepositories:100) { repository { nameWithOwner } }
      pullRequestReviewContributionsByRepository(maxRepositories:100) { repository { nameWithOwner } }
    }
    repositories(first:100, after:$after, ownerAffiliations:OWNER, isFork:false) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {
        name
        isPrivate
        languages(first:15, orderBy:{field:SIZE, direction:DESC}) {
          edges { size node { name color } }
        }
      }
    }
  }
}
"""


# --- data -----------------------------------------------------------------


def token() -> str:
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        return tok.strip()
    gh = shutil.which("gh")
    if gh:
        try:
            return subprocess.check_output([gh, "auth", "token"], text=True).strip()
        except subprocess.CalledProcessError:
            pass
    sys.exit("No GitHub token: set GITHUB_TOKEN (needs `repo` scope for private stats).")


def graphql(query: str, variables: dict, tok: str) -> dict:
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={
            "Authorization": f"bearer {tok}",
            "Content-Type": "application/json",
            "User-Agent": "profile-card",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.load(resp)
    if "errors" in payload:
        sys.exit("GitHub API error: " + json.dumps(payload["errors"], indent=2))
    return payload["data"]


def fetch(login: str, tok: str) -> dict:
    today = dt.datetime.now(dt.timezone.utc).date()
    start = today - dt.timedelta(days=364)
    variables = {
        "login": login,
        "from": f"{start}T00:00:00Z",
        "to": f"{today}T23:59:59Z",
        "after": None,
    }
    data = graphql(GRAPHQL, variables, tok)
    user = data["user"]
    if not user:
        sys.exit(f"No such user: {login}")

    repos, page = [], user["repositories"]
    while True:
        repos.extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            break
        variables["after"] = page["pageInfo"]["endCursor"]
        page = graphql(GRAPHQL, variables, tok)["user"]["repositories"]

    repos = [r for r in repos if r["name"] not in EXCLUDE]

    coll = user["contributionsCollection"]
    cal = coll["contributionCalendar"]
    all_days = [
        (d["date"][:10], d["contributionCount"])
        for w in cal["weeks"]
        for d in w["contributionDays"]
    ]
    # The calendar is Sunday-aligned, so it can carry a few days beyond the
    # requested window. Every stat below uses exactly the 365-day window.
    days = [
        (d, c)
        for d, c in all_days
        if start <= dt.date.fromisoformat(d) <= today
    ]

    # Count repositories touched without ever storing a name for the card.
    touched: set[str] = set()
    for key in (
        "commitContributionsByRepository",
        "pullRequestContributionsByRepository",
        "issueContributionsByRepository",
        "pullRequestReviewContributionsByRepository",
    ):
        for edge in coll[key] or []:
            repo = edge.get("repository")
            if repo:
                touched.add(repo["nameWithOwner"])

    return {
        "days": days,
        "total": cal["totalContributions"],
        "private_contribs": coll["restrictedContributionsCount"],
        "public_contribs": cal["totalContributions"]
        - coll["restrictedContributionsCount"],
        "since": int(user["createdAt"][:4]),
        "repos": len(repos),
        "private_repos": sum(1 for r in repos if r["isPrivate"]),
        "languages": languages(repos),
        "commits": coll["totalCommitContributions"],
        "prs": coll["totalPullRequestContributions"],
        "issues": coll["totalIssueContributions"],
        "reviews": coll["totalPullRequestReviewContributions"],
        "touched": len(touched),
        "today": today,
    }


def languages(repos: list[dict]) -> list[tuple[str, float, str]]:
    """Share of projects per language, not share of bytes.

    Summing bytes across repos lets one generated artefact (a rendered thesis,
    a vendored bundle) outvote every real project. Each repository instead gets
    one equal vote, so the numbers answer "what do I build with".
    """
    share: dict[str, float] = {}
    colors: dict[str, str] = {}
    for repo in repos:
        edges = repo["languages"]["edges"]
        total = sum(e["size"] for e in edges) or 1
        for edge in edges:
            name = edge["node"]["name"]
            share[name] = share.get(name, 0.0) + edge["size"] / total
            colors.setdefault(name, edge["node"]["color"] or "#8B949E")
    weights = {name: value / max(len(repos), 1) for name, value in share.items()}
    return [(n, w, colors[n]) for n, w in sorted(weights.items(), key=lambda kv: -kv[1])]


def streaks(days: list[tuple[str, int]], today: dt.date) -> tuple[int, int]:
    """(current, longest) in consecutive days with at least one contribution."""
    longest = run = 0
    for _, count in days:
        run = run + 1 if count > 0 else 0
        longest = max(longest, run)

    counts = [c for _, c in days]
    i = len(counts) - 1
    if i >= 0 and counts[i] == 0:  # today may simply be young
        i -= 1
    current = 0
    while i >= 0 and counts[i] > 0:
        current += 1
        i -= 1
    return current, longest


def longest_gap(days: list[tuple[str, int]]) -> int:
    """Longest run of consecutive days without a contribution."""
    longest = run = 0
    for _, count in days:
        run = run + 1 if count == 0 else 0
        longest = max(longest, run)
    return longest


def add_months(day: dt.date, delta: int) -> dt.date:
    y = day.year + (day.month - 1 + delta) // 12
    m = (day.month - 1 + delta) % 12 + 1
    return dt.date(y, m, 1)


def monthly_consistency(
    days: list[tuple[str, int]], today: dt.date
) -> list[tuple[str, float, bool]]:
    """Share of days active in each of the last 12 calendar months.

    A rate rather than a count, so a partly-elapsed current month and a full
    month are directly comparable.
    """
    by_date = {dt.date.fromisoformat(d): c for d, c in days}
    first = add_months(today.replace(day=1), -11)
    window_start = today - dt.timedelta(days=364)
    out: list[tuple[str, float, bool]] = []
    for i in range(12):
        m0 = add_months(first, i)
        m1 = add_months(m0, 1) - dt.timedelta(days=1)
        lo = max(m0, window_start)
        hi = min(m1, today)
        span = (hi - lo).days + 1
        active = sum(1 for d, c in by_date.items() if lo <= d <= hi and c > 0)
        out.append(
            (
                m0.strftime("%b").upper(),
                active / span if span > 0 else 0.0,
                m0.year == today.year and m0.month == today.month,
            )
        )
    return out


def momentum(days: list[tuple[str, int]], today: dt.date) -> tuple[int, int | None]:
    """(last 90 days, percent change vs the 90 before), or None without history."""
    by_date = {dt.date.fromisoformat(d): c for d, c in days}
    recent_lo = today - dt.timedelta(days=89)
    prior_hi = today - dt.timedelta(days=90)
    prior_lo = today - dt.timedelta(days=179)
    recent = sum(c for d, c in by_date.items() if recent_lo <= d <= today)
    prior = sum(c for d, c in by_date.items() if prior_lo <= d <= prior_hi)
    if prior == 0:
        return recent, None
    return recent, round((recent - prior) / prior * 100)


def busiest_day(days: list[tuple[str, int]]) -> str:
    totals = [0] * 7
    counts = [0] * 7
    for d, count in days:
        wd = dt.date.fromisoformat(d).weekday()
        totals[wd] += count
        counts[wd] += 1
    avg = [totals[i] / counts[i] if counts[i] else 0.0 for i in range(7)]
    names = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
    return names[max(range(7), key=lambda i: avg[i])]


# --- drawing --------------------------------------------------------------


def mix(a: str, b: str, t: float) -> str:
    av = [int(a[i : i + 2], 16) for i in (1, 3, 5)]
    bv = [int(b[i : i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02X}" for x, y in zip(av, bv))


def esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def t(
    x: float,
    y: float,
    body: str,
    cls: str = "",
    style: str = "",
    anchor: str = "start",
    opacity: float | None = None,
) -> str:
    extra = ""
    if anchor != "start":
        extra += f' text-anchor="{anchor}"'
    if opacity is not None:
        extra += f' opacity="{opacity}"'
    if style:
        extra += f' style="{style}"'
    return f'<text class="{cls}" x="{x:.1f}" y="{y:.1f}"{extra}>{body}</text>'


def rule(x1: float, y1: float, x2: float, y2: float, cls: str = "rule") -> str:
    return (
        f'<line class="{cls}" x1="{x1:.1f}" y1="{y1:.1f}" '
        f'x2="{x2:.1f}" y2="{y2:.1f}" stroke-width="1"/>'
    )


def box(x: float, y: float, w: float, h: float, cls: str, rx: float = 0) -> str:
    return (
        f'<rect class="{cls}" x="{x:.1f}" y="{y:.1f}" '
        f'width="{w:.1f}" height="{h:.1f}" rx="{rx}"/>'
    )


def column(x: float, top: float, w: float, base: float, r: float = 4) -> str:
    """A bar growing up from a baseline: rounded data-end, square at the base."""
    h = base - top
    if h <= 0.5:
        return ""
    r = min(r, h, w / 2)
    return (
        f"M{x:.1f},{base:.1f} L{x:.1f},{top + r:.1f} "
        f"Q{x:.1f},{top:.1f} {x + r:.1f},{top:.1f} "
        f"L{x + w - r:.1f},{top:.1f} Q{x + w:.1f},{top:.1f} {x + w:.1f},{top + r:.1f} "
        f"L{x + w:.1f},{base:.1f} Z"
    )


def ranked_bar(x: float, y: float, w: float, h: float, r: float = 3) -> str:
    """A horizontal ranked bar: square at the baseline, rounded at the data-end."""
    if w <= 0.5:
        return ""
    r = min(r, w, h / 2)
    return (
        f"M{x:.1f},{y:.1f} L{x + w - r:.1f},{y:.1f} "
        f"Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f} "
        f"L{x + w:.1f},{y + h - r:.1f} Q{x + w:.1f},{y + h:.1f} {x + w - r:.1f},{y + h:.1f} "
        f"L{x:.1f},{y + h:.1f} Z"
    )


def build(data: dict) -> str:
    today = data["today"]
    days = data["days"]
    total = data["total"]
    active = sum(1 for _, c in days if c > 0)
    rate = active / len(days) if days else 0.0
    current, longest = streaks(days, today)
    gap = longest_gap(days)
    avg = total / active if active else 0.0
    private = data["private_contribs"]
    private_share = private / total if total else 0.0
    monthly = monthly_consistency(days, today)
    recent, delta = momentum(days, today)
    busiest = busiest_day(days)
    login = data["login"]

    out: list[str] = []
    add = out.append

    add(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" '
        f'viewBox="0 0 {WIDTH} {HEIGHT}" role="img" aria-labelledby="t" '
        f'aria-describedby="d">'
    )
    add(f'<title id="t">{esc(login)} — a year in numbers</title>')
    add(
        f'<desc id="d">{active} active days ({rate * 100:.0f}% of the year), '
        f"{total:,} contributions, {data['commits']:,} commits, "
        f"{data['prs']:,} pull requests, {data['reviews']:,} reviews, "
        f"{data['issues']:,} issues, across {data['touched']} repositories.</desc>"
    )

    add("<style>")
    add(f"text{{font-family:{SANS_FAMILY};font-kerning:normal}}")
    add(
        f".label{{font-family:{MONO_FAMILY};font-size:9.5px;letter-spacing:1.5px;"
        f"fill:{LIGHT['muted']}}}"
    )
    add(
        f".label-sm{{font-family:{MONO_FAMILY};font-size:9px;letter-spacing:1.0px;"
        f"fill:{LIGHT['muted']}}}"
    )
    add(f".data-sm{{font-family:{MONO_FAMILY};font-size:9px;fill:{LIGHT['ink']}}}")
    add(f".lang{{font-family:{SANS_FAMILY};font-size:10px;fill:{LIGHT['muted']}}}")
    add(f".lang-val{{font-family:{SANS_FAMILY};font-size:10px;fill:{LIGHT['ink']}}}")
    add(f".ink{{fill:{LIGHT['ink']}}}.muted{{fill:{LIGHT['muted']}}}")
    add(f".card{{fill:{LIGHT['bg']};stroke:{LIGHT['border']}}}")
    add(f".rule{{stroke:{LIGHT['rule']}}}.axis{{stroke:{LIGHT['axis']}}}")
    add(f".accent{{fill:{ACCENT_LIGHT}}}.bar{{fill:{ACCENT_LIGHT}}}")
    add(f".track{{fill:{mix(LIGHT['bg'], ACCENT_LIGHT, 0.14)}}}")
    add("@media (prefers-color-scheme:dark){")
    add(
        f".label{{fill:{DARK['muted']}}}.label-sm{{fill:{DARK['muted']}}}"
        f".data-sm{{fill:{DARK['ink']}}}"
    )
    add(f".lang{{fill:{DARK['muted']}}}.lang-val{{fill:{DARK['ink']}}}")
    add(f".ink{{fill:{DARK['ink']}}}.muted{{fill:{DARK['muted']}}}")
    add(f".card{{fill:{DARK['bg']};stroke:{DARK['border']}}}")
    add(f".rule{{stroke:{DARK['rule']}}}.axis{{stroke:{DARK['axis']}}}")
    add(f".accent{{fill:{ACCENT_DARK}}}.bar{{fill:{ACCENT_DARK}}}")
    add(f".track{{fill:{mix(DARK['bg'], ACCENT_DARK, 0.22)}}}")
    add("}")
    add("</style>")

    add(box(0.5, 0.5, WIDTH - 1, HEIGHT - 1, "card", 14))

    # header ---------------------------------------------------------------
    add(t(PAD, 40, "A YEAR IN NUMBERS", "label"))
    add(t(WIDTH - PAD, 40, f"{esc(login)} · SINCE {data['since']}", "label", anchor="end"))

    # hero: consistency, the thing the heatmap implies but never says ------
    add(t(PAD, 76, "ACTIVE DAYS", "label"))
    add(
        t(
            PAD - 2,
            140,
            f"{active:,}",
            "ink",
            style="font-size:58px;font-weight:600;letter-spacing:-1.5px",
        )
    )
    add(
        t(
            PAD,
            162,
            f"{rate * 100:.0f}% of the last 365 days",
            "muted",
            style="font-size:10.5px",
        )
    )

    # hero-right: one tile per stat, so a token without private access simply
    # drops the private tile instead of printing a misleading zero.
    tiles: list[tuple[str, str, str, float | None]] = []
    if private:
        tiles.append(
            (
                "PRIVATE WORK",
                f"{private_share * 100:.0f}%",
                f"of {total:,} contributions",
                private_share,
            )
        )
    tiles.append(("CURRENT STREAK", f"{current}d", "days in a row", None))
    tiles.append(("LONGEST STREAK", f"{longest}d", "this year", None))

    tile_span = 400 / len(tiles)
    for i, (label, value, sub, meter) in enumerate(tiles):
        x = 420 + i * tile_span
        add(t(x, 76, label, "label"))
        add(
            t(
                x - 1,
                128,
                value,
                "ink",
                style="font-size:32px;font-weight:600;letter-spacing:-.8px",
            )
        )
        add(t(x, 146, sub, "muted", style="font-size:9px"))
        if meter is not None:
            mw = tile_span - 24
            add(box(x, 156, mw, 6, "track", 3))
            add(box(x, 156, max(4.0, mw * meter), 6, "accent", 3))

    add(rule(PAD, 200, WIDTH - PAD, 200))

    # monthly consistency --------------------------------------------------
    add(t(PAD, 226, "DAYS ACTIVE BY MONTH", "label"))
    add(t(500, 226, "* CURRENT MONTH TO DATE", "label-sm", anchor="end"))

    cx0, cx1 = 74.0, 500.0
    base_y, top_y = 332.0, 248.0
    plot_h = base_y - top_y
    for frac, label in ((1.0, "100%"), (0.5, "50%")):
        y = base_y - plot_h * frac
        add(rule(cx0, y, cx1, y))
        add(t(cx0 - 6, y + 3, label, "label-sm", anchor="end"))
    add(t(cx0 - 6, base_y + 3, "0%", "label-sm", anchor="end"))
    add(rule(cx0, base_y, cx1, base_y, "axis"))

    n = len(monthly)
    band = (cx1 - cx0) / n
    bar_w = min(20.0, band * 0.58)
    for i, (label, share, is_current) in enumerate(monthly):
        x = cx0 + band * i + (band - bar_w) / 2
        top = base_y - plot_h * share
        path = column(x, top, bar_w, base_y)
        if path:
            add(f'<path class="bar" d="{path}"/>')
        add(
            t(
                cx0 + band * i + band / 2,
                348,
                label + ("*" if is_current else ""),
                "label-sm",
                anchor="middle",
            )
        )

    peak = max(range(n), key=lambda i: monthly[i][1])
    if monthly[peak][1] > 0:
        px = cx0 + band * peak + band / 2
        py = base_y - plot_h * monthly[peak][1] - 6
        add(t(px, py, f"{monthly[peak][1] * 100:.0f}%", "data-sm", anchor="middle"))

    # work mix -------------------------------------------------------------
    add(t(540, 226, "HOW THE YEAR WAS SPENT", "label"))
    work = [
        ("COMMITS", data["commits"]),
        ("PULL REQUESTS", data["prs"]),
        ("REVIEWS", data["reviews"]),
        ("ISSUES", data["issues"]),
    ]
    for i, (label, value) in enumerate(work):
        col, row = i % 2, i // 2
        x = 540 + col * 156
        y = 252 + row * 58
        add(t(x, y, label, "label-sm"))
        add(
            t(
                x - 1,
                y + 30,
                f"{value:,}",
                "ink",
                style="font-size:25px;font-weight:600;letter-spacing:-.6px",
            )
        )
    add(t(540, 358, f"ACROSS {data['touched']:,} REPOSITORIES", "label-sm"))

    add(rule(PAD, 372, WIDTH - PAD, 372))

    # languages: ranked bars, one hue, names and shares direct-labeled ------
    add(t(PAD, 396, "LANGUAGES BY PROJECT", "label"))
    langs = data["languages"][:5]
    if len(data["languages"]) > len(langs):
        add(t(500, 396, f"TOP {len(langs)} OF {len(data['languages'])}", "label-sm", anchor="end"))
    if langs:
        top_weight = langs[0][1] or 1.0
        for i, (name, weight, _) in enumerate(langs):
            y = 414 + i * 17
            add(t(PAD, y + 3, esc(name), "lang"))
            add(box(160, y - 3, 300, 6, "track", 3))
            path = ranked_bar(160, y - 3, 300 * weight / top_weight, 6)
            if path:
                add(f'<path class="bar" d="{path}"/>')
            add(t(500, y + 3, f"{weight * 100:.0f}%", "lang-val", anchor="end"))
    else:
        add(t(PAD, 414, "No language data", "muted", style="font-size:10px"))

    # secondary stats ------------------------------------------------------
    if delta is None or delta == 0:
        delta_text = "—"
    else:
        delta_text = f"{'▲' if delta > 0 else '▼'} {abs(delta)}%"

    secondary = [
        ("LONGEST GAP", f"{gap}d", ""),
        ("LAST 90 VS PRIOR", f"{recent:,}", delta_text),
        ("AVG / ACTIVE DAY", f"{avg:.1f}", ""),
        ("BUSIEST DAY", busiest, ""),
    ]
    for i, (label, value, extra) in enumerate(secondary):
        col, row = i % 2, i // 2
        x = 540 + col * 156
        y = 396 + row * 48
        add(t(x, y, label, "label-sm"))
        add(
            t(
                x - 1,
                y + 24,
                value,
                "ink",
                style="font-size:24px;font-weight:600;letter-spacing:-.6px",
            )
        )
        if extra:
            # Rough proportional-digit width; the delta rides just past the value.
            width = len(value) * 24 * 0.58
            add(t(x + width + 8, y + 24, extra, "label-sm"))

    add("</svg>")
    return "\n".join(out)


def preview(svg: str, path: str) -> None:
    """Both palettes side by side, forced rather than following the OS.

    Each SVG is embedded as an image, not inline: inline SVG styles are global
    to the page, so the dark copy would otherwise repaint the light one.
    """
    import base64
    import re

    light = re.sub(r"@media \(prefers-color-scheme:dark\)\{.*?\n\}", "", svg, flags=re.S)
    dark = svg.replace("@media (prefers-color-scheme:dark){", "@media all{")

    def img(source: str) -> str:
        b64 = base64.b64encode(source.encode()).decode()
        return f'<img src="data:image/svg+xml;base64,{b64}" width="{WIDTH}" height="{HEIGHT}">'

    page = f"""<!doctype html><meta charset="utf-8"><title>profile card</title>
<style>
  body{{margin:0;padding:40px;display:grid;gap:24px;justify-content:center;
        font:11px ui-monospace,Menlo,monospace;background:#fff;color:#999}}
  .light{{background:#fff;padding:24px}}
  .dark{{background:#0d1117;padding:24px}}
  h2{{letter-spacing:1.5px;margin:0 0 14px}}
  img{{display:block}}
</style>
<div class="light"><h2>LIGHT</h2>{img(light)}</div>
<div class="dark"><h2 style="color:#666">DARK</h2>{img(dark)}</div>
"""
    with open(path, "w") as fh:
        fh.write(page)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", default=os.environ.get("GITHUB_LOGIN", "touhi99"))
    ap.add_argument("--out", default="stats/card.svg")
    ap.add_argument("--preview", help="also write a side-by-side light/dark HTML preview")
    ap.add_argument(
        "--require-private",
        action="store_true",
        help="fail instead of silently publishing a public-only card",
    )
    args = ap.parse_args()

    data = fetch(args.login, token())
    data["login"] = args.login
    if args.require_private and not data["private_repos"]:
        sys.exit(
            "No private repositories are visible with this token, so the card would "
            "silently drop most of your activity. Add a repo secret STATS_TOKEN "
            "(classic PAT with `repo` scope), or run without --require-private."
        )
    svg = build(data)

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as fh:
        fh.write(svg)
    if args.preview:
        preview(svg, args.preview)

    active = sum(1 for _, c in data["days"] if c > 0)
    cur, longest = streaks(data["days"], data["today"])
    print(
        f"{data['total']:,} contributions · {active} active days · "
        f"{cur}d current · {longest}d longest · {data['touched']} repos touched "
        f"→ {args.out}"
    )


if __name__ == "__main__":
    main()
