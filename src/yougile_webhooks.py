#!/usr/bin/env python3
"""Inspect or create the two YouGile subscriptions used by the receiver."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from urllib.parse import quote, urlparse

from receiver import (
    WEBHOOK_SECRET, YOUGILE_ALLOWED_COLUMN_IDS, yougile_get_json, yougile_post_json,
)


EVENTS = ("task-moved", "chat_message-created")


def callback_url(public_base_url: str, secret: str) -> str:
    parsed = urlparse(public_base_url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("Public webhook base URL must be HTTPS without credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("Public webhook base URL must not contain a query or fragment")
    if not secret or secret == "change-me-before-start":
        raise ValueError("WEBHOOK_SECRET is not configured")
    return public_base_url.rstrip("/") + "/webhooks/yougile/" + quote(secret, safe="")


def location_filters(column_ids: set[str]) -> list[dict]:
    if not column_ids:
        raise ValueError("At least one allowed YouGile column is required")
    return [{"name": "location", "value": sorted(column_ids)}]


def _location_values(subscription: dict) -> list[str]:
    for item in subscription.get("filters") or []:
        if isinstance(item, dict) and item.get("name") == "location":
            value = item.get("value")
            if isinstance(value, str):
                return [value]
            if isinstance(value, list):
                return sorted(str(entry) for entry in value)
    return []


def matches(subscription: dict, event: str, url: str, column_ids: set[str]) -> bool:
    return bool(
        not subscription.get("deleted")
        and not subscription.get("disabled")
        and subscription.get("event") == event
        and subscription.get("url") == url
        and _location_values(subscription) == sorted(column_ids)
    )


def _rows(payload) -> list[dict]:
    if isinstance(payload, dict):
        payload = payload.get("content", [])
    return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []


async def reconcile(public_base_url: str, apply: bool) -> dict:
    url = callback_url(public_base_url, WEBHOOK_SECRET)
    columns = set(YOUGILE_ALLOWED_COLUMN_IDS)
    filters = location_filters(columns)
    existing = _rows(await yougile_get_json("/webhooks", request_kind="webhook_list"))
    results = []
    for event in EVENTS:
        found = next((item for item in existing if matches(item, event, url, columns)), None)
        if found is not None:
            results.append({"event": event, "state": "active", "id": str(found.get("id") or "")})
            continue
        if not apply:
            results.append({"event": event, "state": "missing", "id": ""})
            continue
        created = await yougile_post_json(
            "/webhooks", {"url": url, "event": event, "filters": filters},
            request_kind="webhook_create",
        )
        identifier = str(created.get("id") or "") if isinstance(created, dict) else ""
        results.append({"event": event, "state": "created", "id": identifier})
    return {"apply": apply, "subscriptions": results}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect or create music-verifier YouGile webhooks")
    parser.add_argument(
        "--public-base-url", default=os.getenv("YOUGILE_WEBHOOK_PUBLIC_BASE", ""),
        help="public HTTPS origin routed to the receiver",
    )
    parser.add_argument("--apply", action="store_true", help="create missing subscriptions")
    args = parser.parse_args(argv)
    if not args.public_base_url:
        parser.error("--public-base-url or YOUGILE_WEBHOOK_PUBLIC_BASE is required")
    try:
        result = asyncio.run(reconcile(args.public_base_url, args.apply))
    except Exception as error:
        print(json.dumps({"error": type(error).__name__}, separators=(",", ":")))
        return 2
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
