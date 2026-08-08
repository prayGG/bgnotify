"""Deploy-Announcements: neuer HEAD → Embed in den Updates-Channel.

Git-Helfer + das Deploy-Embed leben zusammen hier, weil der Embed-Fallback
(HEAD-Subject bei unerreichbarem Vorgänger-SHA) selbst Git braucht.
"""
from __future__ import annotations

import logging
import subprocess
from datetime import datetime, timezone

from . import notify
from .config import ROOT
from .embeds import COLOR_BLURPLE

log = logging.getLogger(__name__)


def _git(*args: str) -> str:
    """Run `git <args>` in the repo root. Empty string on any failure."""
    try:
        out = subprocess.check_output(
            ["git", *args], cwd=str(ROOT), text=True, stderr=subprocess.DEVNULL,
        )
        return out.strip()
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return ""


def _sha_reachable(sha: str) -> bool:
    """True if `sha` exists in local git history. Stale SHAs (rebase/force-push)
    return False so the caller can fall back to a HEAD-only deploy embed instead
    of silently dropping the announcement."""
    if not sha:
        return False
    try:
        subprocess.check_output(
            ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
            cwd=str(ROOT), stderr=subprocess.DEVNULL,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return False


def _commits_since(prev_sha: str) -> list[tuple[str, str]]:
    """Return [(short_sha, subject), ...] for commits in prev_sha..HEAD.

    Excludes the bot's own `update state` commits so the deploy feed only
    shows meaningful code/config changes.
    """
    if not prev_sha:
        return []
    raw = _git("log", f"{prev_sha}..HEAD", "--pretty=format:%h|%s")
    if not raw:
        return []
    out: list[tuple[str, str]] = []
    for line in raw.splitlines():
        if "|" not in line:
            continue
        sha, subject = line.split("|", 1)
        if subject.strip().lower().startswith("update state"):
            continue
        out.append((sha, subject))
    return out


def _head_commit_subject(head_sha: str) -> str:
    raw = _git("log", "-1", "--pretty=format:%s", head_sha)
    return raw.strip()


def build_updates_embed(commits: list[tuple[str, str]], head_sha: str) -> dict:
    if commits:
        lines = [f"`{sha}`⠀{subject}" for sha, subject in commits[:15]]
        if len(commits) > 15:
            lines.append(f"_…und {len(commits) - 15} weitere_")
        description = "\n".join(lines)
    else:
        # No reachable commits in prev..HEAD (rebase/force-push or first
        # post-feature run). Show at least the head subject so the channel
        # still surfaces that something shipped.
        subject = _head_commit_subject(head_sha)
        if subject:
            description = f"`{head_sha[:7]}`⠀{subject}"
        else:
            description = f"`{head_sha[:7]}`"
    return {
        "author": {"name": "✦⠀⠀deploy⠀⠀✦"},
        "title": f"Update · `{head_sha[:7]}`",
        "description": description,
        "color": COLOR_BLURPLE,
        "footer": {"text": "bgnotify"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def announce_deploy(state: dict, updates: "notify.UpdateTarget") -> None:
    """Post a deploy embed to the updates channel when HEAD has moved.

    Tracks the last-announced commit in `state["last_deploy_sha"]`. On first
    run after the feature lands, we record HEAD without posting (no history
    to diff against — would otherwise spam every old commit at once).

    `updates` sagt, WOHIN — in der Regel der Channel, in dem zuletzt ein Command
    lief; siehe `notify.UpdateTarget`.
    """
    head_sha = _git("rev-parse", "HEAD")
    if not head_sha:
        return
    last_sha = state.get("last_deploy_sha", "")
    if head_sha == last_sha:
        return

    if last_sha and updates:
        reachable = _sha_reachable(last_sha)
        commits = _commits_since(last_sha) if reachable else []
        # When the previous SHA *is* reachable but the only commits in the
        # range are the bot's own "update state" entries (which _commits_since
        # filters out), this isn't a real deploy — advance last_sha silently
        # so we don't keep firing on every bot tick. The HEAD-only fallback
        # is only correct when the SHA is unreachable (rebase/force-push).
        if reachable and not commits:
            state["last_deploy_sha"] = head_sha
            return
        embed = build_updates_embed(commits, head_sha)
        if not updates.send(embed):
            log.warning("deploy announcement failed — will retry next run")
            return  # keep last_sha so we re-try on the next run

    state["last_deploy_sha"] = head_sha
