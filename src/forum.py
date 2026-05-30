"""Scrape new forum posts by a specific user from a XenForo search URL.

Uses Playwright (headless Chromium) — thinksteroids.com sits behind Incapsula
which serves a JS challenge to plain HTTP clients. A real browser solves the
challenge anonymously, so we don't need (and never want) login cookies tied
to a real account.

Polite by design: one navigation per *forum* run, and the caller gates how
often we even attempt it (default 2h via `forum.check_interval_minutes`).

Posts are keyed by post_id (extracted from `.../post-NNN`) — stable across
pagination and sort order. The caller diffs against state to find what's new.
"""
from __future__ import annotations

import json
import logging
import re
import sys
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_POST_ID_RE = re.compile(r"/post-(\d+)")


def _force_date_sort(url: str) -> str:
    """Override `o=relevance` → `o=date` so newest posts are at the top.

    Without this, the saved-search URL keeps relevance order, which makes the
    diff against state ambiguous (old posts can shift back into the top N).
    """
    parts = urlparse(url)
    q = dict(parse_qsl(parts.query, keep_blank_values=True))
    q["o"] = "date"
    return urlunparse(parts._replace(query=urlencode(q)))


def _origin(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _fetch_html(url: str) -> str:
    """Drive headless Chromium past Incapsula and return the result page HTML.

    Import is deferred so the rest of the bot can run when Playwright isn't
    installed locally (the GitHub runner has it; dev machines may not).
    """
    from playwright.sync_api import sync_playwright  # noqa: WPS433

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            ctx = browser.new_context(
                user_agent=UA,
                locale="de-DE",
                viewport={"width": 1280, "height": 800},
            )
            # Strip the obvious `navigator.webdriver = true` tell. Incapsula
            # checks several signals, this isn't a silver bullet — but a real
                # browser context with normal TLS plus this is usually enough.
            ctx.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            page = ctx.new_page()
            # `networkidle` (not `domcontentloaded`) is critical: Incapsula
            # serves an interstitial iframe that JS-redirects to the real
            # result page. domcontentloaded fires on the *challenge* page and
            # we'd return the 848-byte iframe HTML.
            page.goto(url, wait_until="networkidle", timeout=45000)
            try:
                page.wait_for_selector(
                    ".block-row, .contentRow, .blockMessage",
                    timeout=15000,
                )
            except Exception:
                log.warning("forum: no result selector appeared within 15s")
            return page.content()
        finally:
            browser.close()


def _parse(html_text: str, origin: str) -> list[dict]:
    soup = BeautifulSoup(html_text, "html.parser")
    out: list[dict] = []
    seen_ids: set[str] = set()

    for row in soup.select("li.block-row, .block-row, .contentRow"):
        title_a = row.select_one(".contentRow-title a, h3 a")
        if not title_a:
            continue
        href = title_a.get("href") or ""
        m = _POST_ID_RE.search(href)
        if not m:
            continue
        post_id = m.group(1)
        if post_id in seen_ids:
            continue
        seen_ids.add(post_id)
        if href.startswith("/"):
            href = origin + href

        snippet_el = row.select_one(".contentRow-snippet, .contentRow-blurb, .contentRow-lesser")
        time_el = row.select_one("time")
        author_el = row.select_one("a.username, .username")

        out.append({
            "post_id": post_id,
            "thread_title": _norm(title_a.get_text()),
            "excerpt": _norm(snippet_el.get_text()) if snippet_el else "",
            "url": href,
            "posted_at": (time_el.get("datetime") if time_el else "") or "",
            "author": _norm(author_el.get_text()) if author_el else "",
        })
    return out


def fetch_posts(search_url: str, expected_author: str = "") -> list[dict]:
    """Return posts on the search page, newest first.

    `expected_author` is an optional case-insensitive substring filter —
    defensive in case the URL's user filter ever silently breaks (XenForo
    ignores unknown user filters rather than erroring).
    """
    url = _force_date_sort(search_url)
    html_text = _fetch_html(url)
    posts = _parse(html_text, _origin(url))
    if expected_author:
        needle = expected_author.lower()
        posts = [p for p in posts if needle in (p.get("author", "") or "").lower()]
    return posts


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if len(sys.argv) < 2:
        print("usage: python -m src.forum <search_url> [<expected_author>]")
        sys.exit(2)
    expected = sys.argv[2] if len(sys.argv) >= 3 else ""
    print(json.dumps(fetch_posts(sys.argv[1], expected), indent=2, ensure_ascii=False))
