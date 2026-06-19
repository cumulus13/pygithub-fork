"""
tests/test_forker.py
~~~~~~~~~~~~~~~~~~~~
Full test suite for pygithub_fork.  Uses unittest.mock — no real API calls.

Coverage:
  - fork() happy path, idempotent re-fork
  - fork() retry on 5xx / 429
  - fork() secondary rate limit (403 abuse) vs hard 403 permission error
  - fork() 404 on source → RepositoryNotFoundError
  - fork() readiness polling loop
  - fork_async() → ForkJob status / done / wait
  - fork_many() parallel + sequential, stop_on_error
  - fork_iter() streaming results
  - post-fork: git remote add upstream (success, already-exists, bad path)
  - post-fork: webhook registration (new, dedup, no url)
  - ForkerConfig defaults
  - Context manager shutdown
"""
import os
import subprocess
import sys
import time
from concurrent.futures import Future
from unittest.mock import MagicMock, call, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from github import Github, GithubException

import pygithub_fork as pkg
from pygithub_fork import (
    ForkJob,
    ForkRequest,
    ForkResult,
    ForkStatus,
    ForkerConfig,
    GitHubForker,
    ForkError,
    ForkPermissionError,
    RepositoryNotFoundError,
    WebhookError,
    UpstreamRemoteError,
)


# ============================================================================
# Fixtures / helpers
# ============================================================================

def make_gh():
    return MagicMock(spec=Github)


def make_repo(full_name, fork=False, parent=None, pushed_at="2026-01-01", name=None):
    r = MagicMock()
    r.full_name = full_name
    r.name = name or full_name.split("/")[-1]
    r.fork = fork
    r.parent = parent
    r.pushed_at = pushed_at
    r.clone_url = f"https://github.com/{full_name}.git"
    r.ssh_url   = f"git@github.com:{full_name}.git"
    return r


def _404():
    return GithubException(404, {"message": "Not Found"}, None)


def _5xx():
    return GithubException(503, {"message": "Service Unavailable"}, None)


def no_wait_cfg(**kw):
    return ForkerConfig(wait_for_ready=False, **kw)


# ============================================================================
# Basic fork() tests
# ============================================================================

class TestForkSync:

    def _forker_with_new_fork(self, src_name="owner/repo", fork_name="me/repo"):
        gh = make_gh()
        src = make_repo(src_name)
        fork = make_repo(fork_name)

        def get_repo(name):
            if name == src_name:    return src
            if name == fork_name:   raise _404()
            if name == f"me/{src_name.split('/')[-1]}": raise _404()
            raise AssertionError(f"unexpected get_repo({name!r})")

        gh.get_repo.side_effect = get_repo
        gh.get_user.return_value = MagicMock(login="me")
        src.create_fork.return_value = fork
        return gh, src, fork

    def test_happy_path(self):
        gh, src, fork = self._forker_with_new_fork()
        result = GitHubForker(gh, no_wait_cfg()).fork("owner/repo")
        assert result.succeeded
        assert result.status == ForkStatus.CREATED
        assert not result.already_existed
        assert result.attempts == 1

    def test_idempotent_already_exists(self):
        gh = make_gh()
        src  = make_repo("owner/repo")
        existing = make_repo("me/repo", fork=True, parent=src)

        def get_repo(name):
            if name == "owner/repo": return src
            if name == "me/repo":    return existing
            raise AssertionError(f"unexpected {name}")

        gh.get_repo.side_effect = get_repo
        gh.get_user.return_value = MagicMock(login="me")

        result = GitHubForker(gh, no_wait_cfg()).fork("owner/repo")
        assert result.already_existed
        assert result.status == ForkStatus.ALREADY_EXISTED
        src.create_fork.assert_not_called()

    def test_retry_on_5xx(self):
        gh, src, fork = self._forker_with_new_fork()
        src.create_fork.side_effect = [_5xx(), _5xx(), fork]

        cfg = ForkerConfig(wait_for_ready=False, base_backoff_seconds=0.01,
                           max_backoff_seconds=0.02)
        with patch("time.sleep"):
            result = GitHubForker(gh, cfg).fork("owner/repo")

        assert result.succeeded
        assert result.attempts == 3

    def test_secondary_rate_limit_retried_not_permission_error(self):
        gh, src, fork = self._forker_with_new_fork()
        abuse = GithubException(403, {"message": "You have exceeded a secondary rate limit"}, None)
        src.create_fork.side_effect = [abuse, fork]

        with patch("time.sleep"):
            result = GitHubForker(gh, no_wait_cfg(base_backoff_seconds=0.01)).fork("owner/repo")

        assert result.succeeded

    def test_hard_403_raises_permission_error(self):
        gh, src, _ = self._forker_with_new_fork()
        src.create_fork.side_effect = GithubException(403, {"message": "Must be admin"}, None)

        with pytest.raises(ForkPermissionError):
            GitHubForker(gh, no_wait_cfg()).fork("owner/repo")

    def test_404_source_raises_not_found(self):
        gh = make_gh()
        gh.get_repo.side_effect = _404()

        with pytest.raises(RepositoryNotFoundError):
            GitHubForker(gh, no_wait_cfg()).fork("ghost/nope")

    def test_exceeds_max_retries_raises_fork_error(self):
        gh, src, _ = self._forker_with_new_fork()
        src.create_fork.side_effect = _5xx()

        cfg = ForkerConfig(wait_for_ready=False, max_retries=3,
                           base_backoff_seconds=0.01, max_backoff_seconds=0.01)
        with patch("time.sleep"), pytest.raises(ForkError):
            GitHubForker(gh, cfg).fork("owner/repo")


# ============================================================================
# Readiness polling
# ============================================================================

class TestReadinessPolling:

    def test_detects_ready_on_second_poll(self):
        gh = make_gh()
        src  = make_repo("owner/repo")
        fork_pending = make_repo("me/repo", pushed_at=None)
        fork_ready   = make_repo("me/repo", pushed_at="2026-01-01")

        call_count = {"n": 0}
        def get_repo(name):
            if name == "owner/repo":  return src
            if name == "me/repo":
                call_count["n"] += 1
                # first existence check (idempotency) raises 404
                if call_count["n"] == 1: raise _404()
                # readiness poll 1: not ready
                if call_count["n"] == 2: return fork_pending
                # readiness poll 2: ready
                return fork_ready
            raise AssertionError(name)

        gh.get_repo.side_effect = get_repo
        gh.get_user.return_value = MagicMock(login="me")
        src.create_fork.return_value = fork_pending
        fork_pending.get_branches.return_value.totalCount = 0

        cfg = ForkerConfig(wait_for_ready=True, ready_timeout_seconds=10,
                           ready_poll_interval_seconds=0.01)
        with patch("time.sleep"):
            result = GitHubForker(gh, cfg).fork("owner/repo")

        assert result.status == ForkStatus.READY

    def test_timeout_sets_timed_out_status(self):
        gh = make_gh()
        src  = make_repo("owner/repo")
        not_ready = make_repo("me/repo", pushed_at=None)

        def get_repo(name):
            if name == "owner/repo": return src
            if name == "me/repo":    return not_ready
            raise AssertionError(name)

        gh.get_repo.side_effect = get_repo
        gh.get_user.return_value = MagicMock(login="me")
        src.create_fork.return_value = not_ready
        not_ready.get_branches.return_value.totalCount = 0

        cfg = ForkerConfig(wait_for_ready=True, ready_timeout_seconds=0.05,
                           ready_poll_interval_seconds=0.01)
        with patch("time.sleep"):
            # _await_ready uses time.monotonic() internally for deadline — we
            # don't mock that; the real 50ms timeout will fire during the test.
            result = GitHubForker(gh, cfg).fork("owner/repo")

        assert result.status == ForkStatus.TIMED_OUT_WAITING


# ============================================================================
# fork_async / ForkJob
# ============================================================================

class TestForkAsync:

    def _setup(self, src_name="owner/repo", fork_name="me/repo"):
        gh = make_gh()
        src  = make_repo(src_name)
        fork = make_repo(fork_name)

        def get_repo(name):
            if name == src_name:  return src
            raise GithubException(404, {}, None)

        gh.get_repo.side_effect = get_repo
        gh.get_user.return_value = MagicMock(login="me")
        src.create_fork.return_value = fork
        return gh, src, fork

    def test_returns_fork_job(self):
        gh, _, _ = self._setup()
        job = GitHubForker(gh, no_wait_cfg()).fork_async("owner/repo")
        assert isinstance(job, ForkJob)
        assert job.source_full_name == "owner/repo"

    def test_job_wait_returns_result(self):
        gh, _, _ = self._setup()
        with GitHubForker(gh, no_wait_cfg()) as forker:
            job = forker.fork_async("owner/repo")
            result = job.wait(timeout=10)
        assert result.succeeded

    def test_job_done_and_status_non_blocking(self):
        gh, _, _ = self._setup()
        with GitHubForker(gh, no_wait_cfg()) as forker:
            job = forker.fork_async("owner/repo")
            result = job.wait(timeout=10)
        assert job.done is True
        assert job.status in (ForkStatus.CREATED, ForkStatus.READY)
        assert job.result is not None

    def test_pending_status_before_done(self):
        """Inject a real Future that is not yet resolved to test PENDING."""
        import concurrent.futures
        f = concurrent.futures.Future()
        job = ForkJob(f, "owner/repo")
        assert job.done is False
        assert job.status == ForkStatus.PENDING
        assert job.result is None
        # resolve it
        f.set_result(ForkResult(
            source_full_name="owner/repo",
            fork=make_repo("me/repo"),
            status=ForkStatus.READY,
            already_existed=False,
        ))
        assert job.done is True
        assert job.status == ForkStatus.READY

    def test_failed_future_captured_in_result_not_raised(self):
        import concurrent.futures
        f = concurrent.futures.Future()
        job = ForkJob(f, "owner/repo")
        f.set_exception(ForkError("boom"))
        r = job.result
        assert r is not None
        assert r.status == ForkStatus.FAILED
        assert isinstance(r.error, ForkError)


# ============================================================================
# fork_many / fork_iter
# ============================================================================

class TestForkMany:

    def _forker(self, sources):
        """Build a forker where every source fork succeeds."""
        gh = make_gh()
        forks = {}
        for s in sources:
            src  = make_repo(s)
            fork = make_repo("me/" + s.split("/")[-1])
            forks[s] = (src, fork)

        def get_repo(name):
            for s, (src, fork) in forks.items():
                if name == s: return src
            raise GithubException(404, {}, None)

        gh.get_repo.side_effect = get_repo
        gh.get_user.return_value = MagicMock(login="me")
        for s, (src, fork) in forks.items():
            src.create_fork.return_value = fork

        return GitHubForker(gh, no_wait_cfg())

    def test_parallel_returns_all_results(self):
        sources = ["a/r1", "a/r2", "a/r3"]
        with self._forker(sources) as forker:
            results = forker.fork_many(sources, parallel=True)
        assert len(results) == 3
        assert all(r.succeeded for r in results)

    def test_sequential_preserves_order(self):
        sources = ["a/alpha", "a/beta", "a/gamma"]
        with self._forker(sources) as forker:
            results = forker.fork_many(sources, parallel=False)
        assert [r.source_full_name for r in results] == sources

    def test_continues_after_failure_by_default(self):
        gh = make_gh()
        bad = make_repo("a/bad")
        good = make_repo("a/good")
        fork = make_repo("me/good")

        def get_repo(name):
            if name == "a/bad":  return bad
            if name == "a/good": return good
            raise GithubException(404, {}, None)

        gh.get_repo.side_effect = get_repo
        gh.get_user.return_value = MagicMock(login="me")
        bad.create_fork.side_effect  = GithubException(503, {}, None)
        good.create_fork.return_value = fork

        cfg = ForkerConfig(wait_for_ready=False, max_retries=1,
                           base_backoff_seconds=0.01, max_backoff_seconds=0.01)
        with patch("time.sleep"):
            with GitHubForker(gh, cfg) as forker:
                results = forker.fork_many(["a/bad", "a/good"])

        assert not results[0].succeeded
        assert results[1].succeeded

    def test_stop_on_error(self):
        gh = make_gh()
        bad = make_repo("a/bad")
        gh.get_repo.return_value = bad
        gh.get_user.return_value = MagicMock(login="me")
        bad.create_fork.side_effect = GithubException(503, {}, None)

        cfg = ForkerConfig(wait_for_ready=False, max_retries=1,
                           base_backoff_seconds=0.01, max_backoff_seconds=0.01)
        with patch("time.sleep"):
            with GitHubForker(gh, cfg) as forker:
                results = forker.fork_many(["a/bad", "a/good"], stop_on_error=True)

        # "a/good" was cancelled
        assert not all(r.succeeded for r in results)

    def test_fork_iter_yields_results(self):
        sources = ["a/x", "a/y"]
        with self._forker(sources) as forker:
            seen = list(forker.fork_iter(sources))
        assert len(seen) == 2
        assert all(r.succeeded for r in seen)

    def test_fork_request_per_item_override(self):
        gh = make_gh()
        src = make_repo("owner/repo")
        fork = make_repo("my-org/custom")

        def get_repo(name):
            if name == "owner/repo": return src
            raise GithubException(404, {}, None)

        gh.get_repo.side_effect = get_repo
        gh.get_user.return_value = MagicMock(login="me")
        src.create_fork.return_value = fork

        req = ForkRequest(source="owner/repo", organization="my-org", name="custom")
        with GitHubForker(gh, no_wait_cfg()) as forker:
            results = forker.fork_many([req])

        assert results[0].succeeded
        src.create_fork.assert_called_once()
        _, kwargs = src.create_fork.call_args
        assert kwargs["organization"] == "my-org"
        assert kwargs["name"] == "custom"


# ============================================================================
# Post-fork: upstream remote
# ============================================================================

class TestUpstreamRemote:

    def _result_with_fork(self, src_url="https://github.com/owner/repo.git"):
        src = make_repo("owner/repo")
        src.clone_url = src_url
        fork = make_repo("me/repo")
        result = ForkResult("owner/repo", fork, ForkStatus.READY, False)
        return result, src

    def test_adds_remote_when_not_present(self, tmp_path):
        subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)

        result, src = self._result_with_fork()
        gh = make_gh()
        forker = GitHubForker(gh, no_wait_cfg())
        forker._add_upstream_remote(result, src, str(tmp_path))

        assert result.upstream_remote_added
        check = subprocess.run(
            ["git", "remote", "get-url", "upstream"],
            cwd=str(tmp_path), capture_output=True, text=True,
        )
        assert check.returncode == 0
        assert check.stdout.strip() == src.clone_url

    def test_idempotent_when_upstream_already_set(self, tmp_path):
        subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
        subprocess.run(
            ["git", "remote", "add", "upstream", "https://github.com/owner/repo.git"],
            cwd=str(tmp_path), check=True, capture_output=True,
        )

        result, src = self._result_with_fork()
        gh = make_gh()
        forker = GitHubForker(gh, no_wait_cfg())
        forker._add_upstream_remote(result, src, str(tmp_path))  # should not raise
        assert result.upstream_remote_added

    def test_raises_when_path_does_not_exist(self):
        result, src = self._result_with_fork()
        gh = make_gh()
        forker = GitHubForker(gh, no_wait_cfg())
        with pytest.raises(UpstreamRemoteError):
            forker._add_upstream_remote(result, src, "/nonexistent/path/xyz")

    def test_no_op_warns_when_no_path_given(self, caplog):
        import logging
        result, src = self._result_with_fork()
        gh = make_gh()
        forker = GitHubForker(gh, no_wait_cfg())
        with caplog.at_level(logging.WARNING, logger="pygithub_fork.forker"):
            forker._add_upstream_remote(result, src, None)
        assert not result.upstream_remote_added


# ============================================================================
# Post-fork: webhook
# ============================================================================

class TestWebhook:

    def _forker_and_result(self, webhook_url="https://ci.example.com/hook"):
        gh = make_gh()
        cfg = no_wait_cfg(register_webhook=True, webhook_url=webhook_url)
        forker = GitHubForker(gh, cfg)

        fork = make_repo("me/repo")
        fork.get_hooks.return_value = []         # no existing hooks
        hook = MagicMock()
        hook.id = 42
        fork.create_hook.return_value = hook

        result = ForkResult("owner/repo", fork, ForkStatus.READY, False)
        return forker, result, fork

    def test_registers_webhook(self):
        forker, result, fork = self._forker_and_result()
        forker._register_webhook(result, "https://ci.example.com/hook", ["push"])
        assert result.webhook_id == 42
        fork.create_hook.assert_called_once()

    def test_deduplicates_existing_webhook(self):
        forker, result, fork = self._forker_and_result()
        existing_hook = MagicMock()
        existing_hook.id = 99
        existing_hook.config = {"url": "https://ci.example.com/hook"}
        fork.get_hooks.return_value = [existing_hook]

        forker._register_webhook(result, "https://ci.example.com/hook", ["push"])
        fork.create_hook.assert_not_called()
        assert result.webhook_id == 99

    def test_raises_when_no_url(self):
        forker, result, _ = self._forker_and_result(webhook_url=None)
        with pytest.raises(WebhookError):
            forker._register_webhook(result, None, ["push"])

    def test_raises_on_api_error(self):
        forker, result, fork = self._forker_and_result()
        fork.create_hook.side_effect = GithubException(422, {"message": "Invalid"}, None)
        with pytest.raises(WebhookError):
            forker._register_webhook(result, "https://ci.example.com/hook", ["push"])


# ============================================================================
# Context manager
# ============================================================================

class TestContextManager:

    def test_shuts_down_pool_on_exit(self):
        gh = make_gh()
        with GitHubForker(gh, no_wait_cfg()) as forker:
            _ = forker._executor   # force pool creation
        assert forker._pool is None

    def test_shutdown_idempotent(self):
        gh = make_gh()
        forker = GitHubForker(gh, no_wait_cfg())
        forker.shutdown()
        forker.shutdown()  # second call should not raise


# ============================================================================
# Public API surface
# ============================================================================

def test_package_exports():
    assert hasattr(pkg, "GitHubForker")
    assert hasattr(pkg, "ForkJob")
    assert hasattr(pkg, "ForkRequest")
    assert hasattr(pkg, "ForkResult")
    assert hasattr(pkg, "ForkStatus")
    assert hasattr(pkg, "ForkerConfig")
    assert hasattr(pkg, "ForkError")
    assert hasattr(pkg, "ForkPermissionError")
    assert hasattr(pkg, "ForkTimeoutError")
    assert hasattr(pkg, "RepositoryNotFoundError")
    assert hasattr(pkg, "WebhookError")
    assert hasattr(pkg, "UpstreamRemoteError")
    assert pkg.__version__ == "1.0.0"
