#!/usr/bin/env python3
import argparse
import asyncio

import receiver
from job_store import PipelineStore


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Mark existing YouGile chat history as viewed without creating jobs"
    )
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--chat-id", required=True)
    parser.add_argument("--page-size", type=int, default=20)
    args = parser.parse_args(argv)
    store = PipelineStore(receiver.PIPELINE_DB)
    store.initialize()
    asyncio.run(receiver.baseline_yougile_chat(
        store, args.task_id, args.chat_id, args.page_size
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
