"""
pygithub-fork
~~~~~~~~~~~~~
Production-ready GitHub repository forking built on PyGithub.

Quick start::

    from github import Github
    from pygithub_fork import GitHubForker, ForkerConfig

    gh = Github("ghp_your_token")
    forker = GitHubForker(gh)

    # Synchronous — blocks until fork is ready
    result = forker.fork("octocat/Hello-World")
    print(result.status, result.clone_url)

    # Fire-and-forget — check / wait from anywhere
    job = forker.fork_async("PyGithub/PyGithub")
    print(job.done, job.status)          # non-blocking
    result = job.wait()                  # block when you need the answer

    # Bulk fork with thread pool
    results = forker.fork_many(["owner/a", "owner/b", "owner/c"])
"""

from .forker import ForkJob, GitHubForker
from .models import ForkRequest, ForkResult, ForkStatus, ForkerConfig
from .exceptions import (
    ForkError,
    ForkPermissionError,
    ForkTimeoutError,
    RepositoryNotFoundError,
    UpstreamRemoteError,
    WebhookError,
)

__version__ = "1.0.0"
__author__ = "Hadi Cahyadi"
__email__ = "cumulus13@gmail.com"

__all__ = [
    # Main class + async job handle
    "GitHubForker",
    "ForkJob",
    # Data models
    "ForkRequest",
    "ForkResult",
    "ForkStatus",
    "ForkerConfig",
    # Exceptions
    "ForkError",
    "ForkPermissionError",
    "ForkTimeoutError",
    "RepositoryNotFoundError",
    "UpstreamRemoteError",
    "WebhookError",
]
