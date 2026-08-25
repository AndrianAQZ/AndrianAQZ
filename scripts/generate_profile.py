#!/usr/bin/env python3
"""Generate a privacy-first GitHub profile README from public GitHub data."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"


class ProfileError(RuntimeError):
    """Raised when the profile cannot be generated safely."""


@dataclass(frozen=True)
class ActivityItem:
    line: str
    created_at: datetime


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ProfileError(f"Missing required file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ProfileError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ProfileError(f"Expected a JSON object in {path}")
    return value


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def format_date(value: datetime | None) -> str:
    if value is None:
        return "Unknown date"
    return f"{value.day} {value.strftime('%b %Y')}"


def normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def safe_text(value: Any) -> str:
    return html.escape(normalize_text(value), quote=False)


def api_get(url: str, token: str | None) -> Any:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": "AndrianAQZ-profile-generator",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=30) as response:
            return json.load(response)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise ProfileError(f"GitHub API returned HTTP {exc.code}: {detail}") from exc
    except (URLError, TimeoutError) as exc:
        raise ProfileError(f"Unable to reach the GitHub API: {exc}") from exc


def fetch_public_data(username: str, token: str | None) -> dict[str, Any]:
    repos: list[dict[str, Any]] = []
    for page in range(1, 11):
        batch = api_get(
            f"{API_ROOT}/users/{username}/repos"
            f"?type=owner&sort=pushed&direction=desc&per_page=100&page={page}",
            token,
        )
        if not isinstance(batch, list):
            raise ProfileError("Unexpected repositories response from GitHub")
        repos.extend(batch)
        if len(batch) < 100:
            break

    events = api_get(
        f"{API_ROOT}/users/{username}/events/public?per_page=100",
        token,
    )
    if not isinstance(events, list):
        raise ProfileError("Unexpected public-events response from GitHub")

    # The public endpoint should already enforce this. Keep a second local guard.
    repos = [repo for repo in repos if not bool(repo.get("private", False))]
    return {"repos": repos, "events": events}


def replace_section(template: str, name: str, body: str) -> str:
    start = f"<!-- AUTO:{name}:START -->"
    end = f"<!-- AUTO:{name}:END -->"
    start_index = template.find(start)
    end_index = template.find(end)
    if start_index < 0 or end_index < 0 or end_index < start_index:
        raise ProfileError(f"Template markers are missing or invalid for {name}")
    content_start = start_index + len(start)
    return template[:content_start] + "\n" + body.rstrip() + "\n" + template[end_index:]


def render_intro(config: dict[str, Any]) -> str:
    display_name = safe_text(config.get("display_name", config.get("username", "")))
    headline = safe_text(config.get("headline", ""))
    tags = [f"`{safe_text(tag)}`" for tag in config.get("identity_tags", []) if normalize_text(tag)]
    lines = [f"# Hi, I'm {display_name} 👋", ""]
    if headline:
        lines.append(f"**{headline}**")
    if tags:
        lines.extend(["", " · ".join(tags)])
    return "\n".join(lines)


def render_about(config: dict[str, Any]) -> str:
    paragraphs = [safe_text(item) for item in config.get("about", []) if normalize_text(item)]
    if not paragraphs:
        return "I build and learn in public."
    return "\n\n".join(paragraphs)


def render_toolbox(config: dict[str, Any]) -> str:
    tools = [f"`{safe_text(tool)}`" for tool in config.get("toolbox", []) if normalize_text(tool)]
    return " · ".join(tools) if tools else "Toolbox coming soon."


def repo_is_featureable(repo: dict[str, Any]) -> bool:
    return (
        not bool(repo.get("private", False))
        and not bool(repo.get("fork", False))
        and not bool(repo.get("archived", False))
    )


def render_featured(
    config: dict[str, Any], repos: list[dict[str, Any]]
) -> tuple[str, list[datetime]]:
    repos_by_name = {str(repo.get("name", "")): repo for repo in repos}
    excluded = {str(name).lower() for name in config.get("exclude_repositories", [])}
    blocks: list[str] = []
    timestamps: list[datetime] = []

    for entry in config.get("featured_repositories", []):
        if not isinstance(entry, dict):
            raise ProfileError("Each featured repository entry must be an object")
        name = normalize_text(entry.get("name"))
        if not name:
            raise ProfileError("A featured repository is missing its name")
        if name.lower() in excluded:
            raise ProfileError(f"Featured repository {name!r} is also excluded")

        repo = repos_by_name.get(name)
        if repo is None:
            raise ProfileError(
                f"Featured repository {name!r} was not returned by the public GitHub API"
            )
        if not repo_is_featureable(repo):
            raise ProfileError(
                f"Featured repository {name!r} is private, archived, or a fork"
            )

        url = str(repo.get("html_url") or f"https://github.com/{config['username']}/{name}")
        summary = safe_text(entry.get("summary") or repo.get("description") or "Public project")
        language = safe_text(repo.get("language") or "Mixed")
        pushed_at = parse_timestamp(repo.get("pushed_at") or repo.get("updated_at"))
        if pushed_at:
            timestamps.append(pushed_at)

        highlights = [
            f"`{safe_text(item)}`"
            for item in entry.get("highlights", [])
            if normalize_text(item)
        ]
        metadata = highlights or [f"`{language}`"]
        if language and all(language not in item for item in metadata):
            metadata.insert(0, f"`{language}`")
        if pushed_at:
            metadata.append(f"Last public update: **{format_date(pushed_at)}**")

        blocks.append(
            "\n".join(
                [
                    f"### [{safe_text(name)}]({url})",
                    "",
                    summary,
                    "",
                    " · ".join(metadata),
                ]
            )
        )

    if not blocks:
        return "No featured public projects are configured yet.", timestamps
    return "\n\n".join(blocks), timestamps


def activity_repo_is_excluded(full_name: str, config: dict[str, Any]) -> bool:
    excluded = {str(name).lower() for name in config.get("activity_exclude_repositories", [])}
    short_name = full_name.split("/", 1)[-1]
    return full_name.lower() in excluded or short_name.lower() in excluded


def event_link(event: dict[str, Any], full_name: str) -> str:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    event_type = event.get("type")
    if event_type == "PullRequestEvent":
        pr = payload.get("pull_request") if isinstance(payload.get("pull_request"), dict) else {}
        return str(pr.get("html_url") or f"https://github.com/{full_name}")
    if event_type == "IssuesEvent":
        issue = payload.get("issue") if isinstance(payload.get("issue"), dict) else {}
        return str(issue.get("html_url") or f"https://github.com/{full_name}")
    if event_type == "ReleaseEvent":
        release = payload.get("release") if isinstance(payload.get("release"), dict) else {}
        return str(release.get("html_url") or f"https://github.com/{full_name}/releases")
    if event_type == "PushEvent":
        commits = payload.get("commits") if isinstance(payload.get("commits"), list) else []
        if commits and isinstance(commits[-1], dict) and commits[-1].get("sha"):
            return f"https://github.com/{full_name}/commit/{commits[-1]['sha']}"
    return f"https://github.com/{full_name}"


def event_description(event: dict[str, Any], config: dict[str, Any]) -> str | None:
    repo_info = event.get("repo") if isinstance(event.get("repo"), dict) else {}
    full_name = normalize_text(repo_info.get("name"))
    if not full_name or activity_repo_is_excluded(full_name, config):
        return None

    event_type = normalize_text(event.get("type"))
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    link = event_link(event, full_name)
    repo_markdown = f"[{safe_text(full_name)}]({link})"

    if event_type == "PushEvent":
        size = payload.get("size")
        commits = payload.get("commits") if isinstance(payload.get("commits"), list) else []
        count = int(size) if isinstance(size, int) else len(commits)
        branch = normalize_text(payload.get("ref")).split("/")[-1]
        commit_word = "commit" if count == 1 else "commits"
        branch_text = f" on `{safe_text(branch)}`" if branch else ""
        return f"Pushed {count} {commit_word} to {repo_markdown}{branch_text}."

    if event_type == "PullRequestEvent":
        action = safe_text(payload.get("action") or "updated")
        number = payload.get("number")
        number_text = f" #{number}" if isinstance(number, int) else ""
        return f"{action.capitalize()} pull request{number_text} in {repo_markdown}."

    if event_type == "IssuesEvent":
        action = safe_text(payload.get("action") or "updated")
        issue = payload.get("issue") if isinstance(payload.get("issue"), dict) else {}
        number = issue.get("number")
        number_text = f" #{number}" if isinstance(number, int) else ""
        return f"{action.capitalize()} issue{number_text} in {repo_markdown}."

    if event_type == "ReleaseEvent":
        action = safe_text(payload.get("action") or "published")
        return f"{action.capitalize()} a release in {repo_markdown}."

    if event_type == "CreateEvent":
        ref_type = safe_text(payload.get("ref_type") or "repository item")
        ref = safe_text(payload.get("ref") or "")
        ref_text = f" `{ref}`" if ref else ""
        return f"Created {ref_type}{ref_text} in {repo_markdown}."

    return None


def build_repository_fallback_items(
    repos: list[dict[str, Any]], config: dict[str, Any], limit: int
) -> list[ActivityItem]:
    items: list[ActivityItem] = []
    ordered = sorted(
        repos,
        key=lambda repo: parse_timestamp(repo.get("pushed_at") or repo.get("updated_at"))
        or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    for repo in ordered:
        name = normalize_text(repo.get("name"))
        owner = repo.get("owner") if isinstance(repo.get("owner"), dict) else {}
        owner_login = normalize_text(owner.get("login")) or normalize_text(config.get("username"))
        full_name = normalize_text(repo.get("full_name")) or f"{owner_login}/{name}"
        if (
            not name
            or bool(repo.get("private", False))
            or bool(repo.get("archived", False))
            or activity_repo_is_excluded(full_name, config)
        ):
            continue
        updated_at = parse_timestamp(repo.get("pushed_at") or repo.get("updated_at"))
        if updated_at is None:
            continue
        url = str(repo.get("html_url") or f"https://github.com/{full_name}")
        label = "Updated public fork" if bool(repo.get("fork", False)) else "Updated public project"
        line = f"- **{format_date(updated_at)}** — {label} [{safe_text(full_name)}]({url})."
        items.append(ActivityItem(line, updated_at))
        if len(items) >= limit:
            break
    return items


def build_activity_items(
    events: list[dict[str, Any]],
    repos: list[dict[str, Any]],
    config: dict[str, Any],
) -> list[ActivityItem]:
    items: list[ActivityItem] = []
    seen: set[str] = set()
    limit = max(0, int(config.get("max_activity_items", 5)))

    ordered = sorted(
        events,
        key=lambda event: parse_timestamp(event.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    for event in ordered:
        description = event_description(event, config)
        created_at = parse_timestamp(event.get("created_at"))
        if not description or created_at is None:
            continue
        dedupe_key = f"{created_at.date().isoformat()}::{description}"
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        items.append(ActivityItem(f"- **{format_date(created_at)}** — {description}", created_at))
        if len(items) >= limit:
            break

    # Public event feeds are sparse and may contain only stars or excluded imports.
    # Fall back to transparent repository update timestamps instead of showing an
    # empty activity section. Forks are labelled explicitly and never featured.
    if not items and limit:
        return build_repository_fallback_items(repos, config, limit)
    return items


def render_activity(items: list[ActivityItem]) -> str:
    if not items:
        return "No recent public GitHub activity was available when this profile was generated."
    return "\n".join(item.line for item in items)


def render_status(timestamps: Iterable[datetime]) -> str:
    values = list(timestamps)
    checked_month = datetime.now(timezone.utc).strftime("%b %Y")
    if not values:
        return (
            f"Automation checked: {checked_month} UTC. "
            "The profile refreshes from public GitHub data and uses a monthly heartbeat."
        )
    latest = max(values)
    return (
        f"Latest included public activity: {format_date(latest)} UTC. "
        f"Automation checked: {checked_month} UTC. "
        "A low-noise monthly heartbeat keeps scheduled refreshes active."
    )


def render_profile(
    template: str,
    config: dict[str, Any],
    data: dict[str, Any],
) -> str:
    username = normalize_text(config.get("username"))
    if not username:
        raise ProfileError("profile.config.json must contain a username")

    repos = data.get("repos")
    events = data.get("events")
    if not isinstance(repos, list) or not isinstance(events, list):
        raise ProfileError("Profile data must contain repos and events arrays")

    featured, featured_timestamps = render_featured(config, repos)
    activity_items = build_activity_items(events, repos, config)
    status_timestamps = featured_timestamps + [item.created_at for item in activity_items]

    rendered = template
    rendered = replace_section(rendered, "INTRO", render_intro(config))
    rendered = replace_section(rendered, "ABOUT", render_about(config))
    rendered = replace_section(rendered, "TOOLBOX", render_toolbox(config))
    rendered = replace_section(rendered, "FEATURED", featured)
    rendered = replace_section(rendered, "ACTIVITY", render_activity(activity_items))
    rendered = replace_section(rendered, "STATUS", render_status(status_timestamps))
    return rendered.rstrip() + "\n"


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        handle.write(content)
        temp_path = Path(handle.name)
    temp_path.replace(path)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "profile.config.json")
    parser.add_argument("--template", type=Path, default=ROOT / "README.template.md")
    parser.add_argument("--output", type=Path, default=ROOT / "README.md")
    parser.add_argument(
        "--data-file",
        type=Path,
        help="Use local fixture data instead of calling GitHub (for tests/debugging).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero when README.md is not up to date.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        config = load_json(args.config)
        template = args.template.read_text(encoding="utf-8")
        if args.data_file:
            data = load_json(args.data_file)
        else:
            data = fetch_public_data(
                normalize_text(config.get("username")),
                os.environ.get("GITHUB_TOKEN"),
            )
        rendered = render_profile(template, config, data)

        if args.check:
            current = args.output.read_text(encoding="utf-8") if args.output.exists() else ""
            if current != rendered:
                print(f"{args.output} is not up to date", file=sys.stderr)
                return 1
            print(f"{args.output} is up to date")
            return 0

        current = args.output.read_text(encoding="utf-8") if args.output.exists() else None
        if current == rendered:
            print(f"No profile changes: {args.output}")
            return 0
        atomic_write(args.output, rendered)
        print(f"Generated {args.output}")
        return 0
    except (ProfileError, OSError) as exc:
        print(f"profile generation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
