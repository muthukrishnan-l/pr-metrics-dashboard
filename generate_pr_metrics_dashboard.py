#!/usr/bin/env python3
"""
Generate a multi-team PR metrics dashboard similar to the Galaxy dashboard.

Data source:
- GitHub Pulls API for PR discovery by repository
- GitHub Reviews API for change-request details
- Team ownership and approvals derived from PR labels

Usage:
  python generate_pr_metrics_dashboard.py

Optional env vars:
  GITHUB_TOKEN or GH_TOKEN   GitHub token (recommended to avoid low rate limits)
  DASHBOARD_DAYS             Lookback window in days (default: 30)
  DASHBOARD_OUTPUT           Output HTML path (default: docs/index.html)
"""

from __future__ import annotations

import datetime as dt
import html
import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Dict, Iterable, List, Tuple

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

DEFAULT_DAYS = 30
DEFAULT_OUTPUT_PATH = "docs/index.html"
LEGACY_OUTPUT_PATH = "pr_metrics_dashboard.html"


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
    def __init__(self, token: str | None = None) -> None:
        self.base = "https://api.github.com"
        self.headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "pr-metrics-dashboard-generator",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            self.headers["Authorization"] = f"Bearer {token}"

    def get_json(self, path_or_url: str, params: Dict[str, str] | None = None) -> dict:
      url = path_or_url if path_or_url.startswith("http") else f"{self.base}{path_or_url}"
      if params:
        query = urllib.parse.urlencode(params)
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}{query}"

      req = urllib.request.Request(url, headers=self.headers)
      retries = 3
      for attempt in range(1, retries + 1):
        try:
          with urllib.request.urlopen(req, timeout=60) as resp:
            payload = resp.read().decode("utf-8")
            return json.loads(payload)
        except urllib.error.HTTPError as exc:
          detail = exc.read().decode("utf-8", errors="replace")
          raise RuntimeError(f"GitHub API error {exc.code} for {url}: {detail}") from exc
        except (urllib.error.URLError, ssl.SSLError) as exc:
          if attempt == retries:
            raise RuntimeError(f"GitHub API network error for {url}: {exc}") from exc
          time.sleep(1.5 * attempt)

      raise RuntimeError(f"GitHub API request failed for {url}")

    def get_prs(self, repo: str, since: str = None) -> List[dict]:
        """Fetch all PRs from a repo using /repos/{owner}/{repo}/pulls endpoint."""
        items: List[dict] = []
        page = 1
        params: Dict[str, str] = {"state": "all", "per_page": "100", "page": str(page)}
        if since:
            params["sort"] = "created"
            params["direction"] = "desc"
        
        while True:
            data = self.get_json(
                f"/repos/{repo}/pulls",
                params=params,
            )
            if not isinstance(data, list):
                break
            batch = data
            if not batch:
                break
            items.extend(batch)
            if len(batch) < 100:
                break
            page += 1
            if page > 10:  # limit to 1000 PRs max per repo
                break
            params["page"] = str(page)
            time.sleep(0.15)
        return items


def parse_iso_date(value: str | None) -> str:
    if not value:
        return ""
    return value[:10]


def display_date(value: str) -> str:
    if not value:
        return "-"
    return value


def classify_state(pr: dict) -> str:
    if pr.get("draft"):
        return "Draft"
    if pr.get("merged_at"):
        return "Merged"
    if pr.get("state") == "closed":
        return "Closed"
    return "Open"


def build_approval_strings(labels: Iterable[str]) -> Tuple[str, str]:
    label_set = set(labels)
    approved_parts: List[str] = []
    pending_parts: List[str] = []
    for key, display in APPROVAL_LABELS:
        if key in label_set:
            approved_parts.append(f"{display} ✓")
        else:
            pending_parts.append(f"{display} Approval")

    approvals = " ".join(approved_parts) if approved_parts else "None yet"
    pending = ", ".join(pending_parts) if pending_parts else "All approved ✓"
    return approvals, pending


def latest_changes_requested_by(reviews: List[dict]) -> str:
    latest_by_user: Dict[str, str] = {}
    for r in reviews:
        user = (r.get("user") or {}).get("login")
        state = (r.get("state") or "").upper()
        if not user:
            continue
        latest_by_user[user] = state

    requesters = sorted([u for u, st in latest_by_user.items() if st == "CHANGES_REQUESTED"])
    return ", ".join(requesters) if requesters else "-"


def fetch_team_rows(client: GitHubClient, team: str, since: str) -> List[PullRow]:
    rows: List[PullRow] = []
    seen: set[Tuple[str, int]] = set()
    team_label = f"pd-team-{team.lower()}"
    since_date = dt.datetime.strptime(since, "%Y-%m-%d").date()

    for repo in REPOS:
        try:
            items = client.get_prs(repo, since=since)
        except Exception as exc:
            print(f"Warning: failed to fetch PR list for {team} in {repo}: {exc}")
            continue

        for item in items:
            # Filter by team label locally
            labels = [l.get("name", "") for l in item.get("labels", [])]
            if team_label not in labels:
                continue

            # Filter by date
            created_str = item.get("created_at", "")
            if created_str:
                created_date = dt.datetime.fromisoformat(created_str.replace('Z', '+00:00')).date()
                if created_date < since_date:
                    continue

            number = int(item.get("number"))
            repo_name = repo
            key = (repo_name, number)
            if key in seen:
                continue
            seen.add(key)

            try:
                reviews = client.get_json(f"/repos/{repo_name}/pulls/{number}/reviews")
            except Exception as exc:
                print(f"Warning: failed to fetch reviews for {repo_name}#{number}: {exc}")
                reviews = []

            approvals, pending_approvals = build_approval_strings(labels)
            changes_requested_by = latest_changes_requested_by(reviews if isinstance(reviews, list) else [])
            state = classify_state(item)  # Use item directly, it's the full PR object

            merge_ready = "-"
            if state == "Open":
                has_pending = pending_approvals != "All approved ✓"
                has_changes_requested = changes_requested_by != "-"
                merge_ready = "✓ Yes" if (not has_pending and not has_changes_requested) else "✗ No"

            rows.append(
                PullRow(
                    team=team,
                    number=number,
                    repo=repo_name,
                    title=item.get("title", ""),
                    author=((item.get("user") or {}).get("login") or "-"),
                    state=state,
                    created_at=parse_iso_date(item.get("created_at")),
                    merged_at=parse_iso_date(item.get("merged_at")),
                    closed_at=parse_iso_date(item.get("closed_at")),
                    approvals=approvals,
                    pending_approvals=pending_approvals,
                    changes_requested_by=changes_requested_by,
                    merge_ready=merge_ready,
                    url=item.get("html_url", ""),
                )
            )

            time.sleep(0.12)

    rows.sort(key=lambda x: x.created_at, reverse=True)
    return rows


def summarize(rows: List[PullRow]) -> Dict[str, int]:
    counts = {"Total": len(rows), "Open": 0, "Draft": 0, "Merged": 0, "Closed": 0}
    for r in rows:
        counts[r.state] += 1
    return counts


def render_dashboard(team_to_rows: Dict[str, List[PullRow]], generated_at: str, days: int) -> str:
    all_rows = [r for rows in team_to_rows.values() for r in rows]
    totals = summarize(all_rows)

    sections: List[str] = []
    for team in TEAMS:
        rows = team_to_rows.get(team, [])
        s = summarize(rows)

        table_rows: List[str] = []
        for r in rows:
            title = html.escape(r.title)
            author = html.escape(r.author)
            repo_short = "Learn" if r.repo.endswith("/learn") else "Ultra"
            approvals = html.escape(r.approvals)
            pending = html.escape(r.pending_approvals)
            changes = html.escape(r.changes_requested_by)

            state_lower = r.state.lower()
            changes_data = r.changes_requested_by.lower().replace('"', "")

            table_rows.append(
                f"""
                <tr data-state=\"{state_lower}\" data-created=\"{r.created_at}\" data-changes=\"{changes_data}\">
                  <td><a href=\"{html.escape(r.url)}\" target=\"_blank\">#{r.number}</a></td>
                  <td>{title}</td>
                  <td>{author}</td>
                  <td>{r.state} {repo_short}</td>
                  <td>{display_date(r.created_at)}</td>
                  <td>{display_date(r.merged_at)}</td>
                  <td>{display_date(r.closed_at)}</td>
                  <td>{approvals}</td>
                  <td>{pending}</td>
                  <td>{changes}</td>
                  <td>{r.merge_ready}</td>
                </tr>
                """
            )

        sections.append(
            f"""
            <section class=\"team-section\" data-team=\"{team}\">
              <h2>{team} Team
                <span class=\"team-summary\">Total: {s['Total']} | Open: {s['Open']} | Draft: {s['Draft']} | Merged: {s['Merged']} | Closed: {s['Closed']}</span>
              </h2>
              <div class=\"table-wrap\">
                <table>
                  <thead>
                    <tr>
                      <th>PR</th><th>Title</th><th>Author</th><th>State</th><th>Created</th>
                      <th>Merged</th><th>Closed</th><th>Approvals</th><th>Pending Approval</th><th>Changes Requested By</th><th>Merge Ready</th>
                    </tr>
                  </thead>
                  <tbody>
                    {''.join(table_rows) if table_rows else '<tr><td colspan="11">No PRs found for selected range.</td></tr>'}
                  </tbody>
                </table>
              </div>
            </section>
            """
        )

    team_options = "".join([f"<option value=\"{t}\">{t}</option>" for t in TEAMS])

    return f"""<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width,initial-scale=1\" />
  <title>PR Metrics Dashboard</title>
  <style>
    :root {{
      --bg: #f2f5f9;
      --ink: #1c2533;
      --muted: #5a6472;
      --card: #ffffff;
      --line: #d9e1ea;
      --accent: #0d9488;
      --accent-2: #2563eb;
      --warn: #b45309;
      --ok: #0f766e;
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
      color: #fff;
      border-radius: 16px;
      padding: 18px 22px;
      box-shadow: 0 12px 34px rgba(22, 43, 75, 0.2);
      margin-bottom: 14px;
    }}
    .hero h1 {{ margin: 0 0 8px; font-size: 28px; letter-spacing: 0.3px; }}
    .meta {{ font-size: 14px; opacity: 0.96; }}
    .team-list {{ margin-top: 8px; font-size: 14px; }}

    .summary {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
      gap: 10px;
      margin: 12px 0;
    }}
    .kpi {{
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 10px 12px;
      box-shadow: 0 3px 12px rgba(20, 28, 45, 0.05);
    }}
    .kpi .label {{ font-size: 12px; color: var(--muted); text-transform: uppercase; }}
    .kpi .value {{ font-size: 24px; font-weight: 700; margin-top: 3px; }}

    .filters {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 10px;
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 12px;
      margin-bottom: 12px;
    }}
    label {{ display: block; font-size: 12px; color: var(--muted); margin-bottom: 4px; text-transform: uppercase; }}
    select, input {{
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 9px;
      padding: 8px 10px;
      font-size: 14px;
      background: #fff;
    }}

    .team-section {{
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 12px;
      margin: 10px 0 14px;
      overflow: hidden;
      box-shadow: 0 3px 12px rgba(20, 28, 45, 0.05);
    }}
    .team-section h2 {{
      margin: 0;
      padding: 12px;
      font-size: 19px;
      border-bottom: 1px solid var(--line);
      background: #f8fafc;
    }}
    .team-summary {{
      font-size: 13px;
      color: var(--muted);
      margin-left: 10px;
      font-weight: 500;
    }}

    .table-wrap {{ width: 100%; overflow-x: auto; }}
    table {{ border-collapse: collapse; width: 100%; min-width: 1300px; font-size: 13px; }}
    th, td {{ border-bottom: 1px solid var(--line); padding: 8px 9px; text-align: left; vertical-align: top; }}
    th {{ background: #eef2ff; position: sticky; top: 0; z-index: 1; }}
    tbody tr:nth-child(odd) {{ background: #fcfdff; }}
    a {{ color: var(--accent-2); text-decoration: none; font-weight: 600; }}
    a:hover {{ text-decoration: underline; }}

    .footer {{ color: var(--muted); font-size: 12px; margin-top: 10px; }}

    @media (max-width: 760px) {{
      .hero h1 {{ font-size: 22px; }}
      .team-summary {{ display: block; margin: 6px 0 0; }}
    }}
  </style>
</head>
<body>
  <div class=\"container\">
    <header class=\"hero\">
      <h1>Scrum Teams PR Metrics Dashboard</h1>
      <div class=\"meta\">Repos: blackboard-learn/learn + blackboard-learn/ultra | Last {days} days</div>
      <div class=\"meta\" id=\"lastRefresh\">Last refresh: {generated_at}</div>
      <div style=\"margin-top:10px\">
        <button id=\"refreshBtn\" style=\"border:0;border-radius:10px;padding:9px 14px;background:#f8fafc;color:#0f172a;font-weight:700;cursor:pointer\">Refresh Latest Status</button>
      </div>
      <div class=\"team-list\">Teams: {' | '.join(TEAMS)}</div>
    </header>

    <div class=\"summary\">
      <div class=\"kpi\"><div class=\"label\">Total PRs</div><div class=\"value\">{totals['Total']}</div></div>
      <div class=\"kpi\"><div class=\"label\">Open</div><div class=\"value\">{totals['Open']}</div></div>
      <div class=\"kpi\"><div class=\"label\">Draft</div><div class=\"value\">{totals['Draft']}</div></div>
      <div class=\"kpi\"><div class=\"label\">Merged</div><div class=\"value\">{totals['Merged']}</div></div>
      <div class=\"kpi\"><div class=\"label\">Closed</div><div class=\"value\">{totals['Closed']}</div></div>
    </div>

    <section class=\"filters\">
      <div>
        <label for=\"teamFilter\">Filter by Team</label>
        <select id=\"teamFilter\">
          <option value=\"ALL\">All Teams</option>
          {team_options}
        </select>
      </div>
      <div>
        <label for=\"stateFilter\">State</label>
        <select id=\"stateFilter\">
          <option value=\"ALL\">All States</option>
          <option value=\"open\">Open</option>
          <option value=\"draft\">Draft</option>
          <option value=\"merged\">Merged</option>
          <option value=\"closed\">Closed</option>
        </select>
      </div>
      <div>
        <label for=\"changesFilter\">Changes Requested By</label>
        <input id=\"changesFilter\" type=\"text\" placeholder=\"e.g. user-login\" />
      </div>
      <div>
        <label for=\"daysFilter\">Time Range</label>
        <select id=\"daysFilter\">
          <option value=\"7\">Last 7 days</option>
          <option value=\"30\" selected>Last 30 days</option>
          <option value=\"60\">Last 60 days</option>
          <option value=\"3650\">All fetched</option>
        </select>
      </div>
    </section>

    {''.join(sections)}

    <div class=\"footer\">Approval legend: Team ✓, Tech Lead ✓, CES ✓, VS ✓</div>
  </div>

<script>
(function () {{
  const refreshBtn = document.getElementById('refreshBtn');
  const teamFilter = document.getElementById('teamFilter');
  const stateFilter = document.getElementById('stateFilter');
  const changesFilter = document.getElementById('changesFilter');
  const daysFilter = document.getElementById('daysFilter');

  const sections = Array.from(document.querySelectorAll('.team-section'));

  function parseDateOnly(str) {{
    if (!str) return null;
    const d = new Date(str + 'T00:00:00');
    return Number.isNaN(d.getTime()) ? null : d;
  }}

  function isWithinRange(created, days) {{
    if (!created) return false;
    const createdDate = parseDateOnly(created);
    if (!createdDate) return false;

    const now = new Date();
    const from = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    from.setDate(from.getDate() - days);
    return createdDate >= from;
  }}

  function applyFilters() {{
    const selectedTeam = teamFilter.value;
    const selectedState = stateFilter.value;
    const changesQuery = (changesFilter.value || '').trim().toLowerCase();
    const days = Number(daysFilter.value || '30');

    sections.forEach((section) => {{
      const teamName = section.getAttribute('data-team');
      const showSection = selectedTeam === 'ALL' || teamName === selectedTeam;
      section.style.display = showSection ? '' : 'none';
      if (!showSection) return;

      const rows = Array.from(section.querySelectorAll('tbody tr'));
      let visibleCount = 0;
      rows.forEach((row) => {{
        const state = row.getAttribute('data-state') || '';
        const created = row.getAttribute('data-created') || '';
        const changes = row.getAttribute('data-changes') || '';

        if (row.children.length === 1) {{
          row.style.display = 'none';
          return;
        }}

        const stateOk = selectedState === 'ALL' || state === selectedState;
        const daysOk = isWithinRange(created, days);
        const changesOk = !changesQuery || changes.includes(changesQuery);
        const showRow = stateOk && daysOk && changesOk;

        row.style.display = showRow ? '' : 'none';
        if (showRow) visibleCount += 1;
      }});

      let empty = section.querySelector('tbody tr.__empty');
      if (!empty) {{
        empty = document.createElement('tr');
        empty.className = '__empty';
        empty.innerHTML = '<td colspan="11">No PRs match the current filters.</td>';
        section.querySelector('tbody').appendChild(empty);
      }}
      empty.style.display = visibleCount === 0 ? '' : 'none';
    }});
  }}

  [teamFilter, stateFilter, changesFilter, daysFilter].forEach((el) => {{
    el.addEventListener('input', applyFilters);
    el.addEventListener('change', applyFilters);
  }});

  refreshBtn.addEventListener('click', () => {{
    refreshBtn.disabled = true;
    refreshBtn.textContent = 'Refreshing...';
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
  days = int(os.getenv("DASHBOARD_DAYS", str(DEFAULT_DAYS)))
  output_path = os.getenv("DASHBOARD_OUTPUT", DEFAULT_OUTPUT_PATH)

  now_utc = dt.datetime.now(dt.UTC)
  since_date = (now_utc - dt.timedelta(days=days)).strftime("%Y-%m-%d")
  generated_at = now_utc.strftime("%Y-%m-%d %H:%M:%S UTC")

  client = GitHubClient(token=token)

  team_to_rows: Dict[str, List[PullRow]] = {}
  for team in TEAMS:
    print(f"Fetching team: {team}")
    team_to_rows[team] = fetch_team_rows(client, team, since_date)

  html_text = render_dashboard(team_to_rows, generated_at, days)

  output_dir = os.path.dirname(output_path)
  if output_dir:
    os.makedirs(output_dir, exist_ok=True)

  with open(output_path, "w", encoding="utf-8") as f:
    f.write(html_text)

  if output_path != LEGACY_OUTPUT_PATH:
    with open(LEGACY_OUTPUT_PATH, "w", encoding="utf-8") as f:
      f.write(html_text)

  if output_path.startswith("docs/") or output_path == "docs/index.html":
    os.makedirs("docs", exist_ok=True)
    with open("docs/.nojekyll", "w", encoding="utf-8") as f:
      f.write("\n")

  total = sum(len(v) for v in team_to_rows.values())
  print(f"Dashboard generated: {output_path}")
  if output_path != LEGACY_OUTPUT_PATH:
    print(f"Local copy generated: {LEGACY_OUTPUT_PATH}")
  print(f"Teams: {len(TEAMS)} | PR rows: {total} | Lookback: {days} days")
  return 0


if __name__ == "__main__":
    raise SystemExit(main())
