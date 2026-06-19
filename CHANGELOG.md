# Changelog — github-forker

> **Note:** This package was originally named `pygithub-fork` but was renamed
> to `github-forker` before initial release because PyPI's similarity check
> flagged `pygithub-fork` as too similar to the existing `PyGithub` package
> (PyPI normalizes `pygithub-fork` → `pygithubfork` which is too close to
> `pygithub`). The Python module name `pygithub_fork` is unchanged.


All notable changes to this project will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
This project uses [Semantic Versioning](https://semver.org/).

---

## [1.0.1] — 2026-06-19

### Added

- `GitHubForker.fork()` — synchronous fork with idempotency, retry/backoff, and readiness polling
- `GitHubForker.fork_async()` — fire-and-forget fork returning a `ForkJob` handle
- `ForkJob` — non-blocking `.done` / `.status` / `.result` + blocking `.wait(timeout)`
- `GitHubForker.fork_many()` — bulk fork via `ThreadPoolExecutor` (parallel or sequential)
- `GitHubForker.fork_iter()` — streaming generator yielding results in completion order
- `ForkRequest` — per-item declarative config for `fork_many`/`fork_iter` batches
- `ForkerConfig` — centralized, documented configuration dataclass
- Post-fork `git remote add upstream` support (`add_upstream_remote`, `local_clone_path`)
- Post-fork GitHub webhook registration (`register_webhook`, `webhook_url`, `webhook_events`, `webhook_secret`)
- Idempotent webhook de-duplication (checks existing hooks before creating)
- Idempotent upstream remote (checks before `git remote add`)
- Secondary rate limit (403 "abuse") distinguished from hard 403 permission errors
- PyGithub cross-version compatibility (`RateLimitExceededException` vs `RateLimitExceededError`)
- Full exception hierarchy: `ForkError`, `ForkTimeoutError`, `ForkPermissionError`, `RepositoryNotFoundError`, `WebhookError`, `UpstreamRemoteError`
- `on_retry` and `on_fork_done` callbacks in `ForkerConfig`
- Context manager support (`with GitHubForker(gh) as forker:`) for clean pool shutdown
- MIT license
- Full README with API reference and usage examples
- GitHub Actions CI workflow (lint + test on Python 3.9–3.12)
- GitHub Actions publish workflow (PyPI on tag push)
