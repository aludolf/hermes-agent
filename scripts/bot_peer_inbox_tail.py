#!/usr/bin/env python3
"""Read bot-peer inbox JSONL written by the gateway loop-safety guard.

Usage:
    python3 bot_peer_inbox_tail.py <chat_id> [--limit N] [--since-ts EPOCH]

Examples:
    # Last 5 messages from HDbotDebug group
    python3 bot_peer_inbox_tail.py -1003992792803 --limit 5

    # Messages since a specific timestamp (capture BEFORE sending)
    SEND_TS=$(date +%s.%N)
    # ... send message ...
    python3 bot_peer_inbox_tail.py -1003992792803 --since-ts $SEND_TS

    # All messages for a chat (no --since-ts)
    python3 bot_peer_inbox_tail.py -1003992792803

Note on --since-ts race:
    Always capture the timestamp BEFORE sending the message to the target
    bot, not after.  The target bot's reply can arrive fractions of a
    second before the sender's clock reads, causing the filter to miss
    valid replies.  When --since-ts is omitted, all rows for the chat_id
    are returned (subject to --limit).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read bot-peer inbox JSONL from the gateway loop-safety guard."
    )
    parser.add_argument("chat_id", help="Chat ID to filter by (e.g. -1003992792803)")
    parser.add_argument(
        "--limit", type=int, default=10,
        help="Max number of recent entries to return (default: 10)",
    )
    parser.add_argument(
        "--since-ts", type=float, default=None, dest="since_ts",
        help="Only return entries with ts >= this epoch value. "
             "Capture BEFORE sending to avoid the race condition.",
    )
    parser.add_argument(
        "--inbox", type=str, default=None,
        help="Path to bot_peer_inbox.jsonl (default: ~/.hermes/bot_peer_inbox.jsonl "
             "or $HERMES_HOME/bot_peer_inbox.jsonl)",
    )
    args = parser.parse_args()

    # Resolve inbox path
    if args.inbox:
        inbox = Path(args.inbox)
    else:
        home = Path(os.getenv("HERMES_HOME", Path.home() / ".hermes"))
        inbox = home / "bot_peer_inbox.jsonl"

    if not inbox.exists():
        print("[]")
        return

    chat_id = str(args.chat_id).strip()
    results: list[dict] = []

    with inbox.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            if str(rec.get("chat_id", "")).strip() != chat_id:
                continue

            if args.since_ts is not None and rec.get("ts", 0) < args.since_ts:
                continue

            results.append(rec)

    # Return newest entries up to --limit
    if args.limit > 0:
        results = results[-args.limit:]

    print(json.dumps(results, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
