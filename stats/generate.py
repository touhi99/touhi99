#!/usr/bin/env python3
"""Render one GitHub profile card (SVG) from the GitHub GraphQL API.

    GITHUB_TOKEN=$(gh auth token) python3 stats/generate.py --out stats/card.svg

The card is a single image: a year of contributions as a heatmap, the streak
numbers that matter, and how the code splits across languages. Both palettes
are baked into the file, so one embed is correct in light and dark mode.

No dependencies beyond the Python standard library.
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
from bisect import bisect_right

# --- look -----------------------------------------------------------------

WIDTH, HEIGHT = 880, 332
PAD = 44
ACCENT_LIGHT = "#C25436"
ACCENT_DARK = "#E4704B"

LIGHT = {
    "bg": "#FAF8F5",
    "border": "#E8E2D8",
    "ink": "#17150F",
    "muted": "#7C756A",
    "rule": "#E4DED3",
    "empty": "#EFECE5",
}
DARK = {
    "bg": "#0F1319",
    "border": "#232A33",
    "ink": "#ECEFEE",
    "muted": "#8B949E",
    "rule": "#242B35",
    "empty": "#1A2028",
}

LABEL = "font-family:ui-monospace,SFMono-Regular,'SF Mono',Menlo,Consolas,monospace;font-size:9.5px;letter-spacing:1.6px"
MONO = "font-family:ui-monospace,SFMono-Regular,'SF Mono',Menlo,Consolas,monospace"
SANS = "font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif"

GRAPHQL = """
query($login:String!, $from:DateTime!, $to:DateTime!, $after:String) {
  user(login:$login) {
    contributionsCollection(from:$from, to:$to) {
      restrictedContributionsCount
      contributionCalendar {
        totalContributions
        weeks { contributionDays { date contributionCount } }
      }
    }
    repositories(first:100, after:$after, ownerAffiliations:OWNER, isFork:false) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {
        isPrivate
        stargazerCount
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

    cal = user["contributionsCollection"]["contributionCalendar"]
    days = [(d["date"][:10], d["contributionCount"]) for w in cal["weeks"] for d in w["contributionDays"]]

    return {
        "days": days,
        "total": cal["totalContributions"],
        "private_contribs": user["contributionsCollection"]["restrictedContributionsCount"],
        "repos": len(repos),
        "private_repos": sum(1 for r in repos if r["isPrivate"]),
        "stars": sum(r["stargazerCount"] for r in repos),
        "languages": languages(repos),
        "today": today,
    }


def languages(repos: list[dict]) -> list[tuple[str, int, str]]:
    totals: dict[str, int] = {}
    colors: dict[str, str] = {}
    for repo in repos:
        for edge in repo["languages"]["edges"]:
            name = edge["node"]["name"]
            totals[name] = totals.get(name, 0) + edge["size"]
            colors.setdefault(name, edge["node"]["color"] or "#8B949E")
    return [(n, s, colors[n]) for n, s in sorted(totals.items(), key=lambda kv: -kv[1])]


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


# --- drawing --------------------------------------------------------------


def mix(a: str, b: str, t: float) -> str:
    av = [int(a[i : i + 2], 16) for i in (1, 3, 5)]
    bv = [int(b[i : i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02X}" for x, y in zip(av, bv))


def esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def level_scale(bg: str, accent: str, ramp: tuple[float, ...]) -> list[str]:
    return [mix(bg, accent, t) for t in ramp]


def build(data: dict) -> str:
    today = data["today"]
    by_date = {dt.date.fromisoformat(d): c for d, c in data["days"]}

    # Sunday-aligned grid, 53 weeks. Same geometry GitHub itself uses.
    start = today - dt.timedelta(days=364)
    start -= dt.timedelta(days=(start.weekday() + 1) % 7)  # back to Sunday

    current, longest = streaks(data["days"], today)
    active = sum(1 for _, c in data["days"] if c > 0)
    cols = (today - start).days // 7 + 1

    cell, gap = 12, 3
    pitch = cell + gap
    grid_w = cols * pitch - gap
    grid_x, grid_y = PAD, 166
    grid_h = 7 * pitch - gap

    levels = sorted(c for _, c in data["days"] if c > 0) or [1]
    cuts = [levels[min(len(levels) - 1, int(len(levels) * q))] for q in (0.35, 0.6, 0.82)]

    def level(count: int) -> int:
        if count <= 0:
            return 0
        return 1 + bisect_right(cuts, count - 0.5)

    out: list[str] = []
    add = out.append

    add(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" '
        f'viewBox="0 0 {WIDTH} {HEIGHT}" role="img" aria-labelledby="t">'
    )
    add(f'<title id="t">{esc(data["login"])} — {data["total"]:,} contributions in the last year</title>')
    add("<style>")
    add(f"text{{{SANS};font-kerning:normal}}")
    add(".label{%s;font-size:9.5px;letter-spacing:1.6px;fill:%s}" % (LABEL, LIGHT["muted"]))
    add(".ink{fill:%s}.muted{fill:%s}.rule{stroke:%s}" % (LIGHT["ink"], LIGHT["muted"], LIGHT["rule"]))
    add(".card{fill:%s;stroke:%s}" % (LIGHT["bg"], LIGHT["border"]))
    add(".accent{fill:%s}" % ACCENT_LIGHT)
    for i, color in enumerate(level_scale(LIGHT["empty"], ACCENT_LIGHT, (0.18, 0.42, 0.68, 1.0))):
        add(f".l{i + 1}{{fill:{color}}}")
    add(f".l0{{fill:{LIGHT['empty']}}}")
    add("@media (prefers-color-scheme:dark){")
    add(".label{fill:%s}" % DARK["muted"])
    add(".ink{fill:%s}.muted{fill:%s}.rule{stroke:%s}" % (DARK["ink"], DARK["muted"], DARK["rule"]))
    add(".card{fill:%s;stroke:%s}" % (DARK["bg"], DARK["border"]))
    add(".accent{fill:%s}" % ACCENT_DARK)
    for i, color in enumerate(level_scale(DARK["empty"], ACCENT_DARK, (0.28, 0.52, 0.76, 1.0))):
        add(f".l{i + 1}{{fill:{color}}}")
    add(f".l0{{fill:{DARK['empty']}}}")
    add("}")
    add("</style>")

    add(f'<rect class="card" x=".5" y=".5" width="{WIDTH - 1}" height="{HEIGHT - 1}" rx="14"/>')

    # header: label, the one big number, the three streaks
    add(f'<text class="label" x="{PAD}" y="44">CONTRIBUTIONS</text>')
    add(f'<text class="ink" x="{PAD - 2}" y="108" style="{MONO};font-size:58px;font-weight:500;letter-spacing:-2px">{data["total"]:,}</text>')
    add(f'<text class="muted" x="{PAD}" y="128" style="{SANS};font-size:11.5px">in the last 12 months</text>')

    metrics = [
        ("CURRENT STREAK", current),
        ("LONGEST STREAK", longest),
        ("ACTIVE DAYS", active),
    ]
    for i, (label, value) in enumerate(metrics):
        y = 62 + i * 30
        add(f'<text class="label" x="648" y="{y}">{label}</text>')
        add(f'<text class="ink" x="{WIDTH - PAD}" y="{y + 6}" text-anchor="end" style="{MONO};font-size:21px;font-weight:500;letter-spacing:-.5px">{value}</text>')

    add(f'<line class="rule" x1="{PAD}" y1="146" x2="{WIDTH - PAD}" y2="146"/>')

    # month marks: one above the column that contains the 1st
    for col in range(cols):
        first = None
        for row in range(7):
            d = start + dt.timedelta(days=col * 7 + row)
            if d.day == 1:
                first = d
                break
        if not first:
            continue
        x = grid_x + col * pitch
        anchor = ' text-anchor="end"' if x + 22 > grid_x + grid_w else ""
        x = grid_x + grid_w if anchor else x
        add(
            f'<text class="label" x="{x}" y="{grid_y - 10}"{anchor} '
            f'style="letter-spacing:.8px">{first.strftime("%b").upper()}</text>'
        )

    rects = []
    for col in range(cols):
        for row in range(7):
            d = start + dt.timedelta(days=col * 7 + row)
            if d > today:
                continue
            count = by_date.get(d, 0)
            rects.append(
                f'<rect class="l{level(count)}" x="{grid_x + col * pitch}" y="{grid_y + row * pitch}" '
                f'width="{cell}" height="{cell}" rx="2.5"><title>{d:%Y-%m-%d}: {count}</title></rect>'
            )
    out.extend(rects)

    # language split
    langs = data["languages"][:5]
    total_bytes = sum(s for _, s, _ in data["languages"]) or 1
    bar_y, bar_h = grid_y + grid_h + 20, 8
    x = float(grid_x)
    for i, (_, size, _) in enumerate(langs):
        w = max(3.0, grid_w * size / total_bytes)
        cls = "accent" if i == 0 else "ink"
        op = [1.0, 0.42, 0.28, 0.18, 0.12][i]
        add(f'<rect class="{cls}" x="{x:.1f}" y="{bar_y}" width="{w:.1f}" height="{bar_h}" rx="4" opacity="{op}"/>')
        x += w

    legend_y = bar_y + 26
    lx = float(grid_x)
    for i, (name, size, _) in enumerate(langs):
        share = size / total_bytes * 100
        text = f"{name} {share:.0f}%"
        op = [1.0, 0.42, 0.28, 0.18, 0.12][i]
        cls = "accent" if i == 0 else "ink"
        add(f'<rect class="{cls}" x="{lx:.1f}" y="{legend_y - 8}" width="7" height="7" rx="1.5" opacity="{op}"/>')
        add(f'<text class="label" x="{lx + 12:.1f}" y="{legend_y}" style="letter-spacing:.7px">{esc(text)}</text>')
        lx += 12 + len(text) * 5.9 + 20

    if data["private_repos"]:
        add(
            f'<text class="label" x="{WIDTH - PAD}" y="{legend_y}" text-anchor="end" '
            f'style="letter-spacing:.7px" opacity=".75">INCL. {data["private_repos"]} PRIVATE REPOS</text>'
        )

    add("</svg>")
    return "\n".join(out)


def preview(svg: str, path: str) -> None:
    page = f"""<!doctype html><meta charset="utf-8"><title>profile card</title>
<style>
  body{{margin:0;padding:40px;display:grid;gap:32px;justify-content:center;
        font:13px ui-monospace,Menlo,monospace;background:#fff;color:#666}}
  .light{{background:#fff;padding:32px;border-radius:10px}}
  .dark{{background:#0d1117;padding:32px;border-radius:10px}}
  h2{{font:11px ui-monospace,Menlo,monospace;letter-spacing:1.5px;margin:0 0 16px;color:#999}}
</style>
<div class="light"><h2>LIGHT</h2>{svg}</div>
<div class="dark" style="color-scheme:dark"><h2>DARK</h2>{svg}</div>
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

    cur, longest = streaks(data["days"], data["today"])
    print(
        f"{data['total']:,} contributions · {cur}d current · {longest}d longest · "
        f"{data['repos']} repos ({data['private_repos']} private) → {args.out}"
    )


if __name__ == "__main__":
    main()
