# Private Fork Support Documentation

## Overview

This update adds comprehensive support for forking **private-to-private** repositories in the `github-forker` library. The feature allows users to create private forks from both public and private source repositories.

## New Features

### 1. **Private Parameter**

Added a new `private` parameter across all fork methods:
- `fork(source, ..., private=True)`
- `fork_async(source, ..., private=True)`
- `fork_many(sources, ..., private=True)`
- `fork_iter(sources, ..., private=True)`

### 2. **Configuration Support**

Added `private` field to `ForkerConfig`:

```python
from pygithub_fork import GitHubForker, ForkerConfig

config = ForkerConfig(
    private=True,  # Default all forks to private
    pool_workers=4,
    wait_for_ready=True,
)
forker = GitHubForker(gh, config)
```

### 3. **ForkRequest Enhancement**

Updated `ForkRequest` dataclass to include private fork support:

```python
from pygithub_fork import ForkRequest

requests = [
    ForkRequest("owner/public-repo", private=True),
    ForkRequest("owner/private-repo", private=True, organization="my-org"),
    ForkRequest("owner/another-repo", private=False),  # Override per-request
]
results = forker.fork_many(requests)
```

## Usage Examples

### Single Private Fork

```python
from github import Github
from pygithub_fork import GitHubForker

gh = Github("ghp_your_token")
forker = GitHubForker(gh)

# Fork a private repository as private
result = forker.fork(
    "private-owner/private-repo",
    private=True,
    organization="my-org"  # Optional: fork into an org
)

print(f"Status: {result.status}")
print(f"Fork URL: {result.clone_url}")
print(f"Is Private: {result.fork.private}")
```

### Async Private Fork

```python
# Fire-and-forget private fork
job = forker.fork_async(
    "private-owner/secret-repo",
    private=True
)

# ... do other work ...

# Check status without blocking
if job.done:
    result = job.result
    print(f"Fork is ready: {result.status}")
else:
    print(f"Fork status: {job.status}")

# Block when ready
result = job.wait()
```

### Bulk Private Forks

```python
# Fork multiple private repos concurrently
results = forker.fork_many(
    [
        "team-a/private-service",
        "team-b/private-lib",
        "team-c/private-config",
    ],
    private=True,
    organization="shared-org",
    parallel=True  # Run up to 4 concurrently
)

for result in results:
    if result.succeeded:
        print(f"✓ {result.source_full_name} → {result.fork.full_name}")
    else:
        print(f"✗ {result.source_full_name}: {result.error}")
```

### Per-Request Configuration

```python
from pygithub_fork import ForkRequest

requests = [
    ForkRequest(
        "company/private-repo-1",
        private=True,
        organization="eng-team"
    ),
    ForkRequest(
        "company/public-repo",
        private=False,  # Override: keep public
        organization="eng-team"
    ),
    ForkRequest(
        "contractor/external-lib",
        private=True,
        organization="external-review"
    ),
]

results = forker.fork_many(requests, parallel=False)  # Sequential for safety
```

### Global Configuration

```python
config = ForkerConfig(
    private=True,  # All forks default to private
    add_upstream_remote=True,
    local_clone_path="/path/to/clone",
    register_webhook=True,
    webhook_url="https://ci.example.com/github",
)

forker = GitHubForker(gh, config)

# All subsequent forks are private by default
result = forker.fork("owner/repo")  # private=True inherited from config

# Override per-call
result = forker.fork("owner/another", private=False)  # Force public
```

## Implementation Details

### Changes Made

1. **`models.py`**
   - Added `private: Optional[bool] = None` to `ForkRequest` dataclass
   - Added `private: bool = False` to `ForkerConfig` dataclass
   - Updated docstrings with private fork documentation

2. **`forker.py`**
   - Updated `fork()` method signature to include `private` parameter
   - Updated `fork_async()`, `fork_many()`, and `fork_iter()` methods
   - Modified `_create_with_retry()` to pass `private` parameter to PyGithub's `create_fork()`
   - Updated `_normalize_requests()` to handle `private` field
   - Enhanced `_run_one_request()` to pass private parameter
   - Updated module docstring to document private fork capability
   - Added comprehensive docstrings with usage examples

### Backwards Compatibility

✅ **Fully backwards compatible**
- The `private` parameter is optional with default value `None`
- When not specified, behavior matches PyGithub's default (public fork)
- Existing code requires no changes
- Old fork calls work exactly as before

## Requirements for Private Forks

### GitHub Organization Requirements

To fork a **private repository into a private fork**, your GitHub organization must have this permission enabled:

1. Go to **Organization Settings** → **Member privileges**
2. Find **"Repository forking"** section
3. Enable **"Allow forking of private repositories"**
4. Ensure your token has appropriate permissions (`repo` scope)

### Token Permissions

Your GitHub Personal Access Token (PAT) needs:
- `repo` scope (full control of private repositories)
- Organization admin status (for org-level private forks)

### Example PAT Creation

```bash
# Using GitHub CLI
gh auth login --with-token
# Paste a token with 'repo' scope

# Or create manually:
# GitHub Settings → Developer settings → Personal access tokens
# ✓ repo (Full control of private repositories)
# ✓ admin:org_hook (Administration of organization hooks)
```

## Error Handling

### Common Errors

**ForkPermissionError: "Not permitted to fork..."**
```python
# Cause: Organization doesn't allow private forks or token lacks permissions
# Solution: Enable org-level setting or use a token with 'repo' scope
```

**ForkError: "GitHub rejected fork"** (HTTP 422)
```python
# Cause: Invalid parameters or upstream doesn't support this fork type
# Solution: Check organization permissions and token scope
```

**RepositoryNotFoundError**
```python
# Cause: Token can't access the private repository
# Solution: Add token to organization or grant repo access
```

## Testing

```python
import pytest
from pygithub_fork import GitHubForker, ForkerConfig, ForkRequest

def test_private_fork_creation(gh_token):
    """Test forking a private repo as private."""
    gh = Github(gh_token)
    forker = GitHubForker(gh)
    
    result = forker.fork(
        "test-owner/private-repo",
        private=True
    )
    
    assert result.succeeded
    assert result.fork.private is True
    assert result.status == ForkStatus.READY

def test_private_fork_with_config():
    """Test private fork default from config."""
    config = ForkerConfig(private=True)
    forker = GitHubForker(gh, config)
    
    result = forker.fork("owner/repo")
    assert result.fork.private is True

def test_override_private_per_request():
    """Test per-request override of config default."""
    config = ForkerConfig(private=True)
    forker = GitHubForker(gh, config)
    
    result = forker.fork("owner/repo", private=False)
    assert result.fork.private is False
```

## Migration Guide

### From Previous Versions

If you're upgrading from a previous version:

```python
# Old code (still works)
forker = GitHubForker(gh)
result = forker.fork("owner/repo")

# New: Create private forks
result = forker.fork("owner/repo", private=True)

# Or set globally
config = ForkerConfig(private=True)
forker = GitHubForker(gh, config)
```

No code changes required for existing forks—the feature is additive.

## Changelog

- ✨ Added `private` parameter to all fork methods
- ✨ Added `private` field to `ForkerConfig`
- ✨ Added `private` field to `ForkRequest`
- 📝 Updated documentation and docstrings
- ✅ Maintained 100% backwards compatibility
- ✅ All existing tests pass

## References

- [GitHub API: Create a fork](https://docs.github.com/en/rest/repos/forks#create-a-fork)
- [Managing the forking policy for your organization](https://docs.github.com/en/organizations/managing-organization-settings/managing-the-forking-policy-for-your-organization)
- [PyGithub Repository.create_fork()](https://pygithub.readthedocs.io/en/latest/github_objects/Repository.html#github.Repository.Repository.create_fork)
