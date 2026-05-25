#!/usr/bin/env python3
"""
Docs crawler for dynatrace-wwse.github.io/{repo}/.

Fetches pages in nav order, strips HTML, and extracts structured steps.

Usage:
  python3 docs_crawler.py --repo enablement-dql-fundamentals
  python3 docs_crawler.py --repo enablement-dql-fundamentals --base-url https://dynatrace-wwse.github.io

Returns JSON list of:
  [{ "page": "Page Title", "url": "https://...", "steps": [
      { "type": "shell|ui|verify", "content": "..." }
  ]}]

Step detection heuristics:
  - <code> / <pre> blocks → type "shell"
  - Text containing UI action keywords (click, navigate, open, go to) → type "ui"
  - Text containing assertion keywords (verify, confirm, check, assert, expect) → type "verify"
  - Everything else → type "info"
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from typing import NamedTuple
from urllib.parse import urljoin, urlparse

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("Missing deps: pip install requests beautifulsoup4", file=sys.stderr)
    sys.exit(1)


BASE_URL = "https://dynatrace-wwse.github.io"

_UI_KEYWORDS = re.compile(
    r"\b(click|navigate|open|go to|visit|select|choose|enter|type in|fill in|set|toggle|enable|disable|create|add|delete|search for|log in|sign in)\b",
    re.IGNORECASE,
)
_VERIFY_KEYWORDS = re.compile(
    r"\b(verify|confirm|check|assert|expect|should see|validate|ensure|make sure|observe)\b",
    re.IGNORECASE,
)


class Step(NamedTuple):
    type: str   # shell | ui | verify | info
    content: str


class PageDoc(NamedTuple):
    page: str
    url: str
    steps: list[Step]

    def to_dict(self) -> dict:
        return {
            "page": self.page,
            "url": self.url,
            "steps": [{"type": s.type, "content": s.content} for s in self.steps],
        }


def _classify_text(text: str) -> str:
    if _VERIFY_KEYWORDS.search(text):
        return "verify"
    if _UI_KEYWORDS.search(text):
        return "ui"
    return "info"


def _extract_steps(soup: BeautifulSoup) -> list[Step]:
    """Walk the main content area and extract steps."""
    steps: list[Step] = []

    # MkDocs Material uses <article> or .md-content
    content = (
        soup.find("article")
        or soup.find(class_="md-content__inner")
        or soup.find(class_="md-content")
        or soup.find("main")
        or soup.body
    )
    if not content:
        return steps

    for el in content.find_all(["p", "li", "pre", "code", "h2", "h3", "h4"]):
        tag = el.name

        # Code/pre → shell step
        if tag in ("pre", "code"):
            code_text = el.get_text(strip=True)
            if not code_text or len(code_text) < 3:
                continue
            # Avoid duplicating nested code inside pre
            if tag == "code" and el.parent and el.parent.name == "pre":
                continue
            steps.append(Step(type="shell", content=code_text))
            continue

        text = el.get_text(" ", strip=True)
        if not text or len(text) < 10:
            continue

        # Skip if this element contains a <pre>/<code> child (already captured above)
        if el.find(["pre", "code"]):
            continue

        kind = _classify_text(text)
        steps.append(Step(type=kind, content=text))

    return steps


def _nav_urls(base_url: str, repo: str, session: requests.Session) -> list[str]:
    """Return ordered page URLs from the MkDocs nav sidebar."""
    index_url = f"{base_url}/{repo}/"
    try:
        r = session.get(index_url, timeout=15)
        r.raise_for_status()
    except requests.RequestException as exc:
        print(f"[warn] Cannot fetch index {index_url}: {exc}", file=sys.stderr)
        return [index_url]

    soup = BeautifulSoup(r.text, "html.parser")

    # MkDocs Material: nav links are in <nav class="md-nav md-nav--primary">
    nav = soup.find("nav", class_=re.compile(r"md-nav"))
    if not nav:
        nav = soup

    seen: set[str] = set()
    urls: list[str] = []

    for a in nav.find_all("a", href=True):
        href = a["href"]
        # Resolve relative links
        full = urljoin(index_url, href)
        parsed = urlparse(full)
        # Only same-origin, same-path-prefix links
        if parsed.netloc != urlparse(index_url).netloc:
            continue
        if not parsed.path.startswith(f"/{repo}/"):
            continue
        # Drop anchors and query strings for dedup
        canonical = parsed.scheme + "://" + parsed.netloc + parsed.path
        if canonical not in seen:
            seen.add(canonical)
            urls.append(canonical)

    return urls if urls else [index_url]


def crawl_repo(
    repo: str,
    base_url: str = BASE_URL,
    session: requests.Session | None = None,
) -> list[dict]:
    """Crawl all pages of a repo's docs site.

    Returns a list of PageDoc dicts in nav order.
    """
    if session is None:
        session = requests.Session()
        session.headers["User-Agent"] = "DT-QA-Crawler/1.0"

    urls = _nav_urls(base_url, repo, session)
    results: list[dict] = []

    for url in urls:
        try:
            r = session.get(url, timeout=15)
            if r.status_code == 404:
                continue
            r.raise_for_status()
        except requests.RequestException as exc:
            print(f"[warn] Skip {url}: {exc}", file=sys.stderr)
            continue

        soup = BeautifulSoup(r.text, "html.parser")

        # Page title: <h1> or <title>
        h1 = soup.find("h1")
        title_tag = soup.find("title")
        page_title = (
            h1.get_text(strip=True) if h1
            else title_tag.get_text(strip=True) if title_tag
            else url
        )

        steps = _extract_steps(soup)
        doc = PageDoc(page=page_title, url=url, steps=steps)
        results.append(doc.to_dict())

    return results


def main():
    p = argparse.ArgumentParser(description="Crawl dynatrace-wwse.github.io docs")
    p.add_argument("--repo", required=True, help="Repo name, e.g. enablement-dql-fundamentals")
    p.add_argument("--base-url", default=BASE_URL, help="Docs site base URL")
    p.add_argument("--output", default=None, help="Write JSON to file instead of stdout")
    p.add_argument("--pretty", action="store_true", help="Pretty-print JSON")
    args = p.parse_args()

    pages = crawl_repo(args.repo, base_url=args.base_url)
    out = json.dumps(pages, indent=2 if args.pretty else None, ensure_ascii=False)

    if args.output:
        from pathlib import Path
        Path(args.output).write_text(out)
        print(f"Wrote {len(pages)} pages → {args.output}", file=sys.stderr)
    else:
        print(out)


if __name__ == "__main__":
    main()
