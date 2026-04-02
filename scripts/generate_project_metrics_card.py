#!/usr/bin/env python3
"""
Generate the profile metrics SVG card and a JSON data snapshot.

Data sources:
- GitHub REST API (repos)
- GitHub GraphQL API (30-day commit stats, token path)
- cloc (fair LOC by repository)
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


GITHUB_API = "https://api.github.com"
USER_AGENT = "project-metrics-card-generator"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate project metrics card SVG.")
    parser.add_argument("--username", default="Jacobinwwey", help="GitHub username")
    parser.add_argument("--days", type=int, default=30, help="Activity window in days")
    parser.add_argument(
        "--exclude-update-repo",
        action="append",
        default=[],
        help="Repository name to exclude from activity stats (repeatable)",
    )
    parser.add_argument(
        "--output-svg",
        default="assets/project-metrics-top-repos-card.svg",
        help="Output SVG path",
    )
    parser.add_argument(
        "--output-json",
        default="assets/project-metrics-data.json",
        help="Output JSON path",
    )
    parser.add_argument(
        "--max-loc-repos",
        type=int,
        default=20,
        help="Max repositories to run cloc against",
    )
    return parser.parse_args()


class GitHubClient:
    def __init__(self, token: str | None) -> None:
        self.token = token

    def get(self, path_or_url: str, params: dict[str, Any] | None = None) -> Any:
        if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
            url = path_or_url
        else:
            url = f"{GITHUB_API}{path_or_url}"

        if params:
            query = urllib.parse.urlencode(params)
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{query}"

        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        req = urllib.request.Request(url, headers=headers)

        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                payload = resp.read().decode("utf-8")
                return json.loads(payload)
        except urllib.error.HTTPError as err:
            body = err.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"GitHub API HTTP {err.code} for {url}: {body}") from err

    def post_graphql(self, query: str, variables: dict[str, Any]) -> Any:
        if not self.token:
            raise RuntimeError("GraphQL request requires GITHUB_TOKEN.")
        url = f"{GITHUB_API}/graphql"
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            text = err.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"GitHub GraphQL HTTP {err.code}: {text}") from err
        if body.get("errors"):
            raise RuntimeError(f"GitHub GraphQL errors: {body['errors']}")
        return body.get("data") or {}


def fetch_repositories(client: GitHubClient, username: str) -> list[dict[str, Any]]:
    repos: list[dict[str, Any]] = []
    page = 1
    while True:
        batch = client.get(
            f"/users/{username}/repos",
            {
                "type": "owner",
                "per_page": 100,
                "page": page,
                "sort": "updated",
                "direction": "desc",
            },
        )
        if not batch:
            break
        repos.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return repos


def iso_utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def to_iso_z(ts: dt.datetime) -> str:
    return ts.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso_z(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(dt.timezone.utc)


def fetch_repo_activity(
    client: GitHubClient,
    owner: str,
    repo: str,
    default_branch: str,
    since_iso: str,
) -> tuple[int, int]:
    if client.token:
        return fetch_repo_activity_graphql(client, owner, repo, since_iso)
    return fetch_repo_activity_rest(client, owner, repo, default_branch, since_iso)


def fetch_repo_activity_graphql(
    client: GitHubClient,
    owner: str,
    repo: str,
    since_iso: str,
) -> tuple[int, int]:
    query = """
query RepoActivity($owner: String!, $name: String!, $since: GitTimestamp!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    defaultBranchRef {
      target {
        ... on Commit {
          history(first: 100, since: $since, after: $cursor) {
            nodes {
              additions
              deletions
            }
            pageInfo {
              hasNextPage
              endCursor
            }
          }
        }
      }
    }
  }
}
"""

    total_commits = 0
    changed_lines = 0
    cursor: str | None = None
    while True:
        try:
            data = client.post_graphql(
                query,
                {
                    "owner": owner,
                    "name": repo,
                    "since": since_iso,
                    "cursor": cursor,
                },
            )
        except RuntimeError as err:
            print(f"[warn] graphql activity failed for {repo}: {err}")
            return total_commits, changed_lines

        repository = (data or {}).get("repository") or {}
        default_branch_ref = repository.get("defaultBranchRef") or {}
        target = default_branch_ref.get("target") or {}
        history = target.get("history") or {}
        nodes = history.get("nodes") or []
        if not nodes:
            return total_commits, changed_lines

        total_commits += len(nodes)
        for node in nodes:
            changed_lines += int(node.get("additions") or 0) + int(node.get("deletions") or 0)

        page_info = history.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")

    return total_commits, changed_lines


def fetch_repo_activity_rest(
    client: GitHubClient,
    owner: str,
    repo: str,
    default_branch: str,
    since_iso: str,
) -> tuple[int, int]:
    total_commits = 0
    changed_lines = 0
    page = 1

    while True:
        try:
            commits = client.get(
                f"/repos/{owner}/{repo}/commits",
                {"sha": default_branch, "since": since_iso, "per_page": 100, "page": page},
            )
        except RuntimeError as err:
            # Empty repo or branch mismatch can return 409.
            if "HTTP 409" in str(err):
                return 0, 0
            print(f"[warn] commit list failed for {repo}: {err}")
            return total_commits, changed_lines

        if not commits:
            break

        total_commits += len(commits)
        for commit in commits:
            detail_url = commit.get("url")
            if not detail_url:
                continue
            try:
                detail = client.get(detail_url)
            except RuntimeError as err:
                print(f"[warn] commit detail failed for {repo}: {err}")
                continue
            stats = detail.get("stats") or {}
            additions = int(stats.get("additions", 0))
            deletions = int(stats.get("deletions", 0))
            changed_lines += additions + deletions

        if len(commits) < 100:
            break
        page += 1

    return total_commits, changed_lines


def run_cloc(repo_dir: Path) -> int:
    cmd = [
        "cloc",
        "--json",
        "--quiet",
        "--vcs=git",
        "--exclude-dir",
        "node_modules,dist,build,.next,target,vendor,coverage,.venv,venv,__pycache__,.mypy_cache",
        ".",
    ]
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(repo_dir),
        timeout=600,
        check=False,
    )
    if proc.returncode != 0:
        print(f"[warn] cloc failed for {repo_dir.name}: {proc.stderr.strip()}")
        return 0
    out = proc.stdout
    start = out.find("{")
    if start == -1:
        return 0
    try:
        parsed = json.loads(out[start:])
    except json.JSONDecodeError:
        return 0
    summary = parsed.get("SUM") or {}
    if not summary:
        print(f"[warn] cloc returned empty summary for {repo_dir.name}")
        return 0
    return int(summary.get("code", 0))


def fetch_repo_loc(
    clone_url: str,
    default_branch: str,
    repo_name: str,
    workspace: Path,
) -> int:
    repo_dir = workspace / repo_name
    if repo_dir.exists():
        shutil.rmtree(repo_dir, ignore_errors=True)
    clone_cmd = [
        "git",
        "clone",
        "--depth",
        "1",
        "--single-branch",
        "--branch",
        default_branch,
        clone_url,
        str(repo_dir),
    ]
    proc = subprocess.run(
        clone_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=900,
        check=False,
    )
    if proc.returncode != 0:
        print(f"[warn] clone failed for {repo_name}: {proc.stderr.strip()}")
        return 0
    return run_cloc(repo_dir)


def short_name(name: str, max_len: int = 26) -> str:
    if len(name) <= max_len:
        return name
    return name[: max_len - 1] + "…"


def fmt_num(value: int) -> str:
    return f"{value:,}"


def scale_width(value: int, max_value: int, width: int) -> int:
    if value <= 0 or max_value <= 0:
        return 0
    return max(2, int(round(value / max_value * width)))


def pad_rows(rows: list[dict[str, Any]], size: int) -> list[dict[str, Any]]:
    out = list(rows)
    while len(out) < size:
        out.append({"name": "—", "value": 0, "commits": 0, "changed_lines": 0})
    return out[:size]


def generate_svg(
    left_rows: list[dict[str, Any]],
    right_rows: list[dict[str, Any]],
) -> str:
    left_rows = pad_rows(left_rows, 4)
    right_rows = pad_rows(right_rows, 4)

    left_max = max((int(r.get("value", 0)) for r in left_rows), default=0)
    right_changed_max = max((int(r.get("changed_lines", 0)) for r in right_rows), default=0)
    right_commits_max = max((int(r.get("commits", 0)) for r in right_rows), default=0)
    row_centers = [54, 118, 182, 246]

    lines: list[str] = []
    lines.append('<svg width="1200" height="420" viewBox="0 0 1200 420" xmlns="http://www.w3.org/2000/svg" role="img" aria-labelledby="title desc">')
    lines.append("  <title id=\"title\">Repository Charts</title>")
    lines.append("  <desc id=\"desc\">Left chart: total code lines by repository. Right chart: 30-day change lines and commits by repository.</desc>")
    lines.append("  <defs>")
    lines.append("    <linearGradient id=\"bg2\" x1=\"0\" y1=\"0\" x2=\"1\" y2=\"1\">")
    lines.append("      <stop offset=\"0%\" stop-color=\"#f8fafc\" />")
    lines.append("      <stop offset=\"100%\" stop-color=\"#eef2ff\" />")
    lines.append("    </linearGradient>")
    lines.append("    <linearGradient id=\"barCode\" x1=\"0\" y1=\"0\" x2=\"1\" y2=\"0\">")
    lines.append("      <stop offset=\"0%\" stop-color=\"#2563eb\" />")
    lines.append("      <stop offset=\"100%\" stop-color=\"#60a5fa\" />")
    lines.append("    </linearGradient>")
    lines.append("    <linearGradient id=\"barChange\" x1=\"0\" y1=\"0\" x2=\"1\" y2=\"0\">")
    lines.append("      <stop offset=\"0%\" stop-color=\"#1d4ed8\" />")
    lines.append("      <stop offset=\"100%\" stop-color=\"#3b82f6\" />")
    lines.append("    </linearGradient>")
    lines.append("    <linearGradient id=\"barCommit\" x1=\"0\" y1=\"0\" x2=\"1\" y2=\"0\">")
    lines.append("      <stop offset=\"0%\" stop-color=\"#059669\" />")
    lines.append("      <stop offset=\"100%\" stop-color=\"#34d399\" />")
    lines.append("    </linearGradient>")
    lines.append("  </defs>")
    lines.append("")
    lines.append("  <rect x=\"8\" y=\"8\" width=\"1184\" height=\"404\" rx=\"20\" fill=\"url(#bg2)\" stroke=\"#c7d8f8\" stroke-width=\"2\"/>")
    lines.append("  <rect x=\"20\" y=\"20\" width=\"560\" height=\"380\" rx=\"16\" fill=\"#ffffff\" stroke=\"#dbe7fb\"/>")
    lines.append("  <rect x=\"620\" y=\"20\" width=\"560\" height=\"380\" rx=\"16\" fill=\"#ffffff\" stroke=\"#dbe7fb\"/>")
    lines.append("")

    # Left: fair LOC top 4
    for idx, row in enumerate(left_rows):
        c = row_centers[idx]
        name = html.escape(short_name(str(row.get("name", "—")), 28))
        value = int(row.get("value", 0))
        w = scale_width(value, left_max, 320)
        lines.append(f'  <text x="36" y="{c + 6}" fill="#334155" font-family="Segoe UI, Arial, sans-serif" font-size="16" font-weight="600">{name}</text>')
        lines.append(f'  <rect x="240" y="{c - 8}" width="320" height="16" rx="8" fill="#dbeafe"/>')
        lines.append(f'  <rect x="240" y="{c - 8}" width="{w}" height="16" rx="8" fill="url(#barCode)"/>')
        lines.append(f'  <text x="560" y="{c + 4}" text-anchor="end" fill="#0f172a" font-family="Segoe UI, Arial, sans-serif" font-size="13" font-weight="700">{fmt_num(value)}</text>')
        lines.append("")

    lines.append('  <text x="240" y="328" fill="#64748b" font-family="Segoe UI, Arial, sans-serif" font-size="12">fair LOC</text>')
    lines.append('  <circle cx="230" cy="324" r="5" fill="url(#barCode)"/>')
    lines.append("")

    # Right: 30d changed lines + commits top 4
    for idx, row in enumerate(right_rows):
        c = row_centers[idx]
        name = html.escape(short_name(str(row.get("name", "—")), 26))
        changed = int(row.get("changed_lines", 0))
        commits = int(row.get("commits", 0))
        changed_w = scale_width(changed, right_changed_max, 320)
        commits_w = scale_width(commits, right_commits_max, 320)

        lines.append(f'  <text x="636" y="{c + 6}" fill="#334155" font-family="Segoe UI, Arial, sans-serif" font-size="16" font-weight="600">{name}</text>')
        lines.append(f'  <rect x="840" y="{c - 10}" width="320" height="8" rx="4" fill="#dbeafe"/>')
        lines.append(f'  <rect x="840" y="{c - 10}" width="{changed_w}" height="8" rx="4" fill="url(#barChange)"/>')
        lines.append(f'  <rect x="840" y="{c + 2}" width="320" height="8" rx="4" fill="#d1fae5"/>')
        lines.append(f'  <rect x="840" y="{c + 2}" width="{commits_w}" height="8" rx="4" fill="url(#barCommit)"/>')
        lines.append(f'  <text x="1160" y="{c - 2}" text-anchor="end" fill="#1d4ed8" font-family="Segoe UI, Arial, sans-serif" font-size="12" font-weight="700">{fmt_num(changed)}</text>')
        lines.append(f'  <text x="1160" y="{c + 10}" text-anchor="end" fill="#059669" font-family="Segoe UI, Arial, sans-serif" font-size="12" font-weight="700">{fmt_num(commits)}</text>')
        lines.append("")

    lines.append('  <circle cx="840" cy="324" r="5" fill="url(#barChange)"/>')
    lines.append('  <text x="852" y="328" fill="#64748b" font-family="Segoe UI, Arial, sans-serif" font-size="12">changed lines (30d)</text>')
    lines.append('  <circle cx="840" cy="344" r="5" fill="url(#barCommit)"/>')
    lines.append('  <text x="852" y="348" fill="#64748b" font-family="Segoe UI, Arial, sans-serif" font-size="12">commits (30d)</text>')
    lines.append("</svg>")
    lines.append("")

    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    token = os.getenv("GITHUB_TOKEN")
    client = GitHubClient(token=token)

    now = iso_utc_now()
    since = now - dt.timedelta(days=args.days)
    since_iso = to_iso_z(since)
    excluded = set(args.exclude_update_repo)
    excluded.add(args.username)

    repos = fetch_repositories(client, args.username)
    non_fork_repos = [r for r in repos if not r.get("fork", False)]

    # 30-day activity stats
    activity_rows: list[dict[str, Any]] = []
    total_commits = 0
    total_changed = 0
    updated_repo_count = 0

    for repo in non_fork_repos:
        name = str(repo.get("name"))
        if name in excluded:
            continue

        pushed_at_raw = repo.get("pushed_at")
        if pushed_at_raw:
            try:
                pushed_at = parse_iso_z(str(pushed_at_raw))
                if pushed_at < since:
                    continue
            except Exception:
                pass

        default_branch = str(repo.get("default_branch") or "main")
        commits, changed = fetch_repo_activity(client, args.username, name, default_branch, since_iso)
        if commits <= 0 and changed <= 0:
            continue

        activity_rows.append(
            {
                "name": name,
                "commits": commits,
                "changed_lines": changed,
            }
        )
        total_commits += commits
        total_changed += changed
        updated_repo_count += 1

    activity_rows.sort(key=lambda x: (int(x["changed_lines"]), int(x["commits"])), reverse=True)
    right_top = activity_rows[:4]

    # LOC stats with cloc
    if shutil.which("cloc") is None:
        raise RuntimeError("cloc is required but not found in PATH.")

    loc_candidates = sorted(non_fork_repos, key=lambda r: int(r.get("size") or 0), reverse=True)
    loc_candidates = loc_candidates[: max(1, args.max_loc_repos)]

    loc_rows: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="profile-metrics-") as tmp:
        workspace = Path(tmp)
        for repo in loc_candidates:
            name = str(repo.get("name"))
            default_branch = str(repo.get("default_branch") or "main")
            clone_url = str(repo.get("clone_url"))
            code_loc = fetch_repo_loc(clone_url, default_branch, name, workspace)
            loc_rows.append({"name": name, "value": code_loc})

    loc_rows.sort(key=lambda x: int(x["value"]), reverse=True)
    left_top = loc_rows[:4]

    svg = generate_svg(left_top, right_top)

    output_svg = Path(args.output_svg)
    output_json = Path(args.output_json)
    output_svg.parent.mkdir(parents=True, exist_ok=True)
    output_json.parent.mkdir(parents=True, exist_ok=True)

    output_svg.write_text(svg, encoding="utf-8")

    payload = {
        "username": args.username,
        "generated_at_utc": to_iso_z(now),
        "window_days": args.days,
        "exclude_update_repos": sorted(excluded),
        "totals_30d": {
            "updated_repos": updated_repo_count,
            "commits": total_commits,
            "changed_lines": total_changed,
        },
        "top_loc_repos": left_top,
        "top_activity_repos": right_top,
    }
    output_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"[ok] wrote {output_svg}")
    print(f"[ok] wrote {output_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
