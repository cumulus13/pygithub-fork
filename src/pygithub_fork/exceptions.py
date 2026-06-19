"""
pygithub_fork.exceptions
~~~~~~~~~~~~~~~~~~~~~~~~
All custom exceptions raised by pygithub-fork.
"""
from __future__ import annotations


class ForkError(Exception):
    """Base class for all pygithub-fork errors."""


class ForkTimeoutError(ForkError):
    """Fork was created but did not become ready within the allotted time."""

    def __init__(self, repo_full_name: str, waited_seconds: float) -> None:
        self.repo_full_name = repo_full_name
        self.waited_seconds = waited_seconds
        super().__init__(
            f"Fork of '{repo_full_name}' did not become ready after "
            f"{waited_seconds:.1f}s. It may still finish on GitHub's side — "
            f"increase ready_timeout_seconds or use wait_for_ready=False."
        )


class ForkPermissionError(ForkError):
    """The authenticated token lacks rights to fork into the target."""


class RepositoryNotFoundError(ForkError):
    """The source repository does not exist or is inaccessible."""


class WebhookError(ForkError):
    """Webhook registration or removal failed."""


class UpstreamRemoteError(ForkError):
    """git remote add upstream step failed."""
