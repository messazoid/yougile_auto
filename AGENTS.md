# Codex instructions — music-verifier

## Scope

These instructions apply to `/opt/music-verifier`.

The project is a stateful pipeline:

YouGile
-> source acquisition
-> FFmpeg/WAV
-> ACRCloud recognition
-> candidate verification
-> CIS-Net verification
-> result publication to YouGile

Not all later stages are implemented yet.

## Sources of truth

Prefer current code and runtime configuration over historical documentation.

Use documentation selectively:

- `README.md` — project map, current architecture, components and normal checks.
- `docs/RECOVERY.md` — disaster recovery only.

Do not read historical `.md`, `.diff`, backups, archives or old run artifacts
unless the current task explicitly requires historical investigation.

If documentation conflicts with code or current runtime state, inspect only the
minimum necessary scope and report the conflict.

## Critical invariants

- `data/queue/pipeline.sqlite3` is persistent application state, not a cache.
- Do not delete, replace, recreate or reset the production SQLite database unless explicitly requested.
- Preserve durable source/message deduplication.
- Do not intentionally process the same source twice unless explicitly requested.
- Preserve YouGile rate limiting and `Retry-After` handling.
- Preserve ACRCloud request accounting, retry protection and completed-window deduplication.
- ACRCloud candidates are not verified final results.
- `complete_candidates` does not mean that a composition is confirmed.
- Do not change database schema or stage semantics unless the task requires it.
- Do not expose credentials, API keys, cookies, tokens or secret-bearing URLs.
- Do not make real paid ACRCloud requests unless explicitly requested.
- Do not perform live CIS-Net actions unless explicitly requested.
- Do not restart, stop or enable production services unless explicitly requested.

## Context efficiency

Use the minimum context necessary to complete the task.

- Inspect only files directly relevant to the current task.
- Start with targeted searches; do not scan the whole repository by default.
- Do not perform repository-wide or server-wide audits unless explicitly requested.
- Do not repeat an audit or investigation whose result is already established.
- Prefer targeted `rg`, `sed`, `tail`, SQL and test commands.
- Never dump entire databases, directories, large logs, JSON/JSONL files or run artifacts.
- Normally inspect no more than about 100 relevant log lines at once.
- Filter logs before reading more.
- Do not print full diffs unless explicitly requested.
- Do not print unchanged code.
- Do not investigate or fix unrelated issues.
- If an unrelated issue is noticed, mention it briefly without expanding scope.
- Prefer minimal patches over broad refactors.
- Run targeted tests first.
- Run broader tests only when the change reasonably requires them.

## Task discipline

For each task:

1. identify the smallest relevant component;
2. inspect only that component and its direct dependencies;
3. make the minimum required change;
4. run targeted checks;
5. stop when the requested task is complete.

Do not turn a narrow task into general cleanup, refactoring, security hardening,
documentation rewriting or architecture work unless explicitly requested.

If the requested change is risky or architecture-sensitive, explain the issue
briefly before changing it. Otherwise, implement directly without a long
preliminary audit.

## Final response

Keep the final response concise.

Report only:

1. result;
2. changed files;
3. checks/tests performed;
4. unresolved problem, if any.

Do not repeat the investigation, unchanged behavior or full diff.
