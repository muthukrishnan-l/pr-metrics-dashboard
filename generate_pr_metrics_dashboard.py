#!/usr/bin/env python3
"""
Generate the Scrum Teams PR Metrics Dashboard (static HTML for GitHub Pages).

Data source:
- GitHub Pulls API: each repo is listed once (newest first, stopping at the lookback window)
- GitHub Reviews API: "changes requested" details, only for team-labeled PRs in the window
- Team ownership and approvals come from PR labels (pd-team-<name>, has-*-approval)

Usage:
  GITHUB_TOKEN=<pat> python3 generate_pr_metrics_dashboard.py

Env vars:
  GITHUB_TOKEN or GH_TOKEN   Required. PAT with repo scope, SSO-authorized for blackboard-learn
  DASHBOARD_DAYS             Lookback window in days (default: 60)
  DASHBOARD_OUTPUT           Output HTML path (default: docs/index.html)
  ADO_ORG_URL / ADO_PROJECT  Azure DevOps links for AB#12345 tokens in PR titles
"""

from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

TEAMS: List[str] = [
    "Sparrow",
    "Jupiter",
    "Karikalas",
    "Shine",
    "Mamallas",
    "Trailblazers",
    "Sparkle",
    "Madras",
    "Triton",
    "Helios",
    "Krypton",
    "Radon",
]

REPOS: List[str] = [
    "blackboard-learn/learn",
    "blackboard-learn/ultra",
]

APPROVAL_LABELS: List[Tuple[str, str]] = [
    ("has-team-approval", "Team"),
    ("has-tech-lead-review", "Tech Lead"),
    ("has-ces-approval", "CES"),
    ("has-value-stream-approval", "VS"),
]

DEFAULT_DAYS = 60
DEFAULT_OUTPUT_PATH = "docs/index.html"
MAX_PAGES_PER_REPO = 30  # 3,000 PRs safety cap
ADO_ORG_URL = (os.getenv("ADO_ORG_URL") or os.getenv("AZDO_ORG_URL") or "").rstrip("/")
ADO_PROJECT = os.getenv("ADO_PROJECT") or os.getenv("AZDO_PROJECT") or ""


@dataclass
class PullRow:
    team: str
    number: int
    repo: str
    title: str
    author: str
    state: str
    created_at: str
    merged_at: str
    closed_at: str
    approvals: str
    pending_approvals: str
    changes_requested_by: str
    merge_ready: str
    url: str


class GitHubClient:
    def __init__(self, token: str) -> None:
        self.base = "https://api.github.com"
        self.headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "scrum-pr-dashboard-generator",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def get_json(self, path: str, params: Optional[Dict[str, str]] = None):
        url = path if path.startswith("http") else f"{self.base}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers=self.headers)
        for attempt in range(1, 4):
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:300]
                retry_after = exc.headers.get("Retry-After")
                if exc.code in (403, 429) and retry_after and attempt < 3:
                    time.sleep(min(int(retry_after), 60))
                    continue
                if exc.code >= 500 and attempt < 3:
                    time.sleep(2 * attempt)
                    continue
                raise RuntimeError(f"GitHub API error {exc.code} for {url}: {detail}") from exc
            except (urllib.error.URLError, ssl.SSLError) as exc:
                if attempt == 3:
                    raise RuntimeError(f"GitHub API network error for {url}: {exc}") from exc
                time.sleep(1.5 * attempt)
        raise RuntimeError(f"GitHub API request failed for {url}")

    def list_recent_prs(self, repo: str, since: dt.date) -> List[dict]:
        """List PRs newest-first and stop once they are older than `since`."""
        items: List[dict] = []
        for page in range(1, MAX_PAGES_PER_REPO + 1):
            batch = self.get_json(
                f"/repos/{repo}/pulls",
                {"state": "all", "sort": "created", "direction": "desc",
                 "per_page": "100", "page": str(page)},
            )
            if not isinstance(batch, list) or not batch:
                break
            for pr in batch:
                if created_date(pr) < since:
                    return items
                items.append(pr)
            if len(batch) < 100:
                break
            time.sleep(0.1)
        return items


def created_date(pr: dict) -> dt.date:
    return dt.date.fromisoformat((pr.get("created_at") or "1970-01-01")[:10])


def date_only(value: Optional[str]) -> str:
    return value[:10] if value else ""


def classify_state(pr: dict) -> str:
    if pr.get("merged_at"):
        return "Merged"
    if pr.get("state") == "closed":
        return "Closed"
    if pr.get("draft"):
        return "Draft"
    return "Open"


def build_approval_strings(labels: Iterable[str]) -> Tuple[str, str]:
    label_set = {l.lower() for l in labels}
    approved = [f"{name} ✓" for key, name in APPROVAL_LABELS if key in label_set]
    pending = [f"{name} Approval" for key, name in APPROVAL_LABELS if key not in label_set]
    return (" ".join(approved) or "None yet"), (", ".join(pending) or "All approved ✓")


def latest_changes_requested_by(reviews: List[dict]) -> str:
    latest: Dict[str, str] = {}
    for r in reviews:
        user = (r.get("user") or {}).get("login")
        state = (r.get("state") or "").upper()
        # COMMENTED reviews don't clear an earlier change request
        if user and state in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest[user] = state
    requesters = sorted(u for u, st in latest.items() if st == "CHANGES_REQUESTED")
    return ", ".join(requesters) or "-"


def team_for_labels(labels: Iterable[str]) -> Optional[str]:
    """Map labels like pd-team-sparrow / PD Team: Sparrow / sparrow-team to a team."""
    for raw in labels:
        norm = re.sub(r"[^a-z0-9]+", "-", (raw or "").lower()).strip("-")
        if not norm:
            continue
        for team in TEAMS:
            key = team.lower()
            if norm == f"pd-team-{key}" or (key in norm.split("-") and ("team" in norm or norm == key)):
                return team
    return None


def extract_work_item_id(title: str) -> str:
    match = re.search(r"\bAB#(\d+)\b", title)
    return match.group(1) if match else ""


def ado_work_item_url(work_item_id: str) -> str:
    if not work_item_id or not ADO_ORG_URL:
        return ""
    if ADO_PROJECT:
        return f"{ADO_ORG_URL}/{urllib.parse.quote(ADO_PROJECT, safe='')}/_workitems/edit/{work_item_id}"
    return f"{ADO_ORG_URL}/_workitems/edit/{work_item_id}"


def collect_rows(client: GitHubClient, since: dt.date) -> Tuple[Dict[str, List[PullRow]], List[str]]:
    team_rows: Dict[str, List[PullRow]] = {t: [] for t in TEAMS}
    errors: List[str] = []

    for repo in REPOS:
        try:
            prs = client.list_recent_prs(repo, since)
        except Exception as exc:
            errors.append(f"{repo}: {exc}")
            print(f"ERROR: could not list PRs for {repo}: {exc}", file=sys.stderr)
            continue

        matched = 0
        observed: set = set()
        for pr in prs:
            labels = [l.get("name", "") for l in pr.get("labels", [])]
            observed.update(labels)
            team = team_for_labels(labels)
            if not team:
                continue
            matched += 1

            number = int(pr["number"])
            try:
                reviews = client.get_json(f"/repos/{repo}/pulls/{number}/reviews", {"per_page": "100"})
            except Exception as exc:
                print(f"Warning: reviews unavailable for {repo}#{number}: {exc}", file=sys.stderr)
                reviews = []

            approvals, pending = build_approval_strings(labels)
            changes = latest_changes_requested_by(reviews if isinstance(reviews, list) else [])
            state = classify_state(pr)
            merge_ready = "-"
            if state == "Open":
                merge_ready = "✓ Yes" if pending == "All approved ✓" and changes == "-" else "✗ No"

            team_rows[team].append(PullRow(
                team=team,
                number=number,
                repo=repo,
                title=pr.get("title", ""),
                author=(pr.get("user") or {}).get("login") or "-",
                state=state,
                created_at=date_only(pr.get("created_at")),
                merged_at=date_only(pr.get("merged_at")),
                closed_at=date_only(pr.get("closed_at")),
                approvals=approvals,
                pending_approvals=pending,
                changes_requested_by=changes,
                merge_ready=merge_ready,
                url=pr.get("html_url", ""),
            ))
            time.sleep(0.05)

        print(f"{repo}: {len(prs)} PRs in window, {matched} with a team label")
        if prs and not matched:
            sample = ", ".join(sorted(l for l in observed if l)[:15]) or "none"
            print(f"Warning: no team labels matched in {repo}. Labels seen: {sample}", file=sys.stderr)

    for rows in team_rows.values():
        rows.sort(key=lambda r: r.created_at, reverse=True)
    return team_rows, errors


def summarize(rows: List[PullRow]) -> Dict[str, int]:
    counts = {"Total": len(rows), "Open": 0, "Draft": 0, "Merged": 0, "Closed": 0}
    for r in rows:
        counts[r.state] += 1
    return counts


def render_row(r: PullRow) -> str:
    e = html.escape
    repo_short = "Learn" if r.repo.endswith("/learn") else "Ultra"
    wid = extract_work_item_id(r.title)
    if wid:
        url = ado_work_item_url(wid)
        work_item = f'<a href="{e(url)}" target="_blank">AB#{wid}</a>' if url else f"AB#{wid}"
    else:
        work_item = "-"
    ready_cls = "ok" if r.merge_ready.startswith("✓") else ("no" if r.merge_ready.startswith("✗") else "")
    return f"""
        <tr data-state="{r.state.lower()}" data-created="{r.created_at}" data-changes="{e(r.changes_requested_by.lower())}">
          <td><a href="{e(r.url)}" target="_blank">#{r.number}</a><br/><small class="wi">{work_item}</small></td>
          <td>{e(r.title)}</td>
          <td>{e(r.author)}</td>
          <td><span class="pill {r.state.lower()}">{r.state}</span> <span class="repo">{repo_short}</span></td>
          <td>{r.created_at or "-"}</td>
          <td>{r.merged_at or "-"}</td>
          <td>{r.closed_at or "-"}</td>
          <td>{e(r.approvals)}</td>
          <td>{e(r.pending_approvals)}</td>
          <td>{e(r.changes_requested_by)}</td>
          <td class="{ready_cls}">{r.merge_ready}</td>
        </tr>"""


def render_dashboard(team_rows: Dict[str, List[PullRow]], generated_at: str, days: int, errors: List[str]) -> str:
    totals = summarize([r for rows in team_rows.values() for r in rows])

    sections = []
    for team in TEAMS:
        rows = team_rows.get(team, [])
        s = summarize(rows)
        body = "".join(render_row(r) for r in rows) or '<tr class="__none"><td colspan="11">No PRs found for selected range.</td></tr>'
        sections.append(f"""
    <section class="team-section" data-team="{team}">
      <h2>{team} Team
        <span class="team-summary">Total: {s['Total']} | Open: {s['Open']} | Draft: {s['Draft']} | Merged: {s['Merged']} | Closed: {s['Closed']}</span>
      </h2>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>PR</th><th>Title</th><th>Author</th><th>State</th><th>Created</th>
              <th>Merged</th><th>Closed</th><th>Approvals</th><th>Pending Approval</th><th>Changes Requested By</th><th>Merge Ready</th>
            </tr>
          </thead>
          <tbody>{body}
          </tbody>
        </table>
      </div>
    </section>""")

    banner = ""
    if errors:
        items = "".join(f"<li>{html.escape(e)}</li>" for e in errors)
        banner = f'<div class="error-banner"><strong>Some data could not be fetched.</strong> Check the DASHBOARD_GH_TOKEN secret (repo scope, SSO-authorized).<ul>{items}</ul></div>'

    team_options = "".join(f'<option value="{t}">{t}</option>' for t in TEAMS)
    day_options = "".join(
        f'<option value="{d}"{" selected" if d == min(30, days) else ""}>Last {d} days</option>'
        for d in (7, 14, 30, 60, 90) if d <= days
    )

    return TEMPLATE.format(
        days=days,
        generated_at=generated_at,
        team_list=" | ".join(TEAMS),
        total=totals["Total"], open=totals["Open"], draft=totals["Draft"],
        merged=totals["Merged"], closed=totals["Closed"],
        team_options=team_options,
        day_options=day_options,
        banner=banner,
        sections="".join(sections),
    )


TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>PR Metrics Dashboard</title>
  <style>
    :root {{
      --bg: #f2f5f9; --ink: #1c2533; --muted: #5a6472; --card: #ffffff; --line: #d9e1ea;
      --accent: #0d9488; --accent-2: #2563eb; --warn: #b45309; --ok: #0f766e; --bad: #b91c1c;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Segoe UI", "Trebuchet MS", Tahoma, sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at 20% -10%, #d1fae5 0%, transparent 38%),
        radial-gradient(circle at 90% 0%, #dbeafe 0%, transparent 35%),
        var(--bg);
    }}
    .container {{ max-width: 1500px; margin: 0 auto; padding: 18px; }}
    .hero {{
      background: linear-gradient(135deg, #0f766e, #1d4ed8);
      color: #fff; border-radius: 16px; padding: 18px 22px;
      box-shadow: 0 12px 34px rgba(22, 43, 75, 0.2); margin-bottom: 14px;
    }}
    .hero h1 {{ margin: 0 0 8px; font-size: 28px; letter-spacing: 0.3px; }}
    .meta {{ font-size: 14px; opacity: 0.96; }}
    .team-list {{ margin-top: 8px; font-size: 14px; }}
    .hero button {{ border: 0; border-radius: 10px; padding: 9px 14px; background: #f8fafc; color: #0f172a; font-weight: 700; cursor: pointer; margin-top: 10px; }}

    .error-banner {{ background: #fef2f2; border: 1px solid #fecaca; color: var(--bad); border-radius: 12px; padding: 10px 14px; margin-bottom: 12px; font-size: 14px; }}
    .error-banner ul {{ margin: 6px 0 0; padding-left: 18px; font-size: 12px; }}

    .summary {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 10px; margin: 12px 0; }}
    .kpi {{ background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 10px 12px; box-shadow: 0 3px 12px rgba(20, 28, 45, 0.05); }}
    .kpi .label {{ font-size: 12px; color: var(--muted); text-transform: uppercase; }}
    .kpi .value {{ font-size: 24px; font-weight: 700; margin-top: 3px; font-variant-numeric: tabular-nums; }}

    .filters {{
      display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 10px;
      background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 12px; margin-bottom: 12px;
    }}
    label {{ display: block; font-size: 12px; color: var(--muted); margin-bottom: 4px; text-transform: uppercase; }}
    select, input {{ width: 100%; border: 1px solid var(--line); border-radius: 9px; padding: 8px 10px; font-size: 14px; background: #fff; }}

    .team-section {{ background: var(--card); border: 1px solid var(--line); border-radius: 12px; margin: 10px 0 14px; overflow: hidden; box-shadow: 0 3px 12px rgba(20, 28, 45, 0.05); }}
    .team-section h2 {{ margin: 0; padding: 12px; font-size: 19px; border-bottom: 1px solid var(--line); background: #f8fafc; }}
    .team-summary {{ font-size: 13px; color: var(--muted); margin-left: 10px; font-weight: 500; }}

    .table-wrap {{ width: 100%; overflow-x: auto; }}
    table {{ border-collapse: collapse; width: 100%; min-width: 1300px; font-size: 13px; }}
    th, td {{ border-bottom: 1px solid var(--line); padding: 8px 9px; text-align: left; vertical-align: top; }}
    th {{ background: #eef2ff; position: sticky; top: 0; z-index: 1; }}
    tbody tr:nth-child(odd) {{ background: #fcfdff; }}
    a {{ color: var(--accent-2); text-decoration: none; font-weight: 600; }}
    a:hover {{ text-decoration: underline; }}
    .wi, .wi a {{ color: #94a3b8; font-size: 11px; font-weight: normal; }}
    .pill {{ display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 12px; font-weight: 600; }}
    .pill.open {{ background: #dbeafe; color: #1d4ed8; }}
    .pill.draft {{ background: #f1f5f9; color: #475569; }}
    .pill.merged {{ background: #ede9fe; color: #6d28d9; }}
    .pill.closed {{ background: #fee2e2; color: #b91c1c; }}
    .repo {{ color: var(--muted); font-size: 12px; }}
    td.ok {{ color: var(--ok); font-weight: 600; }}
    td.no {{ color: var(--warn); font-weight: 600; }}

    .footer {{ color: var(--muted); font-size: 12px; margin-top: 10px; }}

    @media (max-width: 760px) {{
      .container {{ padding: 16px; }}
      .hero h1 {{ font-size: 22px; }}
      .team-summary {{ display: block; margin: 6px 0 0; }}
    }}
  </style>
</head>
<body>
  <div class="container">
    <header class="hero">
      <h1>Scrum Teams PR Metrics Dashboard</h1>
      <div class="meta">Repos: blackboard-learn/learn + blackboard-learn/ultra | Last {days} days fetched</div>
      <div class="meta">Last refresh: {generated_at}</div>
      <button id="refreshBtn">Refresh Latest Status</button>
      <div class="team-list">Teams: {team_list}</div>
    </header>

    {banner}

    <div class="summary">
      <div class="kpi"><div class="label">Total PRs</div><div class="value" id="kTotal">{total}</div></div>
      <div class="kpi"><div class="label">Open</div><div class="value" id="kOpen">{open}</div></div>
      <div class="kpi"><div class="label">Draft</div><div class="value" id="kDraft">{draft}</div></div>
      <div class="kpi"><div class="label">Merged</div><div class="value" id="kMerged">{merged}</div></div>
      <div class="kpi"><div class="label">Closed</div><div class="value" id="kClosed">{closed}</div></div>
    </div>

    <section class="filters">
      <div>
        <label for="teamFilter">Filter by Team</label>
        <select id="teamFilter"><option value="ALL">All Teams</option>{team_options}</select>
      </div>
      <div>
        <label for="stateFilter">State</label>
        <select id="stateFilter">
          <option value="ALL">All States</option>
          <option value="open">Open</option>
          <option value="draft">Draft</option>
          <option value="merged">Merged</option>
          <option value="closed">Closed</option>
        </select>
      </div>
      <div>
        <label for="changesFilter">Changes Requested By</label>
        <input id="changesFilter" type="text" placeholder="e.g. user-login" />
      </div>
      <div>
        <label for="daysFilter">Time Range</label>
        <select id="daysFilter">{day_options}<option value="36500">All fetched</option></select>
      </div>
    </section>
{sections}

    <div class="footer">Approval legend: Team ✓, Tech Lead ✓, CES ✓, VS ✓ · Merge Ready = open, all four approvals, no outstanding change requests</div>
  </div>

<script>
(function () {{
  const $ = (id) => document.getElementById(id);
  const teamFilter = $('teamFilter'), stateFilter = $('stateFilter'), changesFilter = $('changesFilter'), daysFilter = $('daysFilter');
  const sections = Array.from(document.querySelectorAll('.team-section'));

  function withinDays(created, days) {{
    if (!created) return false;
    const d = new Date(created + 'T00:00:00');
    if (Number.isNaN(d.getTime())) return false;
    const from = new Date(); from.setHours(0, 0, 0, 0); from.setDate(from.getDate() - days);
    return d >= from;
  }}

  function applyFilters() {{
    const team = teamFilter.value, state = stateFilter.value;
    const q = changesFilter.value.trim().toLowerCase();
    const days = Number(daysFilter.value || '30');
    const totals = {{ total: 0, open: 0, draft: 0, merged: 0, closed: 0 }};

    sections.forEach((section) => {{
      const show = team === 'ALL' || section.dataset.team === team;
      section.style.display = show ? '' : 'none';
      if (!show) return;

      let visible = 0;
      section.querySelectorAll('tbody tr[data-state]').forEach((row) => {{
        const ok = (state === 'ALL' || row.dataset.state === state)
          && withinDays(row.dataset.created, days)
          && (!q || row.dataset.changes.includes(q));
        row.style.display = ok ? '' : 'none';
        if (ok) {{ visible++; totals.total++; totals[row.dataset.state]++; }}
      }});

      const none = section.querySelector('tr.__none');
      if (none) none.style.display = 'none';
      let empty = section.querySelector('tr.__empty');
      if (!empty) {{
        empty = document.createElement('tr');
        empty.className = '__empty';
        empty.innerHTML = '<td colspan="11">No PRs match the current filters.</td>';
        section.querySelector('tbody').appendChild(empty);
      }}
      empty.style.display = visible ? 'none' : '';
    }});

    $('kTotal').textContent = totals.total; $('kOpen').textContent = totals.open;
    $('kDraft').textContent = totals.draft; $('kMerged').textContent = totals.merged;
    $('kClosed').textContent = totals.closed;
  }}

  [teamFilter, stateFilter, changesFilter, daysFilter].forEach((el) => el.addEventListener('input', applyFilters));

  $('refreshBtn').addEventListener('click', (e) => {{
    e.target.disabled = true; e.target.textContent = 'Refreshing...';
    const u = new URL(window.location.href);
    u.searchParams.set('refresh', String(Date.now()));
    window.location.href = u.toString();
  }});

  applyFilters();
}})();
</script>
</body>
</html>
"""


def main() -> int:
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    if not token:
        print("ERROR: set GITHUB_TOKEN (PAT with repo scope, SSO-authorized for blackboard-learn).", file=sys.stderr)
        return 1

    days = int(os.getenv("DASHBOARD_DAYS", str(DEFAULT_DAYS)))
    output_path = os.getenv("DASHBOARD_OUTPUT", DEFAULT_OUTPUT_PATH)
    now = dt.datetime.now(dt.timezone.utc)
    since = (now - dt.timedelta(days=days)).date()

    team_rows, errors = collect_rows(GitHubClient(token), since)

    # Don't publish an empty page over a good one when every repo failed
    if len(errors) == len(REPOS):
        print("ERROR: all repositories failed to load; check token scope and SSO authorization.", file=sys.stderr)
        return 2

    html_text = render_dashboard(team_rows, now.strftime("%Y-%m-%d %H:%M:%S UTC"), days, errors)

    out_dir = os.path.dirname(output_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, ".nojekyll"), "w", encoding="utf-8") as f:
            f.write("\n")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_text)

    total = sum(len(v) for v in team_rows.values())
    print(f"Dashboard generated: {output_path} | PR rows: {total} | Lookback: {days} days | Fetch errors: {len(errors)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
