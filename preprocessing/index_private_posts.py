#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from elasticsearch import Elasticsearch
from elasticsearch.helpers import bulk

REPO_ROOT = Path(__file__).resolve().parents[1]
PRIVATE_POSTS_DIR = REPO_ROOT / "_private_posts"

DEFAULT_INDEX_NAME = "private_posts"


@dataclass
class SearchDocument:
    id: str
    title: str
    url: str
    date: str
    content: str
    tags: list[str]
    categories: list[str]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Index _private_posts markdown files into Elasticsearch")
    parser.add_argument("--index", default=DEFAULT_INDEX_NAME, help="Target Elasticsearch index")
    parser.add_argument(
        "--elasticsearch-url",
        default="http://localhost:9200",
        help="Elasticsearch URL, e.g. http://localhost:9200",
    )
    parser.add_argument("--username", help="Elasticsearch username", default=None)
    parser.add_argument("--password", help="Elasticsearch password", default=None)
    parser.add_argument("--api-key", help="Elasticsearch API key", default=None)
    parser.add_argument(
        "--private-posts-dir",
        type=Path,
        default=PRIVATE_POSTS_DIR,
        help="Directory containing private post markdown files",
    )
    parser.add_argument(
        "--no-recreate",
        action="store_true",
        help="Do not delete and recreate the index before indexing",
    )
    return parser.parse_args()


def split_front_matter(text: str) -> tuple[dict[str, Any], str]:
    match = re.match(r"^---\n(?P<front_matter>.*?)\n---\n(?P<body>.*)$", text, re.DOTALL)
    if not match:
        return {}, text

    front_matter_raw = match.group("front_matter")
    body = match.group("body")
    parsed = yaml.safe_load(front_matter_raw) or {}
    if not isinstance(parsed, dict):
        parsed = {}
    return parsed, body


def normalize_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        candidate = value.strip()
        if not candidate:
            return []
        if candidate.startswith("[") and candidate.endswith("]"):
            parts = [part.strip() for part in candidate.strip("[]").split(",")]
            return [part.strip("'\"") for part in parts if part]
        return [candidate]
    return [str(value).strip()]


def strip_markdown(text: str) -> str:
    cleaned = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    cleaned = re.sub(r"`([^`]*)`", r"\1", cleaned)
    cleaned = re.sub(r"!\[[^\]]*\]\([^\)]*\)", " ", cleaned)
    cleaned = re.sub(r"\[([^\]]+)\]\([^\)]*\)", r"\1", cleaned)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = re.sub(r"(^|\n)#{1,6}\s*", " ", cleaned)
    cleaned = re.sub(r"\*\*|__|\*|_", " ", cleaned)
    cleaned = re.sub(r"\n+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def parse_filename(path: Path) -> tuple[str, str] | None:
    match = re.match(r"(?P<date>\d{4}-\d{2}-\d{2})-(?P<slug>.+)\.md$", path.name)
    if not match:
        return None
    return match.group("date"), match.group("slug")


def build_doc(path: Path) -> SearchDocument | None:
    parsed_name = parse_filename(path)
    if not parsed_name:
        return None

    file_date, slug = parsed_name
    raw = path.read_text(encoding="utf-8")
    front_matter, body = split_front_matter(raw)

    title = str(front_matter.get("title") or slug.replace("-", " ").title()).strip()
    date_value = str(front_matter.get("date") or file_date).strip()

    try:
        normalized_date = datetime.fromisoformat(date_value.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        normalized_date = file_date

    content = strip_markdown(body)
    url = f"/private/{file_date[0:4]}/{file_date[5:7]}/{file_date[8:10]}/{slug}/"
    doc_id = f"{file_date}-{slug}"

    return SearchDocument(
        id=doc_id,
        title=title,
        url=url,
        date=normalized_date,
        content=content,
        tags=normalize_list(front_matter.get("tags")),
        categories=normalize_list(front_matter.get("categories")),
    )


def load_documents(private_posts_dir: Path) -> list[SearchDocument]:
    docs: list[SearchDocument] = []
    for path in sorted(private_posts_dir.glob("*.md")):
        doc = build_doc(path)
        if doc:
            docs.append(doc)
    return docs


def create_index(client: Elasticsearch, index_name: str, recreate: bool) -> None:
    if recreate and client.indices.exists(index=index_name):
        client.indices.delete(index=index_name)

    if client.indices.exists(index=index_name):
        return

    client.indices.create(
        index=index_name,
        mappings={
            "properties": {
                "id": {"type": "keyword"},
                "title": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
                "url": {"type": "keyword"},
                "date": {"type": "date"},
                "content": {"type": "text"},
                "tags": {"type": "keyword"},
                "categories": {"type": "keyword"},
            }
        },
    )


def to_bulk_actions(index_name: str, docs: list[SearchDocument]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for doc in docs:
        actions.append(
            {
                "_op_type": "index",
                "_index": index_name,
                "_id": doc.id,
                "_source": {
                    "id": doc.id,
                    "title": doc.title,
                    "url": doc.url,
                    "date": doc.date,
                    "content": doc.content,
                    "tags": doc.tags,
                    "categories": doc.categories,
                },
            }
        )
    return actions


def build_es_client(args: argparse.Namespace) -> Elasticsearch:
    kwargs: dict[str, Any] = {"hosts": [args.elasticsearch_url]}
    if args.api_key:
        kwargs["api_key"] = args.api_key
    elif args.username and args.password:
        kwargs["basic_auth"] = (args.username, args.password)
    return Elasticsearch(**kwargs)


def main() -> int:
    args = parse_args()
    private_posts_dir = args.private_posts_dir.expanduser()
    if not private_posts_dir.is_absolute():
        private_posts_dir = (REPO_ROOT / private_posts_dir).resolve()

    if not private_posts_dir.exists():
        raise FileNotFoundError(f"Private posts directory not found: {private_posts_dir}")

    docs = load_documents(private_posts_dir)
    if not docs:
        print(f"No private posts found in {private_posts_dir}")
        return 0

    client = build_es_client(args)
    create_index(client, args.index, recreate=not args.no_recreate)

    actions = to_bulk_actions(args.index, docs)
    success_count, _ = bulk(client, actions, refresh="wait_for")
    print(f"Indexed {success_count} private posts into '{args.index}'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
