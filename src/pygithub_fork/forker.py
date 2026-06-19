"""
pygithub_fork.forker
~~~~~~~~~~~~~~~~~~~~
Core GitHubForker class.

Key capabilities on top of a bare PyGithub create_fork() call
--------------------------------------------------------------
1. **Idempotency** — detects pre-existing forks so re-runs are safe.
2. **Retry + exponential backoff with jitter** — handles 5xx, network
   timeouts, and GitHub secondary rate limits (403 "abuse" / 429).
3. **Fork-readiness polling** — GitHub's fork endpoint is asynchronous;
   we poll until the fork is actually populated, not just created.
4. **Thread pool** — fork_many() can run up to `pool_workers` forks
   concurrently via ThreadPoolExecutor.
5. **Background / fire-and-forget** — fork_async() returns a ForkJob
   whose .result() / .status / .done can be queried from another thread
   without blocking the caller.
6. **Post-fork upstream remote** — optionally runs
   `git remote add upstream <url>` in a local clone.
7. **Post-fork webhook** — optionally registers a GitHub webhook on the
   freshly created fork.
"""
from __future__ import annotations

import logging
import os
import random
import subprocess
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Iterable, Iterator, Optional, Union

from github import Github, GithubException
from github.Repository import Repository
from github.Organization import Organization
from github.GithubObject import NotSet

try:
    from github import RateLimitExceededError as _RateLimitExc
except ImportError:
    from github import RateLimitExceededException as _RateLimitExc  # type: ignore[attr-defined]

from .exceptions import (
    ForkError,
    ForkPermissionError,
    ForkTimeoutError,
    RepositoryNotFoundError,
    UpstreamRemoteError,
    WebhookError,
)
from .models import ForkRequest, ForkResult, ForkStatus, ForkerConfig

logger = logging.getLogger(__name__)


# ============================================================================
# ForkJob — a thin wrapper around concurrent.futures.Future
# ============================================================================

class ForkJob:
    """
    Handle returned by fork_async().

    Lets you fire off a fork in the background and query it later from a
    completely separate part of your code — or a different thread.

    Example::

        job = forker.fork_async("octocat/Hello-World")
        # ... do other things ...
        result = job.wait()          # blocks until done
        print(result.status)

        # Or poll without blocking:
        if job.done:
            print(job.result)        # ForkResult, never raises
        else:
            print("still running, status =", job.status)
    """

    def __init__(self, future: Future, source_full_name: str) -> None:
        self._future = future
        self.source_full_name = source_full_name

    # ---- non-blocking accessors ----------------------------------------- #

    @property
    def done(self) -> bool:
        """True if the fork operation has finished (success *or* failure)."""
        return self._future.done()

    @property
    def status(self) -> ForkStatus:
        """
        Best-effort current status without blocking.
        Returns PENDING while running, then the real status once done.
        """
        if not self._future.done():
            return ForkStatus.PENDING
        exc = self._future.exception()
        if exc is not None:
            return ForkStatus.FAILED
        return self._future.result().status

    @property
    def result(self) -> Optional[ForkResult]:
        """
        The ForkResult if finished, None if still running.
        Never raises — check .error on the returned ForkResult instead.
        """
        if not self._future.done():
            return None
        exc = self._future.exception()
        if exc is not None:
            return ForkResult(
                source_full_name=self.source_full_name,
                fork=None,
                status=ForkStatus.FAILED,
                already_existed=False,
                error=exc,
            )
        return self._future.result()

    # ---- blocking accessor ----------------------------------------------- #

    def wait(self, timeout: Optional[float] = None) -> ForkResult:
        """
        Block until the fork completes and return the ForkResult.

        Args:
            timeout: seconds to wait; None = wait forever.

        Raises:
            concurrent.futures.TimeoutError if timeout elapses before done.
            ForkError (or subclass) on fork failure.
        """
        return self._future.result(timeout=timeout)

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"ForkJob(source={self.source_full_name!r}, "
            f"status={self.status.value}, done={self.done})"
        )


# ============================================================================
# GitHubForker
# ============================================================================

class GitHubForker:
    """
    Production-ready GitHub repository forker.

    Parameters
    ----------
    client : github.Github
        An authenticated PyGithub client (token, app auth, etc.).
    config : ForkerConfig, optional
        Tunable settings; ForkerConfig() defaults are sane for most use cases.

    Quick start::

        from github import Github
        from pygithub_fork import GitHubForker

        gh = Github("ghp_xxx")
        forker = GitHubForker(gh)

        # --- synchronous, blocks until fork is ready ---
        result = forker.fork("octocat/Hello-World")
        print(result.status, result.clone_url)

        # --- fire-and-forget, check later ---
        job = forker.fork_async("PyGithub/PyGithub")
        # ... other work ...
        result = job.wait()

        # --- bulk fork with thread pool ---
        results = forker.fork_many(["owner/a", "owner/b", "owner/c"])
    """

    def __init__(self, client: Github, config: Optional[ForkerConfig] = None) -> None:
        if not isinstance(client, Github):
            raise TypeError("client must be an authenticated github.Github instance")
        self._gh = client
        self.config = config or ForkerConfig()
        # Shared pool — created lazily, lives for the lifetime of this forker.
        self._pool: Optional[ThreadPoolExecutor] = None

    # ------------------------------------------------------------------ #
    # Context-manager support (shuts down the thread pool cleanly)
    # ------------------------------------------------------------------ #

    def __enter__(self) -> "GitHubForker":
        return self

    def __exit__(self, *_: object) -> None:
        self.shutdown()

    def shutdown(self, wait: bool = True) -> None:
        """Shut down the internal thread pool.  Safe to call multiple times."""
        if self._pool is not None:
            self._pool.shutdown(wait=wait)
            self._pool = None

    @property
    def _executor(self) -> ThreadPoolExecutor:
        if self._pool is None:
            self._pool = ThreadPoolExecutor(
                max_workers=self.config.pool_workers,
                thread_name_prefix="pygithub_fork",
            )
        return self._pool

    # ================================================================== #
    # Public API
    # ================================================================== #

    # ---- 1. fork() — synchronous, blocking ----------------------------- #

    def fork(
        self,
        source: Union[str, Repository],
        *,
        organization: Optional[Union[str, Organization]] = None,
        name: Optional[str] = None,
        default_branch_only: Optional[bool] = None,
        # Per-call overrides (use config for global defaults)
        add_upstream_remote: Optional[bool] = None,
        local_path: Optional[str] = None,
        register_webhook: Optional[bool] = None,
        webhook_url: Optional[str] = None,
        webhook_events: Optional[list[str]] = None,
    ) -> ForkResult:
        """
        Fork a single repository synchronously.

        Blocks until the fork is confirmed ready (respecting
        config.wait_for_ready and config.ready_timeout_seconds), then
        optionally adds an upstream remote and/or registers a webhook.

        Parameters
        ----------
        source:
            ``"owner/repo"`` string **or** a PyGithub Repository object.
        organization:
            Org login or Organization object to fork into.
            Defaults to the authenticated user's personal account.
        name:
            Custom name for the resulting fork (avoids collision when forking
            the same upstream into multiple org targets).
        default_branch_only:
            When True, GitHub copies only the default branch.
        add_upstream_remote:
            Override config.add_upstream_remote for this call.
        local_path:
            Local clone path for ``git remote add upstream``.
            Overrides config.local_clone_path.
        register_webhook:
            Override config.register_webhook for this call.
        webhook_url:
            Webhook payload URL (overrides config.webhook_url).
        webhook_events:
            Events to subscribe to (overrides config.webhook_events).

        Returns
        -------
        ForkResult

        Raises
        ------
        RepositoryNotFoundError
            Source repo not found or token can't see it.
        ForkPermissionError
            Token lacks fork permission (distinct from secondary rate limit).
        ForkTimeoutError
            Fork created but not confirmed ready within timeout (only when
            wait_for_ready=True).
        ForkError
            Any other unrecoverable error.
        """
        start = time.monotonic()
        repo = self._resolve_source(source)
        full_name = repo.full_name

        existing = self._find_existing_fork(repo, organization)
        if existing is not None:
            logger.info(
                "Fork of '%s' already exists at '%s'; reusing.",
                full_name, existing.full_name,
            )
            result = ForkResult(
                source_full_name=full_name,
                fork=existing,
                status=ForkStatus.ALREADY_EXISTED,
                already_existed=True,
                elapsed_seconds=time.monotonic() - start,
            )
        else:
            fork_obj, attempts = self._create_with_retry(
                repo,
                organization=organization,
                name=name,
                default_branch_only=default_branch_only,
            )
            result = ForkResult(
                source_full_name=full_name,
                fork=fork_obj,
                status=ForkStatus.CREATED,
                already_existed=False,
                attempts=attempts,
                elapsed_seconds=time.monotonic() - start,
            )

        if self.config.wait_for_ready and result.fork is not None:
            self._await_ready(result, start)

        # ---- post-fork actions ----------------------------------------- #
        if result.fork is not None:
            do_upstream = (
                add_upstream_remote
                if add_upstream_remote is not None
                else self.config.add_upstream_remote
            )
            effective_path = local_path or self.config.local_clone_path
            if do_upstream:
                self._add_upstream_remote(result, repo, effective_path)

            do_webhook = (
                register_webhook
                if register_webhook is not None
                else self.config.register_webhook
            )
            effective_wh_url = webhook_url or self.config.webhook_url
            effective_wh_events = webhook_events or self.config.webhook_events
            if do_webhook:
                self._register_webhook(result, effective_wh_url, effective_wh_events)

        result.elapsed_seconds = time.monotonic() - start
        logger.info(
            "fork '%s' → '%s'  status=%s  attempts=%d  %.1fs",
            full_name,
            result.fork.full_name if result.fork else "n/a",
            result.status.value,
            result.attempts,
            result.elapsed_seconds,
        )

        if self.config.on_fork_done:
            try:
                self.config.on_fork_done(result)
            except Exception:
                logger.debug("on_fork_done callback raised; ignoring.", exc_info=True)

        return result

    # ---- 2. fork_async() — fire-and-forget, returns ForkJob ------------- #

    def fork_async(
        self,
        source: Union[str, Repository],
        *,
        organization: Optional[Union[str, Organization]] = None,
        name: Optional[str] = None,
        default_branch_only: Optional[bool] = None,
        add_upstream_remote: Optional[bool] = None,
        local_path: Optional[str] = None,
        register_webhook: Optional[bool] = None,
        webhook_url: Optional[str] = None,
        webhook_events: Optional[list[str]] = None,
    ) -> ForkJob:
        """
        Submit a fork to the background thread pool and return immediately.

        The returned :class:`ForkJob` lets you query progress and retrieve
        the result from *any* thread without blocking the caller:

        .. code-block:: python

            job = forker.fork_async("octocat/Hello-World")

            # do other things ...

            # poll without blocking:
            print(job.done, job.status)

            # or block when you actually need the result:
            result = job.wait()

        The fork runs inside the shared ``ThreadPoolExecutor`` (size set by
        ``config.pool_workers``).  All the same retry, backoff, readiness
        polling, and post-fork actions as :meth:`fork` apply.

        Parameters
        ----------
        Same as :meth:`fork`.

        Returns
        -------
        ForkJob
        """
        source_name = (
            source if isinstance(source, str)
            else getattr(source, "full_name", str(source))
        )
        future = self._executor.submit(
            self.fork,
            source,
            organization=organization,
            name=name,
            default_branch_only=default_branch_only,
            add_upstream_remote=add_upstream_remote,
            local_path=local_path,
            register_webhook=register_webhook,
            webhook_url=webhook_url,
            webhook_events=webhook_events,
        )
        return ForkJob(future, source_name)

    # ---- 3. fork_many() — batch, optionally pooled ---------------------- #

    def fork_many(
        self,
        sources: Iterable[Union[str, Repository, ForkRequest]],
        *,
        organization: Optional[Union[str, Organization]] = None,
        name: Optional[str] = None,
        default_branch_only: Optional[bool] = None,
        add_upstream_remote: Optional[bool] = None,
        local_path: Optional[str] = None,
        register_webhook: Optional[bool] = None,
        webhook_url: Optional[str] = None,
        webhook_events: Optional[list[str]] = None,
        parallel: bool = True,
        stop_on_error: bool = False,
    ) -> list[ForkResult]:
        """
        Fork multiple repositories.

        When ``parallel=True`` (default), forks run concurrently inside the
        shared ``ThreadPoolExecutor`` (up to ``config.pool_workers`` at once).
        When ``parallel=False``, they run one at a time in order.

        Each item in *sources* can be:

        * A plain ``"owner/repo"`` string — uses the shared keyword args below.
        * A :class:`~pygithub_fork.models.Repository` object — same.
        * A :class:`~pygithub_fork.models.ForkRequest` — overrides shared
          args for that one item; ideal when batch-forking into different
          organizations or with different names.

        Per-item failures are captured in the returned ``ForkResult``
        (.succeeded == False, .error set) rather than raised, **unless**
        ``stop_on_error=True``.

        Parameters
        ----------
        sources : iterable
            Repositories to fork.
        parallel : bool
            Run forks concurrently (default True).
        stop_on_error : bool
            Abort the batch on the first error (default False).
        organization, name, default_branch_only, add_upstream_remote,
        local_path, register_webhook, webhook_url, webhook_events :
            Shared defaults for all items; overridden per-item by ForkRequest.

        Returns
        -------
        list[ForkResult]
            In the *same order* as ``sources``, regardless of completion order.
        """
        requests = self._normalize_requests(
            sources,
            organization=organization,
            name=name,
            default_branch_only=default_branch_only,
            add_upstream_remote=add_upstream_remote,
            local_path=local_path,
            register_webhook=register_webhook,
            webhook_url=webhook_url,
            webhook_events=webhook_events,
        )

        if parallel:
            return self._run_parallel(requests, stop_on_error=stop_on_error)
        else:
            return self._run_sequential(requests, stop_on_error=stop_on_error)

    # ---- 4. fork_iter() — streaming generator for large batches --------- #

    def fork_iter(
        self,
        sources: Iterable[Union[str, Repository, ForkRequest]],
        *,
        organization: Optional[Union[str, Organization]] = None,
        name: Optional[str] = None,
        default_branch_only: Optional[bool] = None,
        add_upstream_remote: Optional[bool] = None,
        local_path: Optional[str] = None,
        register_webhook: Optional[bool] = None,
        webhook_url: Optional[str] = None,
        webhook_events: Optional[list[str]] = None,
    ) -> Iterator[ForkResult]:
        """
        Like fork_many(parallel=True) but yields each ForkResult as soon as
        it completes (completion order, not submission order).  Useful for
        large batches where you want to process results incrementally.

        .. code-block:: python

            for result in forker.fork_iter(["owner/a", "owner/b", "owner/c"]):
                print(result.source_full_name, result.status)
        """
        requests = self._normalize_requests(
            sources,
            organization=organization,
            name=name,
            default_branch_only=default_branch_only,
            add_upstream_remote=add_upstream_remote,
            local_path=local_path,
            register_webhook=register_webhook,
            webhook_url=webhook_url,
            webhook_events=webhook_events,
        )
        futures = {
            self._executor.submit(self._run_one_request, req): req
            for req in requests
        }
        for future in as_completed(futures):
            result = future.result()  # _run_one_request never raises
            yield result

    # ================================================================== #
    # Internals — fork lifecycle
    # ================================================================== #

    def _resolve_source(self, source: Union[str, Repository]) -> Repository:
        if isinstance(source, Repository):
            return source
        if not isinstance(source, str) or "/" not in source:
            raise ValueError(
                f"source must be 'owner/repo' string or Repository object, got {source!r}"
            )
        try:
            return self._gh.get_repo(source)
        except GithubException as exc:
            if exc.status == 404:
                raise RepositoryNotFoundError(
                    f"Repository '{source}' not found or not accessible "
                    f"with the current token."
                ) from exc
            if exc.status in (401, 403):
                raise ForkPermissionError(
                    f"Not authorized to access '{source}': {exc.data}"
                ) from exc
            raise ForkError(f"Failed to fetch '{source}': {exc.data}") from exc

    def _find_existing_fork(
        self,
        repo: Repository,
        organization: Optional[Union[str, Organization]],
    ) -> Optional[Repository]:
        """Return the pre-existing fork if present, otherwise None."""
        try:
            target_login = self._target_login(organization)
        except GithubException:
            return None

        try:
            candidate = self._gh.get_repo(f"{target_login}/{repo.name}")
        except GithubException as exc:
            if exc.status == 404:
                return None
            logger.debug("Existing-fork lookup failed non-fatally: %s", exc)
            return None

        is_fork = getattr(candidate, "fork", False)
        parent = getattr(candidate, "parent", None)
        if is_fork and parent is not None and parent.full_name == repo.full_name:
            return candidate
        return None

    def _target_login(
        self, organization: Optional[Union[str, Organization]]
    ) -> str:
        if organization is None:
            return self._gh.get_user().login
        if isinstance(organization, Organization):
            return organization.login
        return str(organization)

    def _create_with_retry(
        self,
        repo: Repository,
        *,
        organization: Optional[Union[str, Organization]],
        name: Optional[str],
        default_branch_only: Optional[bool],
    ) -> tuple[Repository, int]:
        cfg = self.config
        kwargs: dict = {
            "organization": organization if organization is not None else NotSet,
            "name":         name         if name         is not None else NotSet,
            "default_branch_only": (
                default_branch_only if default_branch_only is not None else NotSet
            ),
        }

        last_exc: Optional[Exception] = None
        for attempt in range(1, cfg.max_retries + 1):
            try:
                return repo.create_fork(**kwargs), attempt

            except _RateLimitExc as exc:
                sleep_for = self._seconds_until_reset(exc)
                self._notify_retry(attempt, exc, sleep_for)
                time.sleep(sleep_for)
                last_exc = exc

            except GithubException as exc:
                last_exc = exc
                if exc.status == 404:
                    raise RepositoryNotFoundError(
                        f"Repository '{repo.full_name}' disappeared mid-fork."
                    ) from exc
                if exc.status in (401, 403):
                    if self._is_secondary_rate_limit(exc):
                        sleep_for = self._backoff(attempt, cfg)
                        self._notify_retry(attempt, exc, sleep_for)
                        time.sleep(sleep_for)
                        continue
                    raise ForkPermissionError(
                        f"Not permitted to fork '{repo.full_name}' "
                        f"(target: {organization or 'your account'}): {exc.data}"
                    ) from exc
                if exc.status == 422:
                    raise ForkError(
                        f"GitHub rejected fork of '{repo.full_name}': {exc.data}"
                    ) from exc
                if exc.status >= 500 or exc.status == 429:
                    sleep_for = self._backoff(attempt, cfg)
                    self._notify_retry(attempt, exc, sleep_for)
                    time.sleep(sleep_for)
                    continue
                raise ForkError(
                    f"Unexpected error (HTTP {exc.status}) forking "
                    f"'{repo.full_name}': {exc.data}"
                ) from exc

            except Exception as exc:
                last_exc = exc
                sleep_for = self._backoff(attempt, cfg)
                self._notify_retry(attempt, exc, sleep_for)
                time.sleep(sleep_for)

        raise ForkError(
            f"Max retries ({cfg.max_retries}) exceeded forking "
            f"'{repo.full_name}'. Last error: {last_exc}"
        ) from last_exc

    def _await_ready(self, result: ForkResult, start_time: float) -> None:
        """
        Poll the fork until GitHub says it's populated (pushed_at or branches
        present), or until ready_timeout_seconds elapses.

        GitHub's fork endpoint returns *immediately* but the underlying Git
        data (refs, objects, branches) can take 5-30+ seconds to actually
        copy server-side.  Code that reads/pushes to the fork without waiting
        will see 404s or empty repos.
        """
        cfg = self.config
        deadline = time.monotonic() + cfg.ready_timeout_seconds
        full_name = result.fork.full_name  # type: ignore[union-attr]

        logger.debug("Waiting for fork '%s' to be ready …", full_name)

        while time.monotonic() < deadline:
            try:
                refreshed = self._gh.get_repo(full_name)
                if self._is_populated(refreshed):
                    result.fork = refreshed
                    result.status = ForkStatus.READY
                    return
            except GithubException as exc:
                if exc.status != 404:
                    logger.debug("Non-fatal poll error: %s", exc)
            time.sleep(cfg.ready_poll_interval_seconds)

        waited = time.monotonic() - start_time
        result.status = ForkStatus.TIMED_OUT_WAITING
        logger.warning(
            "Fork '%s' created but not confirmed ready after %.0fs.",
            full_name, waited,
        )

    # ================================================================== #
    # Post-fork actions
    # ================================================================== #

    def _add_upstream_remote(
        self,
        result: ForkResult,
        source_repo: Repository,
        local_path: Optional[str],
    ) -> None:
        """
        Run ``git remote add upstream <source_clone_url>`` in *local_path*.

        The remote is named ``upstream`` (conventional name for the original
        repo when working with forks).  If ``upstream`` already exists the
        command is a no-op (we check first rather than fail on duplicate).

        Raises
        ------
        UpstreamRemoteError
            If git is not available, local_path doesn't exist, or the
            subprocess fails for any reason other than "remote already exists".
        """
        if not local_path:
            logger.warning(
                "add_upstream_remote=True but no local_path provided; skipping."
            )
            return

        if not os.path.isdir(local_path):
            raise UpstreamRemoteError(
                f"local_path '{local_path}' does not exist or is not a directory."
            )

        upstream_url = source_repo.clone_url  # HTTPS; swap for ssh_url if preferred

        # Check whether 'upstream' already exists to keep the operation idempotent.
        check = subprocess.run(
            ["git", "remote", "get-url", "upstream"],
            cwd=local_path,
            capture_output=True,
            text=True,
        )
        if check.returncode == 0:
            existing_url = check.stdout.strip()
            if existing_url == upstream_url:
                logger.debug(
                    "Remote 'upstream' already points to '%s'; nothing to do.",
                    upstream_url,
                )
                result.upstream_remote_added = True
                return
            else:
                logger.warning(
                    "Remote 'upstream' exists but points to '%s' (expected '%s'); "
                    "leaving it unchanged.",
                    existing_url, upstream_url,
                )
                return

        proc = subprocess.run(
            ["git", "remote", "add", "upstream", upstream_url],
            cwd=local_path,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise UpstreamRemoteError(
                f"git remote add upstream failed in '{local_path}': "
                f"{proc.stderr.strip()}"
            )

        logger.info(
            "Added remote 'upstream' → '%s' in '%s'.",
            upstream_url, local_path,
        )
        result.upstream_remote_added = True

    def _register_webhook(
        self,
        result: ForkResult,
        webhook_url: Optional[str],
        events: Optional[list[str]],
    ) -> None:
        """
        Register a GitHub webhook on the freshly created fork.

        The hook is created with ``active=True`` and the content type and
        secret from ForkerConfig.  Duplicate hooks (same url + events) on the
        same fork are silently de-duplicated by checking existing hooks first.

        Raises
        ------
        WebhookError
            If no webhook_url is configured or the API call fails.
        """
        if not webhook_url:
            raise WebhookError(
                "register_webhook=True but no webhook_url configured. "
                "Set ForkerConfig.webhook_url or pass webhook_url=... to fork()."
            )
        if not result.fork:
            return

        events = events or self.config.webhook_events or ["push", "fork"]
        cfg = self.config

        # De-duplicate: skip if an identical hook already exists.
        try:
            for hook in result.fork.get_hooks():
                if hook.config.get("url") == webhook_url:
                    logger.debug(
                        "Webhook for '%s' already registered (id=%d); skipping.",
                        webhook_url, hook.id,
                    )
                    result.webhook_id = hook.id
                    return
        except GithubException as exc:
            logger.warning("Could not list existing hooks: %s; proceeding.", exc)

        hook_config: dict = {
            "url":          webhook_url,
            "content_type": cfg.webhook_content_type,
            "insecure_ssl": "1" if cfg.webhook_insecure_ssl else "0",
        }
        if cfg.webhook_secret:
            hook_config["secret"] = cfg.webhook_secret

        try:
            hook = result.fork.create_hook(
                name="web",
                config=hook_config,
                events=events,
                active=True,
            )
            result.webhook_id = hook.id
            logger.info(
                "Registered webhook id=%d on '%s' for events %s.",
                hook.id, result.fork.full_name, events,
            )
        except GithubException as exc:
            raise WebhookError(
                f"Failed to register webhook on '{result.fork.full_name}': "
                f"{exc.data}"
            ) from exc

    # ================================================================== #
    # Internals — batch helpers
    # ================================================================== #

    @staticmethod
    def _normalize_requests(
        sources: Iterable[Union[str, Repository, ForkRequest]],
        **defaults,
    ) -> list[ForkRequest]:
        out = []
        for item in sources:
            if isinstance(item, ForkRequest):
                # Per-item ForkRequest overrides shared defaults only for
                # fields where the request has an explicit (non-None) value.
                req = ForkRequest(
                    source=item.source,
                    organization=item.organization if item.organization is not None else defaults.get("organization"),
                    name=item.name if item.name is not None else defaults.get("name"),
                    default_branch_only=item.default_branch_only if item.default_branch_only is not None else defaults.get("default_branch_only"),
                    add_upstream_remote=item.add_upstream_remote if item.add_upstream_remote is not None else defaults.get("add_upstream_remote"),
                    local_path=item.local_path if item.local_path is not None else defaults.get("local_path"),
                    register_webhook=item.register_webhook if item.register_webhook is not None else defaults.get("register_webhook"),
                    webhook_url=item.webhook_url if item.webhook_url is not None else defaults.get("webhook_url"),
                    webhook_events=item.webhook_events if item.webhook_events is not None else defaults.get("webhook_events"),
                )
            else:
                req = ForkRequest(source=item, **{k: v for k, v in defaults.items() if v is not None})
            out.append(req)
        return out

    def _run_one_request(self, req: ForkRequest) -> ForkResult:
        """Run a single ForkRequest; catches all exceptions into ForkResult."""
        src_name = (
            req.source if isinstance(req.source, str)
            else getattr(req.source, "full_name", str(req.source))
        )
        try:
            return self.fork(
                req.source,
                organization=req.organization,
                name=req.name,
                default_branch_only=req.default_branch_only,
                add_upstream_remote=req.add_upstream_remote,
                local_path=req.local_path,
                register_webhook=req.register_webhook,
                webhook_url=req.webhook_url,
                webhook_events=req.webhook_events,
            )
        except ForkError as exc:
            logger.error("Failed to fork '%s': %s", src_name, exc)
            return ForkResult(
                source_full_name=str(src_name),
                fork=None,
                status=ForkStatus.FAILED,
                already_existed=False,
                error=exc,
            )
        except Exception as exc:
            logger.error("Unexpected error forking '%s': %s", src_name, exc, exc_info=True)
            return ForkResult(
                source_full_name=str(src_name),
                fork=None,
                status=ForkStatus.FAILED,
                already_existed=False,
                error=exc,
            )

    def _run_parallel(
        self, requests: list[ForkRequest], *, stop_on_error: bool
    ) -> list[ForkResult]:
        """Submit all requests to the pool; collect results in original order."""
        index_future: dict[int, Future] = {
            i: self._executor.submit(self._run_one_request, req)
            for i, req in enumerate(requests)
        }
        results: list[Optional[ForkResult]] = [None] * len(requests)
        for i, future in index_future.items():
            result = future.result()  # _run_one_request never raises
            results[i] = result
            if stop_on_error and not result.succeeded:
                # Cancel remaining pending futures
                for j, f in index_future.items():
                    if j > i:
                        f.cancel()
                break
        # Fill any cancelled slots
        for i, r in enumerate(results):
            if r is None:
                src = requests[i].source
                results[i] = ForkResult(
                    source_full_name=str(src),
                    fork=None,
                    status=ForkStatus.FAILED,
                    already_existed=False,
                    error=ForkError("Cancelled due to stop_on_error."),
                )
        return results  # type: ignore[return-value]

    def _run_sequential(
        self, requests: list[ForkRequest], *, stop_on_error: bool
    ) -> list[ForkResult]:
        results = []
        for req in requests:
            result = self._run_one_request(req)
            results.append(result)
            if stop_on_error and not result.succeeded:
                break
        return results

    # ================================================================== #
    # Internals — utilities
    # ================================================================== #

    @staticmethod
    def _is_populated(repo: Repository) -> bool:
        if getattr(repo, "pushed_at", None) is not None:
            return True
        try:
            return repo.get_branches().totalCount > 0
        except Exception:
            return False

    @staticmethod
    def _is_secondary_rate_limit(exc: GithubException) -> bool:
        data = exc.data or {}
        msg = str(data.get("message", "")).lower()
        return "secondary rate limit" in msg or "abuse" in msg

    @staticmethod
    def _seconds_until_reset(exc: Exception) -> float:
        try:
            headers = getattr(exc, "headers", {}) or {}
            reset_at = headers.get("x-ratelimit-reset")
            if reset_at:
                reset_dt = datetime.fromtimestamp(int(reset_at), tz=timezone.utc)
                delta = (reset_dt - datetime.now(timezone.utc)).total_seconds()
                return max(delta, 1.0) + 1.0
        except Exception:
            pass
        return 30.0

    @staticmethod
    def _backoff(attempt: int, cfg: ForkerConfig) -> float:
        raw = cfg.base_backoff_seconds * (2 ** (attempt - 1))
        capped = min(raw, cfg.max_backoff_seconds)
        return capped + random.uniform(0.0, capped * 0.25)

    def _notify_retry(self, attempt: int, exc: Exception, sleep_for: float) -> None:
        logger.warning(
            "Attempt %d/%d failed (%s); retrying in %.1fs.",
            attempt, self.config.max_retries, exc, sleep_for,
        )
        if self.config.on_retry:
            try:
                self.config.on_retry(attempt, exc, sleep_for)
            except Exception:
                logger.debug("on_retry callback raised; ignoring.", exc_info=True)
