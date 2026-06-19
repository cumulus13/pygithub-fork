# github-forker

[![PyPI version](https://badge.fury.io/py/pygithub-fork.svg)](https://pypi.org/project/pygithub-fork/)
[![Python](https://img.shields.io/pypi/pyversions/pygithub-fork)](https://pypi.org/project/pygithub-fork/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Production-ready GitHub repository forking built on [PyGithub](https://github.com/PyGithub/PyGithub).

A bare `repo.create_fork()` call returns immediately but the fork is not actually usable yet — GitHub builds the copy asynchronously in the background. `github-forker` handles everything you need for real-world use:

- **Idempotency** — detects pre-existing forks so re-runs never crash
- **Retry + exponential backoff with jitter** — survives 5xx, timeouts, rate limits, and GitHub's secondary ("abuse") rate limit
- **Fork-readiness polling** — waits until the fork is actually populated before returning
- **Thread pool** — `fork_many()` runs up to N forks concurrently
- **Background / fire-and-forget** — `fork_async()` returns a `ForkJob` you can query or wait on from any thread
- **Streaming generator** — `fork_iter()` yields results as each fork completes
- **Post-fork upstream remote** — runs `git remote add upstream <url>` in your local clone
- **Post-fork webhook** — registers GitHub push/fork (or any) events on the new fork

---

## Installation

```bash
pip install github-forker
```

Requires Python ≥ 3.9 and PyGithub ≥ 1.55.

---

## Quick start

```python
from github import Github
from pygithub_fork import GitHubForker

gh = Github("ghp_your_token")
forker = GitHubForker(gh)

result = forker.fork("octocat/Hello-World")
print(result.status)      # ForkStatus.READY
print(result.clone_url)   # https://github.com/you/Hello-World.git
```

---

## Usage

### 1. `fork()` — synchronous, blocking

Forks one repo and blocks until it is confirmed ready on GitHub's side.

```python
from pygithub_fork import GitHubForker, ForkerConfig

forker = GitHubForker(gh)

result = forker.fork("octocat/Hello-World")
# result.status  → ForkStatus.READY
# result.fork    → github.Repository.Repository
# result.clone_url
# result.ssh_url
# result.already_existed  → False (or True on re-run)
# result.elapsed_seconds
```

Fork into an **organization** with a custom **name**:

```python
result = forker.fork(
    "octocat/Hello-World",
    organization="my-org",
    name="hello-world-internal",
    default_branch_only=True,
)
```

---

### 2. `fork_async()` — fire-and-forget, separated process

Submit a fork to the background thread pool and **return immediately**.  
Query the `ForkJob` handle from anywhere — the caller is never blocked.

```python
job = forker.fork_async("octocat/Hello-World")

# --- do other things in the meantime ---

# Non-blocking status check:
print(job.done)          # True / False
print(job.status)        # ForkStatus.PENDING | CREATED | READY | FAILED …

# Access result without blocking (returns None if still running):
result = job.result      # ForkResult | None

# Block when you actually need the answer:
result = job.wait()      # blocks until done, returns ForkResult
result = job.wait(timeout=30)  # TimeoutError after 30s if not done
```

This is the answer to **"fork then get status in a separate process"** — submit with `fork_async()` and poll `job.done` / `job.status` from any thread at any time without blocking.

**Concrete pattern — submit all, poll separately:**

```python
jobs = [forker.fork_async(repo) for repo in ["owner/a", "owner/b", "owner/c"]]

# ... do other work ...

# Later, collect all results:
results = [job.wait() for job in jobs]

# Or poll individually without waiting:
for job in jobs:
    if job.done:
        print(job.source_full_name, job.status)
    else:
        print(job.source_full_name, "still running")
```

---

### 3. `fork_many()` — bulk fork with thread pool

Fork a list in parallel (default) or sequentially:

```python
results = forker.fork_many([
    "owner/repo-a",
    "owner/repo-b",
    "owner/repo-c",
])

for r in results:
    print(r.source_full_name, r.status, r.succeeded)
```

**Parallel vs sequential:**

```python
# Parallel (default) — up to config.pool_workers concurrent forks
results = forker.fork_many(repos, parallel=True)

# Sequential — one at a time, guaranteed order, easier to debug
results = forker.fork_many(repos, parallel=False)
```

**Per-item control with `ForkRequest`:**

```python
from pygithub_fork import ForkRequest

requests = [
    ForkRequest("owner/public-repo", organization="my-org"),
    ForkRequest("owner/private-repo", name="private-fork", default_branch_only=True),
    ForkRequest("owner/widget",       organization="other-org", register_webhook=True,
                webhook_url="https://ci.example.com/hooks/github"),
]
results = forker.fork_many(requests)
```

**Stop on first failure:**

```python
results = forker.fork_many(repos, stop_on_error=True)
```

---

### 4. `fork_iter()` — streaming results (completion order)

Yields each `ForkResult` as soon as it finishes — useful for large batches
where you want to start processing early:

```python
for result in forker.fork_iter(["owner/a", "owner/b", "owner/c"]):
    # results arrive in completion order, not submission order
    print(result.source_full_name, result.status)
```

---

### 5. Post-fork: upstream remote

After forking, automatically run `git remote add upstream <source_url>` in a
local clone:

```python
from pygithub_fork import ForkerConfig

config = ForkerConfig(
    add_upstream_remote=True,
    local_clone_path="/path/to/your/local/clone",
)
forker = GitHubForker(gh, config)
result = forker.fork("octocat/Hello-World")

print(result.upstream_remote_added)  # True
# Now: git remote -v shows `upstream → https://github.com/octocat/Hello-World.git`
```

Override per-call:

```python
result = forker.fork(
    "octocat/Hello-World",
    add_upstream_remote=True,
    local_path="/path/to/clone",
)
```

---

### 6. Post-fork: webhook registration

Register a GitHub webhook on the new fork immediately after creation:

```python
config = ForkerConfig(
    register_webhook=True,
    webhook_url="https://ci.example.com/hooks/github",
    webhook_events=["push", "pull_request", "fork"],
    webhook_secret="s3cr3t",
)
forker = GitHubForker(gh, config)
result = forker.fork("octocat/Hello-World")

print(result.webhook_id)   # GitHub hook ID
```

Override per-call:

```python
result = forker.fork(
    "octocat/Hello-World",
    register_webhook=True,
    webhook_url="https://ci.example.com/hooks/github",
    webhook_events=["push"],
)
```

---

### 7. Advanced configuration

```python
from pygithub_fork import ForkerConfig

config = ForkerConfig(
    # Retry
    max_retries=8,
    base_backoff_seconds=2.0,
    max_backoff_seconds=120.0,

    # Readiness polling
    wait_for_ready=True,
    ready_timeout_seconds=120.0,
    ready_poll_interval_seconds=3.0,

    # Thread pool size (keep ≤ 4 to avoid GitHub secondary rate limits)
    pool_workers=4,

    # Post-fork actions
    add_upstream_remote=True,
    local_clone_path="/repos/my-clone",
    register_webhook=True,
    webhook_url="https://ci.example.com/hooks/github",
    webhook_events=["push", "fork"],
    webhook_secret="s3cr3t",

    # Callbacks
    on_retry=lambda attempt, exc, sleep: print(f"retry {attempt}: {exc}"),
    on_fork_done=lambda result: print(f"done: {result.source_full_name}"),
)

forker = GitHubForker(gh, config)
```

---

### 8. Context manager (pool cleanup)

```python
with GitHubForker(gh) as forker:
    results = forker.fork_many(repos)
# Thread pool is shut down cleanly here
```

---

## ForkResult fields

| Field | Type | Description |
|---|---|---|
| `source_full_name` | `str` | `"owner/repo"` of the source |
| `fork` | `Repository \| None` | The forked repo object (PyGithub) |
| `status` | `ForkStatus` | `READY`, `CREATED`, `ALREADY_EXISTED`, `TIMED_OUT_WAITING`, `FAILED` |
| `already_existed` | `bool` | True if the fork pre-existed |
| `attempts` | `int` | How many API attempts were made |
| `elapsed_seconds` | `float` | Wall time from call to return |
| `clone_url` | `str \| None` | HTTPS clone URL of the fork |
| `ssh_url` | `str \| None` | SSH URL of the fork |
| `upstream_remote_added` | `bool` | Whether `git remote add upstream` ran |
| `webhook_id` | `int \| None` | GitHub hook ID if registered |
| `error` | `Exception \| None` | Set on failure; None on success |
| `succeeded` | `bool` | `fork is not None and error is None` |

---

## ForkJob fields / methods (fork_async)

| | Description |
|---|---|
| `.done` | `bool` — non-blocking check |
| `.status` | `ForkStatus` — `PENDING` while running, real status when done |
| `.result` | `ForkResult \| None` — non-blocking; `None` if still running |
| `.wait(timeout=None)` | Block and return `ForkResult`; raises `ForkError` on failure |
| `.source_full_name` | The `"owner/repo"` string passed in |

---

## Exception hierarchy

```
ForkError
├── ForkTimeoutError       # readiness timeout
├── ForkPermissionError    # 401/403 (distinct from secondary rate limit)
├── RepositoryNotFoundError
├── WebhookError           # webhook registration failed
└── UpstreamRemoteError    # git remote add upstream failed
```

---

## License

MIT © [Hadi Cahyadi](https://github.com/cumulus13)

## 👤 Author
        
[Hadi Cahyadi](mailto:cumulus13@gmail.com)
    

[![Buy Me a Coffee](https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png)](https://www.buymeacoffee.com/cumulus13)

[![Donate via Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/cumulus13)
 
[Support me on Patreon](https://www.patreon.com/cumulus13)
