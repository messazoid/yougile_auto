#!/usr/bin/env python3
import argparse
import asyncio
from pathlib import Path

import receiver
from job_store import PipelineStore


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Queue previously ignored YouGile WAV attachments without running ACRCloud"
    )
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--chat-id", required=True)
    parser.add_argument("--page-size", type=int, default=20)
    args = parser.parse_args(argv)
    store = PipelineStore(Path(receiver.PIPELINE_DB))
    store.initialize()
    asyncio.run(receiver.backfill_yougile_wav_history(
        store, args.task_id, args.chat_id, args.page_size
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
