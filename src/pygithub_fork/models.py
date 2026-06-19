"""
pygithub_fork.models
~~~~~~~~~~~~~~~~~~~~
Dataclasses and enums for fork operations.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional, Union

from github.Repository import Repository
from github.Organization import Organization


class ForkStatus(Enum):
    PENDING          = "pending"           # Submitted but not yet confirmed
    CREATED          = "created"           # API call succeeded; readiness unconfirmed
    ALREADY_EXISTED  = "already_existed"   # Idempotent re-run; fork pre-existed
    READY            = "ready"             # Fork is fully populated and usable
    TIMED_OUT_WAITING = "timed_out_waiting"  # Created but readiness unconfirmed within timeout
    FAILED           = "failed"            # Unrecoverable error


@dataclass
class ForkRequest:
    """Declarative description of a single fork operation for use with fork_many / pool."""
    source: Union[str, Repository]
    organization: Optional[Union[str, Organization]] = None
    name: Optional[str] = None
    default_branch_only: Optional[bool] = None
    # Per-request post-fork actions (override global config when set)
    add_upstream_remote: Optional[bool] = None   # git remote add upstream <clone_url>
    local_path: Optional[str] = None             # path to local clone for remote setup
    register_webhook: Optional[bool] = None      # register push/fork events webhook
    webhook_url: Optional[str] = None            # target URL for webhook
    webhook_events: Optional[list[str]] = None   # e.g. ["push", "fork"]


@dataclass
class ForkResult:
    """Outcome of a single fork operation."""
    source_full_name: str
    fork: Optional[Repository]
    status: ForkStatus
    already_existed: bool
    attempts: int = 1
    elapsed_seconds: float = 0.0
    error: Optional[Exception] = None
    upstream_remote_added: bool = False
    webhook_id: Optional[int] = None           # GitHub hook ID if webhook was registered

    @property
    def succeeded(self) -> bool:
        return self.fork is not None and self.error is None

    @property
    def clone_url(self) -> Optional[str]:
        return self.fork.clone_url if self.fork else None

    @property
    def ssh_url(self) -> Optional[str]:
        return self.fork.ssh_url if self.fork else None

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"ForkResult(source={self.source_full_name!r}, "
            f"status={self.status.value}, "
            f"fork={self.fork.full_name if self.fork else None!r}, "
            f"elapsed={self.elapsed_seconds:.2f}s)"
        )


@dataclass
class ForkerConfig:
    """
    Tunable behavior for GitHubForker.  All fields have sensible production
    defaults; override only what you need.
    """
    # ---- Retry / rate-limit ----
    max_retries: int = 5
    base_backoff_seconds: float = 1.5
    max_backoff_seconds: float = 60.0

    # ---- Fork readiness polling ----
    wait_for_ready: bool = True
    ready_timeout_seconds: float = 90.0
    ready_poll_interval_seconds: float = 3.0

    # ---- Thread pool (fork_many / fork_async) ----
    pool_workers: int = 4
    """Max concurrent forks.  GitHub secondary rate limits kick in when you
    fork too many repos in rapid succession; keep this ≤ 4 for safety."""

    # ---- Post-fork: upstream remote ----
    add_upstream_remote: bool = False
    """If True, runs `git remote add upstream <source_clone_url>` in
    local_clone_path after a successful fork."""
    local_clone_path: Optional[str] = None
    """Absolute path of the local clone where the upstream remote is added.
    Only used when add_upstream_remote=True."""

    # ---- Post-fork: webhook ----
    register_webhook: bool = False
    """If True, registers a webhook on the freshly created fork."""
    webhook_url: Optional[str] = None
    """Payload URL for the webhook."""
    webhook_events: list[str] = field(default_factory=lambda: ["push", "fork"])
    webhook_content_type: str = "json"
    webhook_secret: Optional[str] = None
    webhook_insecure_ssl: bool = False

    # ---- Callbacks ----
    on_retry: Optional[Callable[[int, Exception, float], None]] = None
    """Called as on_retry(attempt, exc, sleep_seconds) before each retry sleep."""
    on_fork_done: Optional[Callable[["ForkResult"], None]] = None
    """Called after each successful (or failed) fork when using fork_many/pool."""
