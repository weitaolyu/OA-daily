#!/usr/bin/env python3
"""Build a daily open-access literature digest for three astronomy topics.

Sources
-------
* arXiv Atom API (preprints, always open access)
* OpenAlex API (journal articles where ``is_oa`` is true)

The script is intentionally dependency-free: it only uses the Python
standard library. It keeps a rolling archive in ``data/archive.json`` and
renders both an HTML dashboard and a Markdown digest under ``docs/``.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import html as html_lib
import json
import os
import re
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config" / "topics.json"
DEFAULT_ARCHIVE = ROOT / "data" / "archive.json"
DEFAULT_OUT = ROOT / "docs"
DEFAULT_TEMPLATE = ROOT / "scripts" / "template.html"

ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"
RETRY_STATUS = {429, 500, 502, 503, 504}
SOURCE_ORDER = {"arXiv": 0, "OpenAlex": 1}

_DASHES = str.maketrans(
    {
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2015": "-",
        "\u2212": "-",
    }
)


def log(message: str) -> None:
    stamp = datetime.now().astimezone().strftime("%H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def clean_title(value: str | None) -> str:
    value = html_lib.unescape(value or "")
    value = unicodedata.normalize("NFKC", value).translate(_DASHES)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def clean_abstract(value: str | None) -> str:
    value = html_lib.unescape(value or "")
    value = re.sub(r"</?[A-Za-z][^>]*>", " ", value)
    value = unicodedata.normalize("NFKC", value).translate(_DASHES)
    value = re.sub(r"^\s*abstract[\s:.\-–—]*", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def clean_author_name(value: str | None) -> str:
    name = clean_title(value)
    name = re.sub(r"\s*\([^()]*\)\s*$", "", name).strip()
    name = re.sub(r"\s*\([^()]*$", "", name).strip()
    if re.search(
        r"\b(Department|University|College|Institute|Laboratory|Observatory|Center|Centre)\b",
        name,
        flags=re.IGNORECASE,
    ):
        return ""
    return name


def parse_author_list(value: str | None) -> list[str]:
    text = value or ""
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    for char in text:
        if char == "(":
            depth += 1
        elif char == ")" and depth > 0:
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return [name for name in (clean_author_name(part) for part in parts) if name]


def summarize_abstract(abstract: str, max_chars: int = 460) -> str:
    """Return a short, extractive two-sentence summary.

    This is deliberately dependency-free and deterministic: it picks the
    opening sentence plus the most informative result-bearing sentence. It is
    not an LLM rewrite, so it never invents claims that are absent from the
    abstract.
    """
    text = clean_abstract(abstract)
    if not text:
        return ""
    if len(text) <= 260:
        return text
    sentences = [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?。！？])\s+(?=[A-Z0-9\u4e00-\u9fff])", text)
        if len(sentence.strip()) >= 35
    ]
    if not sentences:
        return text[:max_chars].rsplit(" ", 1)[0] + "…"
    cues = (
        "we find",
        "we show",
        "we report",
        "we present",
        "we measure",
        "we detect",
        "we constrain",
        "we conclude",
        "results",
        "conclude",
        "reveal",
        "demonstrate",
        "detect",
        "measure",
        "constrain",
        "first",
        "new",
        "sensitivity",
        "upper limit",
        "improve",
        "future",
    )
    scored: list[tuple[float, int, str]] = []
    for index, sentence in enumerate(sentences):
        lower = sentence.lower()
        score = 0.0
        if index == 0:
            score += 2.5
        score += sum(1.2 for cue in cues if cue in lower)
        if re.search(r"\d", sentence):
            score += 0.8
        if 90 <= len(sentence) <= 260:
            score += 0.5
        scored.append((score, index, sentence))
    best_index = max(scored, key=lambda item: (item[0], -item[1]))[1]
    if len(sentences) > 1 and best_index == 0:
        best_index = max(scored[1:], key=lambda item: (item[0], -item[1]))[1]
    chosen = sorted({0, best_index})
    summary = " ".join(sentences[index] for index in chosen)
    if len(summary) > max_chars:
        summary = summary[:max_chars].rsplit(" ", 1)[0] + "…"
    return summary


def normalize_text(value: str | None) -> str:
    value = html_lib.unescape(value or "")
    value = unicodedata.normalize("NFKC", value).translate(_DASHES)
    value = re.sub(r"\s+", " ", value)
    return value.strip().lower()


def norm_title(value: str | None) -> str:
    value = unicodedata.normalize("NFKC", value or "").casefold()
    value = re.sub(r"[^\w]+", " ", value, flags=re.UNICODE)
    return " ".join(value.split())


def normalize_doi(value: str | None) -> str:
    value = (value or "").strip()
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^doi:\s*", "", value, flags=re.IGNORECASE)
    return value.strip().lower()


def normalize_arxiv_id(value: str | None) -> str:
    value = (value or "").strip()
    value = re.sub(r"^https?://arxiv\.org/(?:abs|pdf)/", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\.pdf$", "", value, flags=re.IGNORECASE)
    value = re.sub(r"v\d+$", "", value)
    return value.strip()


def keyword_pattern(term: str) -> re.Pattern[str]:
    term = term.strip().strip('"').lower()
    term = re.sub(r"\s+", " ", term)
    pieces = [re.escape(piece) for piece in re.split(r"[\s\-\u2010-\u2015\u2212]+", term) if piece]
    body = r"[\s\-\u2010-\u2015\u2212]+".join(pieces)
    prefix = r"(?<![a-z0-9])" if term[:1].isalnum() else ""
    suffix = r"(?![a-z0-9])" if term[-1:].isalnum() else ""
    return re.compile(prefix + body + suffix, re.IGNORECASE)


def paper_keys(paper: dict[str, Any]) -> set[str]:
    keys = set(paper.get("keys") or [])
    doi = normalize_doi(paper.get("doi"))
    if doi:
        keys.add(f"doi:{doi}")
    arxiv_id = normalize_arxiv_id(paper.get("arxiv_id"))
    if arxiv_id:
        keys.add(f"arxiv:{arxiv_id}")
    title = norm_title(paper.get("title"))
    if title:
        keys.add(f"title:{title}")
    return keys


def make_paper_id(paper: dict[str, Any]) -> str:
    basis = (
        normalize_arxiv_id(paper.get("arxiv_id"))
        or normalize_doi(paper.get("doi"))
        or norm_title(paper.get("title"))
    )
    return "p_" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]


class Topic:
    def __init__(self, raw: dict[str, Any], order: int):
        self.raw = raw
        self.order = order
        self.id = str(raw["id"])
        self.label = str(raw.get("label") or self.id)
        self.label_en = str(raw.get("label_en") or self.label)
        self.color = str(raw.get("color") or "#64748b")
        self.icon = str(raw.get("icon") or "•")
        self.description = str(raw.get("description") or "")
        self.categories = [str(item) for item in raw.get("arxiv_categories") or []]
        self.arxiv_queries = [str(item) for item in raw.get("arxiv_queries") or []]
        self.arxiv_groups = [
            [str(term) for term in group] for group in raw.get("arxiv_groups") or []
        ]
        if not self.arxiv_queries:
            for group in self.arxiv_groups:
                self.arxiv_queries.extend(f'all:"{term.strip(chr(34))}"' for term in group)
        if not self.arxiv_queries:
            self.arxiv_queries = [f'all:"{self.label}"']
        self.openalex_queries = [str(item) for item in raw.get("openalex_queries") or []]
        self.require_groups = [
            [str(term).lower() for term in group]
            for group in raw.get("require_groups") or []
        ]
        self.min_score = float(raw.get("min_score") or 0)
        self.weights = {
            str(term).lower(): float(weight)
            for term, weight in (raw.get("weights") or {}).items()
        }
        terms = set(self.weights)
        for group in self.require_groups:
            terms.update(group)
        self.patterns = {term: keyword_pattern(term) for term in terms}

    def _hit(self, term: str, title_l: str, abstract_l: str) -> bool:
        pattern = self.patterns.get(term) or keyword_pattern(term)
        return bool(pattern.search(title_l) or pattern.search(abstract_l))

    def match(self, title_l: str, abstract_l: str) -> tuple[float, list[str]] | None:
        for group in self.require_groups:
            if not any(self._hit(term, title_l, abstract_l) for term in group):
                return None
        score = 0.0
        matched: list[str] = []
        for term, weight in self.weights.items():
            pattern = self.patterns[term]
            in_title = bool(pattern.search(title_l))
            in_abstract = bool(pattern.search(abstract_l))
            if in_title or in_abstract:
                score += weight * (2.0 if in_title else 1.0)
                matched.append(term)
        if score < self.min_score:
            return None
        matched.sort(key=lambda item: (-self.weights.get(item, 0), item))
        return score, matched[:10]

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "label_en": self.label_en,
            "color": self.color,
            "icon": self.icon,
            "description": self.description,
        }


def load_topics(config_path: Path) -> tuple[list[Topic], int]:
    if not config_path.exists():
        raise SystemExit(f"Config not found: {config_path}")
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in {config_path}: {exc}") from exc
    entries = raw.get("topics") if isinstance(raw, dict) else raw
    if not isinstance(entries, list) or not entries:
        raise SystemExit("Config must contain a non-empty 'topics' list")
    topics = [Topic(item, index) for index, item in enumerate(entries)]
    ids = [topic.id for topic in topics]
    if len(ids) != len(set(ids)):
        raise SystemExit("Topic ids must be unique")
    offset = int(raw.get("timezone_offset_hours", 8)) if isinstance(raw, dict) else 8
    return topics, offset


def classify_record(paper: dict[str, Any], topics: list[Topic]) -> dict[str, Any] | None:
    title_l = normalize_text(paper.get("title"))
    abstract_l = normalize_text(paper.get("abstract"))
    scores: dict[str, float] = {}
    matched: dict[str, list[str]] = {}
    paper_categories = set(paper.get("categories") or [])
    for topic in topics:
        if paper_categories and topic.categories and not (paper_categories & set(topic.categories)):
            continue
        result = topic.match(title_l, abstract_l)
        if result is None:
            continue
        score, keywords = result
        scores[topic.id] = round(score, 1)
        matched[topic.id] = keywords
    if not scores:
        return None
    order = {topic.id: topic.order for topic in topics}
    paper["topics"] = [topic.id for topic in topics if topic.id in scores]
    paper["scores"] = scores
    paper["matched"] = matched
    paper["primary_topic"] = max(
        scores, key=lambda topic_id: (scores[topic_id], -order.get(topic_id, 99))
    )
    return paper


def finalize_record(paper: dict[str, Any], topics: list[Topic]) -> dict[str, Any] | None:
    paper["title"] = clean_title(paper.get("title"))
    paper["abstract"] = clean_abstract(paper.get("abstract"))
    paper["summary"] = summarize_abstract(paper["abstract"])
    if not paper["title"]:
        return None
    paper["sources"] = sorted(
        set(paper.get("sources") or []), key=lambda item: SOURCE_ORDER.get(item, 9)
    )
    if not paper["sources"]:
        paper["sources"] = ["OpenAlex"]
    paper["oa"] = bool(paper.get("oa"))
    paper["authors"] = [
        name
        for name in (clean_author_name(author) for author in paper.get("authors") or [])
        if name
    ]
    paper.setdefault("citations", 0)
    paper["citations"] = int(paper.get("citations") or 0)
    paper["keys"] = sorted(paper_keys(paper))
    paper["id"] = paper.get("id") or make_paper_id(paper)
    return classify_record(paper, topics)


def merge_paper(
    base: dict[str, Any], incoming: dict[str, Any], topic_order: dict[str, int]
) -> dict[str, Any]:
    merged = dict(base)
    merged["sources"] = sorted(
        set(base.get("sources") or []) | set(incoming.get("sources") or []),
        key=lambda item: SOURCE_ORDER.get(item, 9),
    )
    merged["topics"] = sorted(
        set(base.get("topics") or []) | set(incoming.get("topics") or []),
        key=lambda item: topic_order.get(item, 99),
    )
    scores = dict(base.get("scores") or {})
    for topic_id, score in (incoming.get("scores") or {}).items():
        scores[topic_id] = max(float(scores.get(topic_id, 0)), float(score))
    merged["scores"] = scores
    matched = {key: list(value) for key, value in (base.get("matched") or {}).items()}
    for topic_id, keywords in (incoming.get("matched") or {}).items():
        matched[topic_id] = sorted(set(matched.get(topic_id, [])) | set(keywords))
    merged["matched"] = matched
    if scores:
        merged["primary_topic"] = max(
            scores,
            key=lambda topic_id: (
                scores[topic_id],
                -topic_order.get(topic_id, 99),
            ),
        )

    dates = [value for value in (base.get("published"), incoming.get("published")) if value]
    merged["published"] = min(dates) if dates else ""
    first_seen = [value for value in (base.get("first_seen"), incoming.get("first_seen")) if value]
    merged["first_seen"] = min(first_seen) if first_seen else ""
    last_seen = [value for value in (base.get("last_seen"), incoming.get("last_seen")) if value]
    merged["last_seen"] = max(last_seen) if last_seen else ""

    if len(incoming.get("abstract") or "") > len(base.get("abstract") or ""):
        merged["abstract"] = incoming["abstract"]
    merged["summary"] = summarize_abstract(merged.get("abstract") or "")
    base_authors = base.get("authors") or []
    incoming_authors = incoming.get("authors") or []
    if len(incoming_authors) > len(base_authors) or (
        len(incoming_authors) == len(base_authors)
        and sum(len(author) for author in incoming_authors) < sum(len(author) for author in base_authors)
    ):
        merged["authors"] = incoming_authors

    def venue_rank(value: str | None) -> int:
        if not value or value == "OpenAlex":
            return 0
        if value.startswith("arXiv"):
            return 1
        return 2

    if venue_rank(incoming.get("venue")) > venue_rank(base.get("venue")):
        merged["venue"] = incoming["venue"]

    base_url = base.get("url") or ""
    incoming_url = incoming.get("url") or ""
    if base_url.startswith("https://arxiv.org") and incoming_url and not incoming_url.startswith(
        "https://arxiv.org"
    ):
        merged["url"] = incoming_url
    elif not base_url:
        merged["url"] = incoming_url
    for field in ("pdf_url", "arxiv_url", "doi", "doi_url", "arxiv_id", "openalex_id", "journal_ref"):
        if not merged.get(field) and incoming.get(field):
            merged[field] = incoming[field]
    if (
        base.get("pdf_url", "").startswith("https://arxiv.org")
        and incoming.get("pdf_url")
        and not incoming["pdf_url"].startswith("https://arxiv.org")
    ):
        merged["pdf_url"] = incoming["pdf_url"]
    if base.get("type") == "preprint" and incoming.get("type") == "article":
        merged["type"] = "article"
    merged["citations"] = max(int(base.get("citations") or 0), int(incoming.get("citations") or 0))
    merged["oa"] = bool(base.get("oa")) or bool(incoming.get("oa"))
    merged["keys"] = sorted(paper_keys(merged) | set(incoming.get("keys") or []))
    return merged


def integrate_papers(
    existing: list[dict[str, Any]], incoming: list[dict[str, Any]], topics: list[Topic]
) -> tuple[list[dict[str, Any]], int, int]:
    topic_order = {topic.id: topic.order for topic in topics}
    by_id: dict[str, dict[str, Any]] = {}
    for paper in existing:
        by_id[paper["id"]] = paper
    index: dict[str, str] = {}
    for paper in by_id.values():
        for key in paper_keys(paper):
            index[key] = paper["id"]

    new_count = 0
    updated_count = 0
    for paper in incoming:
        keys = paper_keys(paper)
        matched_ids = {index[key] for key in keys if key in index}
        if matched_ids:
            base_id = min(
                matched_ids,
                key=lambda paper_id: by_id[paper_id].get("first_seen") or "9999-12-31",
            )
            base = by_id[base_id]
            for other_id in list(matched_ids):
                if other_id == base_id:
                    continue
                base = merge_paper(base, by_id.pop(other_id), topic_order)
                for key in [k for k, value in index.items() if value == other_id]:
                    del index[key]
            base = merge_paper(base, paper, topic_order)
            by_id[base_id] = base
            updated_count += 1
        else:
            paper_id = paper["id"]
            while paper_id in by_id:
                paper_id += "x"
            paper["id"] = paper_id
            by_id[paper_id] = paper
            base = paper
            new_count += 1
        for key in paper_keys(base):
            index[key] = base["id"]
    return list(by_id.values()), new_count, updated_count


def load_archive(path: Path, today_iso: str) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid archive JSON ({path}): {exc}") from exc
    papers = raw.get("papers", []) if isinstance(raw, dict) else raw
    if not isinstance(papers, list):
        raise SystemExit(f"Archive must contain a list of papers: {path}")
    cleaned: list[dict[str, Any]] = []
    for paper in papers:
        if not isinstance(paper, dict) or not paper.get("title"):
            continue
        paper.setdefault("sources", [paper["source"]] if paper.get("source") else [])
        paper["authors"] = [
            name
            for name in (clean_author_name(author) for author in paper.get("authors") or [])
            if name
        ]
        paper.setdefault("topics", [])
        paper.setdefault("scores", {})
        paper.setdefault("matched", {})
        paper.setdefault("published", "")
        paper.setdefault("summary", summarize_abstract(paper.get("abstract") or ""))
        paper.setdefault("first_seen", (paper.get("published") or today_iso)[:10])
        paper.setdefault("last_seen", paper["first_seen"])
        paper.setdefault("id", make_paper_id(paper))
        paper.setdefault("keys", sorted(paper_keys(paper)))
        cleaned.append(paper)
    return cleaned


class PoliteClient:
    def __init__(self, delay: float = 3.0, timeout: int = 45):
        self.delay = max(0.0, delay)
        self.timeout = timeout
        self.last_request = 0.0
        repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
        self.user_agent = "oa-daily-digest/1.0"
        if repo:
            self.user_agent += f" (+https://github.com/{repo})"

    def get(self, url: str, timeout: int | None = None, attempts: int = 4) -> bytes:
        for attempt in range(attempts):
            wait = self.delay - (time.monotonic() - self.last_request)
            if wait > 0:
                time.sleep(wait)
            request = Request(
                url,
                headers={
                    "User-Agent": self.user_agent,
                    "Accept": "application/json, application/atom+xml, text/xml, */*",
                    "Accept-Encoding": "gzip",
                },
            )
            try:
                with urlopen(request, timeout=timeout or self.timeout) as response:
                    data = response.read()
                    if "gzip" in (response.headers.get("Content-Encoding") or "").lower():
                        data = gzip.decompress(data)
                    self.last_request = time.monotonic()
                    return data
            except HTTPError as exc:
                self.last_request = time.monotonic()
                if exc.code in RETRY_STATUS and attempt < attempts - 1:
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    retry_seconds = 0
                    if retry_after and retry_after.isdigit():
                        retry_seconds = int(retry_after)
                    delay = max(retry_seconds, min(60, 5 * (2**attempt)))
                    log(f"HTTP {exc.code}; retrying in {delay}s: {url[:140]}")
                    time.sleep(delay)
                    continue
                raise
            except (URLError, TimeoutError, OSError) as exc:
                self.last_request = time.monotonic()
                if attempt < attempts - 1:
                    delay = min(60, 5 * (2**attempt))
                    log(f"{type(exc).__name__}; retrying in {delay}s: {url[:140]}")
                    time.sleep(delay)
                    continue
                raise
        raise RuntimeError("unreachable")

    def get_json(self, url: str) -> dict[str, Any]:
        return json.loads(self.get(url).decode("utf-8"))

    def get_xml(self, url: str, timeout: int = 90, attempts: int = 4) -> ET.Element:
        return ET.fromstring(self.get(url, timeout=timeout, attempts=attempts))


def build_arxiv_query(term: str, start: datetime, end: datetime) -> str:
    term = term.strip()
    if not term.startswith("all:") and not term.startswith("cat:"):
        term = f"all:{term}"
    date_expr = (
        f"submittedDate:[{start.strftime('%Y%m%d%H%M')} TO {end.strftime('%Y%m%d%H%M')}]"
    )
    return f"({term}) AND {date_expr}"


def parse_arxiv_entry(
    entry: ET.Element, today_iso: str, now_iso: str, topics: list[Topic]
) -> dict[str, Any] | None:
    title_el = entry.find(ATOM + "title")
    title = clean_title(title_el.text if title_el is not None else "")
    if not title or title.lower() == "error":
        return None
    raw_id = (entry.findtext(ATOM + "id") or "").strip()
    arxiv_id = normalize_arxiv_id(raw_id)
    if not arxiv_id:
        return None
    abstract = clean_abstract(entry.findtext(ATOM + "summary"))
    published_raw = (entry.findtext(ATOM + "published") or "")[:10]
    updated_raw = (entry.findtext(ATOM + "updated") or "")[:10]
    primary_el = entry.find(ARXIV + "primary_category")
    primary = primary_el.get("term") if primary_el is not None else ""
    categories = [
        (category.get("term") or "").strip()
        for category in entry.findall(ATOM + "category")
        if category.get("term")
    ]
    if primary and primary not in categories:
        categories.insert(0, primary)
    authors = [
        clean_title(author.findtext(ATOM + "name"))
        for author in entry.findall(ATOM + "author")
        if author.findtext(ATOM + "name")
    ]
    journal_ref = clean_title(entry.findtext(ARXIV + "journal_ref"))
    doi = normalize_doi(entry.findtext(ARXIV + "doi"))
    abs_url = f"https://arxiv.org/abs/{arxiv_id}"
    pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"
    for link in entry.findall(ATOM + "link"):
        href = link.get("href") or ""
        if link.get("title") == "pdf" and href:
            pdf_url = href
        elif link.get("rel") == "alternate" and href:
            abs_url = href
    paper: dict[str, Any] = {
        "title": title,
        "abstract": abstract,
        "authors": authors,
        "published": published_raw or updated_raw,
        "updated": updated_raw,
        "first_seen": today_iso,
        "last_seen": now_iso,
        "sources": ["arXiv"],
        "venue": journal_ref or (f"arXiv · {primary}" if primary else "arXiv"),
        "journal_ref": journal_ref,
        "doi": doi,
        "doi_url": f"https://doi.org/{doi}" if doi else "",
        "url": abs_url,
        "pdf_url": pdf_url,
        "arxiv_id": arxiv_id,
        "arxiv_url": abs_url,
        "oa": True,
        "type": "preprint",
        "citations": 0,
        "primary_category": primary,
        "categories": categories,
    }
    return finalize_record(paper, topics)


def fetch_arxiv(
    topic: Topic,
    start: datetime,
    end: datetime,
    client: PoliteClient,
    max_results: int,
    today_iso: str,
    now_iso: str,
    topics: list[Topic],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    failures = 0
    for term in topic.arxiv_queries:
        if getattr(client, "arxiv_disabled", False):
            break
        query = build_arxiv_query(term, start, end)
        params = urlencode(
            {
                "search_query": query,
                "start": 0,
                "max_results": max_results,
                "sortBy": "submittedDate",
                "sortOrder": "descending",
            }
        )
        url = f"https://arxiv.org/api/query?{params}"
        log(f"[arXiv] {topic.id}: {query[:160]}")
        try:
            root = client.get_xml(url, timeout=45, attempts=2)
        except Exception as exc:  # noqa: BLE001 - try the next simple query
            failures += 1
            log(f"[arXiv] {topic.id}/{term!r} failed: {type(exc).__name__}: {exc}")
            if failures >= 2:
                setattr(client, "arxiv_disabled", True)
                log("[arXiv] API unavailable; falling back to the RSS feeds")
                break
            continue
        failures = 0
        entries = root.findall(ATOM + "entry")
        matched = 0
        for entry in entries:
            paper = parse_arxiv_entry(entry, today_iso, now_iso, topics)
            if paper is None:
                continue
            arxiv_id = paper.get("arxiv_id")
            if arxiv_id in seen_ids:
                continue
            seen_ids.add(arxiv_id)
            records.append(paper)
            matched += 1
        log(f"[arXiv] {topic.id}/{term!r}: {len(entries)} entries, {matched} matched")
    return records


def fetch_arxiv_rss(
    topics: list[Topic],
    client: PoliteClient,
    today_iso: str,
    now_iso: str,
) -> list[dict[str, Any]]:
    categories: list[str] = []
    for topic in topics:
        for category in topic.categories:
            if category not in categories:
                categories.append(category)
    records: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for category in categories:
        url = f"https://rss.arxiv.org/rss/{category}"
        log(f"[arXiv RSS] {category}")
        try:
            root = client.get_xml(url, timeout=45, attempts=2)
        except Exception as exc:  # noqa: BLE001 - one feed should not kill the run
            log(f"[arXiv RSS] {category} failed: {type(exc).__name__}: {exc}")
            continue
        items = root.findall("./channel/item")
        matched = 0
        for item in items:
            link = (item.findtext("link") or "").strip()
            arxiv_id = normalize_arxiv_id(link)
            if not arxiv_id:
                description = item.findtext("description") or ""
                match = re.search(r"arXiv:(\d{4}\.\d{4,5})", description)
                arxiv_id = match.group(1) if match else ""
            if not arxiv_id or arxiv_id in seen_ids:
                continue
            announce_type = (item.findtext(ARXIV + "announce_type") or "").strip().lower()
            if announce_type and announce_type not in {"new", "cross"}:
                continue
            title = clean_title(item.findtext("title"))
            if not title:
                continue
            raw_description = item.findtext("description") or ""
            abstract_match = re.search(r"Abstract:\s*(.+)$", raw_description, re.IGNORECASE | re.DOTALL)
            abstract = clean_abstract(
                abstract_match.group(1) if abstract_match else raw_description
            )
            author_text = item.findtext("{http://purl.org/dc/elements/1.1/}creator") or ""
            authors = parse_author_list(author_text)
            item_categories = [
                (element.text or "").strip()
                for element in item.findall("category")
                if element.text
            ]
            primary = item_categories[0] if item_categories else category
            if primary not in item_categories:
                item_categories.insert(0, primary)
            published = today_iso
            raw_date = item.findtext("pubDate") or ""
            try:
                published = parsedate_to_datetime(raw_date).date().isoformat()
            except (TypeError, ValueError, OverflowError):
                pass
            journal_ref = clean_title(item.findtext(ARXIV + "journal_ref"))
            doi = normalize_doi(item.findtext(ARXIV + "doi"))
            abs_url = link or f"https://arxiv.org/abs/{arxiv_id}"
            paper: dict[str, Any] = {
                "title": title,
                "abstract": abstract,
                "authors": authors,
                "published": published,
                "updated": published,
                "first_seen": today_iso,
                "last_seen": now_iso,
                "sources": ["arXiv"],
                "venue": journal_ref or (f"arXiv · {primary}" if primary else "arXiv"),
                "journal_ref": journal_ref,
                "doi": doi,
                "doi_url": f"https://doi.org/{doi}" if doi else "",
                "url": abs_url,
                "pdf_url": f"https://arxiv.org/pdf/{arxiv_id}",
                "arxiv_id": arxiv_id,
                "arxiv_url": abs_url,
                "oa": True,
                "type": "preprint",
                "citations": 0,
                "primary_category": primary,
                "categories": item_categories,
            }
            finalized = finalize_record(paper, topics)
            if finalized is None:
                continue
            seen_ids.add(arxiv_id)
            records.append(finalized)
            matched += 1
        log(f"[arXiv RSS] {category}: {len(items)} items, {matched} matched")
    return records


def rebuild_abstract(inverted: dict[str, list[int]] | None) -> str:
    if not inverted:
        return ""
    positions: list[tuple[int, str]] = []
    for word, indices in inverted.items():
        for position in indices:
            positions.append((int(position), word))
    positions.sort()
    text = " ".join(word for _, word in positions)
    text = re.sub(r"\s+([,.;:!?%)\]])", r"\1", text)
    text = re.sub(r"([(\[])\s+", r"\1", text)
    return text


OPENALEX_SELECT = ",".join(
    [
        "id",
        "doi",
        "title",
        "display_name",
        "publication_date",
        "publication_year",
        "created_date",
        "updated_date",
        "type",
        "open_access",
        "best_oa_location",
        "primary_location",
        "authorships",
        "abstract_inverted_index",
        "cited_by_count",
    ]
)


def fetch_openalex(
    topic: Topic,
    query: str,
    start_date: str,
    end_date: str,
    client: PoliteClient,
    max_results: int,
    today_iso: str,
    now_iso: str,
    topics: list[Topic],
) -> list[dict[str, Any]]:
    filter_value = (
        f"is_oa:true,from_publication_date:{start_date},"
        f"to_publication_date:{end_date},type:article"
    )
    params: list[tuple[str, str]] = [
        ("filter", filter_value),
        ("search", query),
        ("per-page", str(max_results)),
        ("select", OPENALEX_SELECT),
    ]
    mailto = os.environ.get("OPENALEX_MAILTO", "").strip()
    if mailto:
        params.append(("mailto", mailto))
    url = "https://api.openalex.org/works?" + "&".join(
        f"{key}={quote(value, safe=':,|')}" for key, value in params
    )
    log(f"[OpenAlex] {topic.id}: {query!r}")
    try:
        data = client.get_json(url)
    except HTTPError as exc:
        if exc.code != 400:
            raise
        log("[OpenAlex] 400 response; retrying without select field")
        params = [item for item in params if item[0] != "select"]
        url = "https://api.openalex.org/works?" + "&".join(
            f"{key}={quote(value, safe=':,|')}" for key, value in params
        )
        data = client.get_json(url)
    results = data.get("results") or []
    records: list[dict[str, Any]] = []
    for work in results:
        title = clean_title(work.get("title") or work.get("display_name"))
        if not title:
            continue
        abstract = clean_abstract(rebuild_abstract(work.get("abstract_inverted_index")))
        oa = work.get("open_access") or {}
        best = work.get("best_oa_location") or {}
        primary = work.get("primary_location") or {}
        source = (primary.get("source") or best.get("source") or {}) if isinstance(primary, dict) else {}
        venue = clean_title(source.get("display_name") if isinstance(source, dict) else "")
        doi = normalize_doi(work.get("doi"))
        landing = best.get("landing_page_url") or oa.get("oa_url") or (f"https://doi.org/{doi}" if doi else "")
        pdf_url = best.get("pdf_url") or ""
        authors = []
        for authorship in work.get("authorships") or []:
            author = (authorship or {}).get("author") or {}
            name = clean_title(author.get("display_name"))
            if name:
                authors.append(name)
        published = (work.get("publication_date") or work.get("created_date") or work.get("updated_date") or "")[:10]
        openalex_id = (work.get("id") or "").rsplit("/", 1)[-1]
        paper: dict[str, Any] = {
            "title": title,
            "abstract": abstract,
            "authors": authors,
            "published": published,
            "first_seen": today_iso,
            "last_seen": now_iso,
            "sources": ["OpenAlex"],
            "venue": venue or "OpenAlex",
            "doi": doi,
            "doi_url": f"https://doi.org/{doi}" if doi else "",
            "url": landing or (f"https://openalex.org/{openalex_id}" if openalex_id else ""),
            "pdf_url": pdf_url,
            "openalex_id": openalex_id,
            "oa": bool(oa.get("is_oa")),
            "type": work.get("type") or "article",
            "citations": int(work.get("cited_by_count") or 0),
        }
        finalized = finalize_record(paper, topics)
        if finalized is not None:
            records.append(finalized)
    log(
        f"[OpenAlex] {topic.id}: {data.get('meta', {}).get('count', len(results))} hits, "
        f"{len(records)} matched"
    )
    return records


def public_paper(paper: dict[str, Any]) -> dict[str, Any]:
    fields = [
        "id",
        "title",
        "abstract",
        "summary",
        "authors",
        "published",
        "first_seen",
        "last_seen",
        "sources",
        "venue",
        "journal_ref",
        "doi",
        "doi_url",
        "url",
        "pdf_url",
        "arxiv_id",
        "arxiv_url",
        "openalex_id",
        "oa",
        "type",
        "citations",
        "topics",
        "scores",
        "matched",
        "primary_topic",
    ]
    result: dict[str, Any] = {}
    for field in fields:
        value = paper.get(field)
        if value is None:
            continue
        result[field] = value
    result["oa"] = bool(paper.get("oa"))
    result["sources"] = paper.get("sources") or []
    result["authors"] = paper.get("authors") or []
    result["topics"] = paper.get("topics") or []
    result["scores"] = paper.get("scores") or {}
    result["matched"] = paper.get("matched") or {}
    return result


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def venue_display(paper: dict[str, Any]) -> str:
    venue = paper.get("venue") or ""
    if paper.get("journal_ref") and paper["journal_ref"] not in venue:
        return f"{venue} · {paper['journal_ref']}" if venue else paper["journal_ref"]
    return venue


def format_authors(authors: list[str], limit: int = 6) -> str:
    if not authors:
        return "未知作者"
    if len(authors) <= limit:
        return ", ".join(authors)
    return ", ".join(authors[:limit]) + f", et al. ({len(authors)} 位作者)"


def md_text(value: str) -> str:
    value = html_lib.unescape(value or "")
    for char in ("\\", "[", "]", "*", "_", "`"):
        value = value.replace(char, "\\" + char)
    return value.replace("<", "&lt;").replace(">", "&gt;")


def md_url(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if any(char in value for char in " ()<>"):
        value = value.replace(" ", "%20").replace("(", "%28").replace(")", "%29")
        return f"<{value}>"
    return value


def md_link(label: str, url: str) -> str:
    if not url:
        return label
    return f"[{md_text(label)}]({md_url(url)})"


def paper_links(paper: dict[str, Any]) -> list[str]:
    links: list[str] = []
    if paper.get("url"):
        links.append(md_link("原文", paper["url"]))
    if paper.get("arxiv_url") and paper.get("arxiv_url") != paper.get("url"):
        links.append(md_link("arXiv", paper["arxiv_url"]))
    if paper.get("pdf_url"):
        links.append(md_link("PDF", paper["pdf_url"]))
    if paper.get("doi_url"):
        links.append(md_link("DOI", paper["doi_url"]))
    return links


def paper_tags(paper: dict[str, Any]) -> list[str]:
    tags: list[str] = []
    for topic_id in paper.get("topics") or []:
        tags.extend((paper.get("matched") or {}).get(topic_id) or [])
    seen: set[str] = set()
    result = []
    for tag in tags:
        if tag not in seen:
            seen.add(tag)
            result.append(tag)
    return result[:8]


def render_digest(
    new_papers: list[dict[str, Any]],
    paper_count: int,
    topics: list[Topic],
    today: str,
    generated_label: str,
    lookback_days: int,
    archive_days: int,
) -> str:
    order = {topic.id: topic.order for topic in topics}
    counts = {topic.id: 0 for topic in topics}
    grouped: dict[str, list[dict[str, Any]]] = {topic.id: [] for topic in topics}
    other: list[dict[str, Any]] = []
    for paper in new_papers:
        primary = paper.get("primary_topic")
        if primary in grouped:
            grouped[primary].append(paper)
            counts[primary] += 1
        else:
            other.append(paper)
    for items in grouped.values():
        items.sort(key=lambda item: (-(item.get("scores") or {}).get(item.get("primary_topic", ""), 0), item.get("title", "")))
    other.sort(key=lambda item: item.get("title", ""))

    lines = [
        f"# OA Daily · {today}",
        "",
        f"> 每日开放获取文献 · 北京时间 {generated_label} 自动更新  ",
        f"> 抓取窗口：最近 {lookback_days} 天 · 滚动归档：{archive_days} 天 · 今日新收录 **{len(new_papers)}** 篇（归档共 {paper_count} 篇）",
        "",
        "| 主题 | 今日新增 |",
        "|---|---:|",
    ]
    for topic in topics:
        lines.append(f"| {topic.icon} {md_text(topic.label)} | {counts[topic.id]} |")
    if other:
        lines.append(f"| 其他 | {len(other)} |")
    lines.extend(
        [
            "",
            "> 主题：MKIDs 探测器 / 亚毫米波天文 / 分子云磁场 · "
            f"[打开交互式仪表盘](../index.html) · [日报归档](index.md)",
            "",
        ]
    )

    if not new_papers:
        lines.extend(
            [
                "## 今日暂无新收录",
                "",
                "今天没有抓取到新的匹配文献。可以稍后手动运行工作流，或在仪表盘查看最近的归档。",
                "",
            ]
        )

    section_order = sorted(topics, key=lambda topic: topic.order)
    for topic in section_order:
        items = grouped.get(topic.id) or []
        if not items:
            continue
        lines.extend(
            [
                f"## {topic.icon} {md_text(topic.label)}（{len(items)}）",
                "",
                f"*{md_text(topic.description)}*",
                "",
            ]
        )
        for paper in items:
            url = paper.get("url") or paper.get("doi_url") or paper.get("arxiv_url") or ""
            title = md_text(paper.get("title") or "Untitled")
            lines.append(f"### [{title}]({md_url(url)})" if url else f"### {title}")
            lines.append("")
            authors = format_authors(paper.get("authors") or [])
            meta_bits = [
                f"**{md_text(paper.get('published') or '日期未知')}**",
                f"来源：{md_text(' + '.join(paper.get('sources') or []))}",
            ]
            venue = venue_display(paper)
            if venue:
                meta_bits.append(md_text(venue))
            if paper.get("oa"):
                meta_bits.append("开放获取")
            lines.append(" · ".join(meta_bits) + "  ")
            lines.append(f"**作者**：{md_text(authors)}  ")
            tags = paper_tags(paper)
            if tags:
                lines.append("**匹配**：" + " · ".join(f"`{md_text(tag)}`" for tag in tags) + "  ")
            links = paper_links(paper)
            if links:
                lines.append("**链接**：" + " · ".join(links))
            summary = (paper.get("summary") or "").strip()
            if summary:
                lines.extend(["", f"**速读总结（自动提取）**：{md_text(summary)}"])
            abstract = (paper.get("abstract") or "").strip()
            if abstract:
                snippet = abstract[:900] + ("…" if len(abstract) > 900 else "")
                lines.extend(["", "**摘要**", "", f"> {md_text(snippet)}", ""])
            else:
                lines.append("")
        lines.append("")

    if other:
        lines.extend(["## 其他（未归类）", ""])
        for paper in other:
            url = paper.get("url") or paper.get("doi_url") or ""
            lines.append(f"### {md_link(paper.get('title') or 'Untitled', url)}")
            lines.append("")
            lines.append(f"**作者**：{md_text(format_authors(paper.get('authors') or []))}  ")
            summary = (paper.get("summary") or "").strip()
            if summary:
                lines.append(f"**速读总结（自动提取）**：{md_text(summary)}  ")
            links = paper_links(paper)
            if links:
                lines.append("**链接**：" + " · ".join(links))
            lines.append("")

    lines.extend(
        [
            "---",
            "",
            "由 GitHub Actions 自动生成 · 数据来自 arXiv 与 OpenAlex（仅开放获取记录）· "
            f"[返回仪表盘](../index.html)",
            "",
        ]
    )
    return "\n".join(lines)


def update_digest_index(
    digests_dir: Path,
    today: str,
    total: int,
    counts: dict[str, int],
    topics: list[Topic],
    generated_label: str,
) -> None:
    index_path = digests_dir / "index.json"
    entries: list[dict[str, Any]] = []
    if index_path.exists():
        try:
            raw = json.loads(index_path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                entries = [item for item in raw if isinstance(item, dict) and item.get("date")]
        except json.JSONDecodeError:
            log("Digest index was invalid; rebuilding it")
    entries = [item for item in entries if item.get("date") != today]
    entries.append(
        {
            "date": today,
            "total": total,
            "counts": counts,
            "generated": generated_label,
        }
    )
    entries.sort(key=lambda item: item.get("date", ""), reverse=True)
    write_json(index_path, entries)

    lines = [
        "# 每日文摘归档",
        "",
        "> 每个日期对应一份 Markdown 日报；仪表盘在 [这里](../index.html)。",
        "",
        "| 日期 | 今日新增 | " + " | ".join(topic.label for topic in topics) + " | 链接 |",
        "|---|---:|" + "---:|" * len(topics) + "---|",
    ]
    for entry in entries:
        date = entry.get("date", "")
        entry_counts = entry.get("counts") or {}
        cells = [str(entry_counts.get(topic.id, 0)) for topic in topics]
        lines.append(
            f"| {date} | {entry.get('total', 0)} | " + " | ".join(cells) + f" | [查看]({date}.md) |"
        )
    lines.append("")
    write_text(digests_dir / "index.md", "\n".join(lines))


def render_html(
    template_path: Path,
    meta: dict[str, Any],
    topics: list[Topic],
    papers: list[dict[str, Any]],
) -> str:
    if not template_path.exists():
        raise SystemExit(f"HTML template not found: {template_path}")
    template = template_path.read_text(encoding="utf-8")
    payload = {
        "meta": meta,
        "topics": [topic.public() for topic in topics],
        "papers": [public_paper(paper) for paper in papers],
    }
    data_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    data_json = (
        data_json.replace("</", "<\\/")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    repo_link = (
        f'<a href="https://github.com/{html_lib.escape(repo)}" target="_blank" rel="noopener">GitHub 仓库</a>'
        if repo
        else ""
    )
    replacements = {
        "__GENERATED_LABEL__": html_lib.escape(meta["generated_label"]),
        "__TODAY_LABEL__": html_lib.escape(meta["today"]),
        "__TOPIC_SUMMARY__": html_lib.escape(" · ".join(topic.label for topic in topics)),
        "__PAPER_COUNT__": str(meta["paper_count"]),
        "__NEW_COUNT__": str(meta["new_count"]),
        "__DIGEST_URL__": html_lib.escape(meta["digest_url"]),
        "__ARCHIVE_URL__": html_lib.escape(meta["archive_url"]),
        "__REPO_LINK__": repo_link,
        "__YEAR__": str(datetime.now().year),
        "__DATA_JSON__": data_json,
    }
    for key, value in replacements.items():
        template = template.replace(key, value)
    return template


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch daily open-access papers and render an HTML/Markdown digest."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    parser.add_argument("--days", type=int, default=7, help="Publication lookback window in days")
    parser.add_argument(
        "--archive-days", type=int, default=60, help="Keep papers seen/published within N days"
    )
    parser.add_argument(
        "--max-per-query", type=int, default=200, help="Maximum records fetched per query"
    )
    parser.add_argument(
        "--delay", type=float, default=3.0, help="Seconds between HTTP requests (politeness)"
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Do not fetch; rebuild the HTML/Markdown from the existing archive",
    )
    parser.add_argument(
        "--skip-arxiv", action="store_true", help="Do not query arXiv (useful for debugging)"
    )
    parser.add_argument(
        "--skip-arxiv-api",
        action="store_true",
        help="Skip the arXiv API but still read the daily arXiv RSS feeds",
    )
    parser.add_argument(
        "--skip-openalex",
        action="store_true",
        help="Do not query OpenAlex (useful for debugging)",
    )
    parser.add_argument(
        "--seed",
        action="store_true",
        help="Backfill mode: for older papers, set first_seen to the publication date so only today's papers are marked new",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    topics, offset_hours = load_topics(args.config)
    tz = timezone(timedelta(hours=offset_hours))
    now_local = datetime.now(tz)
    today = now_local.date()
    today_iso = today.isoformat()
    now_iso = now_local.isoformat(timespec="seconds")
    generated_label = now_local.strftime("%Y-%m-%d %H:%M")

    start_date = today - timedelta(days=max(0, args.days - 1))
    start_dt = datetime.combine(start_date, datetime.min.time(), tzinfo=timezone.utc)
    end_dt = datetime.combine(today + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)

    papers = load_archive(args.archive, today_iso)
    errors = 0
    new_count = 0
    updated_count = 0

    if not args.offline:
        client = PoliteClient(delay=args.delay)
        fetched: list[dict[str, Any]] = []
        for topic in topics:
            if not args.skip_arxiv and not args.skip_arxiv_api:
                try:
                    fetched.extend(
                        fetch_arxiv(
                            topic,
                            start_dt,
                            end_dt,
                            client,
                            min(args.max_per_query, 2000),
                            today_iso,
                            now_iso,
                            topics,
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - keep other sources running
                    errors += 1
                    log(f"[arXiv] {topic.id} failed: {type(exc).__name__}: {exc}")
            if args.skip_openalex:
                continue
            for query in topic.openalex_queries:
                try:
                    fetched.extend(
                        fetch_openalex(
                            topic,
                            query,
                            start_date.isoformat(),
                            today_iso,
                            client,
                            min(args.max_per_query, 200),
                            today_iso,
                            now_iso,
                            topics,
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - one query should not kill the run
                    errors += 1
                    log(f"[OpenAlex] {topic.id}/{query!r} failed: {type(exc).__name__}: {exc}")
        if not args.skip_arxiv:
            try:
                fetched.extend(fetch_arxiv_rss(topics, client, today_iso, now_iso))
            except Exception as exc:  # noqa: BLE001 - RSS is a best-effort safety net
                errors += 1
                log(f"[arXiv RSS] failed: {type(exc).__name__}: {exc}")
        log(f"Fetched {len(fetched)} classified records; merging archive")
        papers, new_count, updated_count = integrate_papers(papers, fetched, topics)
    else:
        log(f"Offline mode: loaded {len(papers)} archived papers")

    if args.archive_days > 0:
        cutoff = (today - timedelta(days=args.archive_days)).isoformat()
        papers = [
            paper
            for paper in papers
            if any(
                (paper.get(field) or "")[:10] >= cutoff
                for field in ("published", "first_seen", "last_seen")
            )
        ]

    if args.seed:
        for paper in papers:
            published = (paper.get("published") or "")[:10]
            if published and published < today_iso and paper.get("first_seen") == today_iso:
                paper["first_seen"] = published

    papers.sort(
        key=lambda paper: (
            paper.get("published") or "",
            paper.get("first_seen") or "",
            paper.get("title") or "",
        ),
        reverse=True,
    )
    new_papers = [paper for paper in papers if paper.get("first_seen") == today_iso]
    new_papers.sort(
        key=lambda paper: (
            -float((paper.get("scores") or {}).get(paper.get("primary_topic", ""), 0)),
            paper.get("title") or "",
        )
    )

    args.out.mkdir(parents=True, exist_ok=True)
    meta = {
        "generated_at": now_iso,
        "generated_label": generated_label,
        "today": today_iso,
        "timezone_label": f"UTC{offset_hours:+d}",
        "lookback_days": args.days,
        "archive_days": args.archive_days,
        "paper_count": len(papers),
        "new_count": len(new_papers),
        "digest_url": f"digests/{today_iso}.md",
        "archive_url": "digests/index.md",
    }
    payload = {
        "meta": meta,
        "topics": [topic.public() for topic in topics],
        "papers": [public_paper(paper) for paper in papers],
    }
    write_json(args.archive, payload)
    write_json(args.out / "data" / "papers.json", payload)
    write_text(
        args.out / "index.html", render_html(args.template, meta, topics, papers)
    )
    write_text(args.out / ".nojekyll", "")

    digests_dir = args.out / "digests"
    digests_dir.mkdir(parents=True, exist_ok=True)
    counts = {topic.id: 0 for topic in topics}
    for paper in new_papers:
        primary = paper.get("primary_topic")
        if primary in counts:
            counts[primary] += 1
    digest = render_digest(
        new_papers,
        len(papers),
        topics,
        today_iso,
        generated_label,
        args.days,
        args.archive_days,
    )
    write_text(digests_dir / f"{today_iso}.md", digest)
    update_digest_index(
        digests_dir, today_iso, len(new_papers), counts, topics, generated_label
    )

    log(
        f"Done: {len(papers)} archived, {len(new_papers)} new today, "
        f"{new_count} new records, {updated_count} updated records, {errors} source errors"
    )
    log(f"Dashboard: {(args.out / 'index.html').resolve()}")
    if errors and not papers:
        log("All sources failed and the archive is empty")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
