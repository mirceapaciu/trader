"""Replay dead-lettered messages back into the news_raw_queue.

Usage:
    uv run python scripts/replay_dlq.py                          # dry-run (default)
    uv run python scripts/replay_dlq.py --execute                # actually replay
    uv run python scripts/replay_dlq.py --error-code missing_article_payload  # filter by error code
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from urllib.parse import quote, urlsplit, urlunsplit

import redis

from src.product_components.news_fetcher.env_loader import load_env_files


def _load_env() -> None:
    root = Path(__file__).resolve().parent.parent
    load_env_files(root, filenames=(".env.shared", ".env.prod", ".env.secrets"))
    # Redis deployment env (contains REDIS_PASSWORD)
    redis_env = root / "scripts" / "deployment" / "redis" / ".env"
    if redis_env.is_file():
        load_env_files(redis_env.parent, filenames=(".env",))


def _queue_url() -> str:
    url = os.environ.get("QUEUE_URL", "redis://127.0.0.1:6379/0").strip()
    password = (os.environ.get("REDIS_PASSWORD") or "").strip()
    if not password:
        return url
    parts = urlsplit(url)
    if parts.scheme not in {"redis", "rediss"} or "@" in parts.netloc:
        return url
    return urlunsplit((parts.scheme, f":{quote(password, safe='')}@{parts.netloc}", parts.path, parts.query, parts.fragment))


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay DLQ messages into news_raw_queue")
    parser.add_argument("--execute", action="store_true", help="Actually replay (default is dry-run)")
    parser.add_argument("--error-code", default=None, help="Only replay messages with this error_code")
    parser.add_argument("--batch-size", type=int, default=100, help="XRANGE batch size (default 100)")
    args = parser.parse_args()

    _load_env()

    queue_url = _queue_url()
    news_raw_queue = os.environ.get("NEWS_RAW_QUEUE", "news_raw_queue")
    dlq_stream = os.environ.get("FAILED_MESSAGES_DLQ", "failed_messages_dlq")

    client = redis.from_url(queue_url, decode_responses=True)
    client.ping()

    cursor = "-"
    matched = 0
    replayed = 0
    skipped = 0

    while True:
        entries = client.xrange(dlq_stream, min=cursor, count=args.batch_size)
        if not entries:
            break

        for message_id, fields in entries:
            cursor = f"({message_id}"  # exclusive lower bound for next batch

            error_code = fields.get("error_code", "")
            if args.error_code and error_code != args.error_code:
                skipped += 1
                continue

            matched += 1
            payload_json = fields.get("payload_json", "{}")
            try:
                payload = json.loads(payload_json)
            except json.JSONDecodeError:
                print(f"  SKIP {message_id}: invalid payload_json")
                skipped += 1
                continue

            occurred_at = payload.get("occurred_at") or payload.get("published_at") or ""

            stream_fields = {
                "event_id": fields.get("event_id", ""),
                "event_type": fields.get("event_type", ""),
                "event_version": "1.0",
                "occurred_at": occurred_at,
                "producer": "dlq_replay",
                "dedupe_key": fields.get("dedupe_key", ""),
                "payload_json": payload_json,
            }

            if args.execute:
                client.xadd(news_raw_queue, stream_fields)
                client.xdel(dlq_stream, message_id)
                replayed += 1
            else:
                event_id = fields.get("event_id", "?")
                print(f"  [dry-run] would replay {message_id} (event_id={event_id}, error={error_code})")
                replayed += 1

    mode = "EXECUTE" if args.execute else "DRY-RUN"
    print(f"\n{mode}: matched={matched}, replayed={replayed}, skipped={skipped}")
    if not args.execute and matched > 0:
        print("Re-run with --execute to replay for real.")


if __name__ == "__main__":
    main()
