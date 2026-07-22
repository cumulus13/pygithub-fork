## Description

This pull request adds comprehensive support for **private-to-private repository forking** to the `github-forker` library.

## What's New

### Features Added ✨
- **`private` parameter** added to all fork methods (`fork()`, `fork_async()`, `fork_many()`, `fork_iter()`)
- **`ForkerConfig.private`** field for global default private fork setting
- **`ForkRequest.private`** field for per-request override
- Full support for creating private forks from both public and private source repositories
- Comprehensive documentation and usage examples

### Changes

#### `src/pygithub_fork/models.py`
- Added `private: Optional[bool] = None` to `ForkRequest` dataclass
- Added `private: bool = False` to `ForkerConfig` dataclass
- Updated docstrings with private fork documentation

#### `src/pygithub_fork/forker.py`
- Updated `fork()` method signature to include `private` parameter
- Updated `fork_async()`, `fork_many()`, and `fork_iter()` method signatures
- Modified `_create_with_retry()` to pass `private` parameter to PyGithub's `create_fork()` API
- Updated `_normalize_requests()` to handle `private` field in ForkRequest
- Enhanced `_run_one_request()` to pass private parameter
- Updated module docstring to document private fork capability
- Added comprehensive docstrings with usage examples

### Documentation
- Added `PRIVATE_FORK_SUPPORT.md` with complete usage guide
- Included setup requirements and permissions needed
- Added error handling examples
- Provided migration guide for existing users

## Usage Examples

### Single Private Fork
```python
from github import Github
from pygithub_fork import GitHubForker

gh = Github("ghp_your_token")
forker = GitHubForker(gh)

result = forker.fork(
    "private-owner/private-repo",
    private=True,
    organization="my-org"
)
```

### Global Configuration
```python
from pygithub_fork import ForkerConfig, GitHubForker

config = ForkerConfig(private=True)  # Default all forks to private
forker = GitHubForker(gh, config)

result = forker.fork("owner/repo")  # Creates private fork
```

### Bulk Private Forks
```python
results = forker.fork_many(
    ["team-a/private-service", "team-b/private-lib"],
    private=True,
    organization="shared-org",
    parallel=True
)
```

## Backwards Compatibility ✅
- **100% backwards compatible** - no breaking changes
- `private` parameter is optional with sensible defaults
- Existing code continues to work unchanged
- Old fork calls behave exactly as before

## Testing
- All existing tests pass
- Added test examples in documentation
- Ready for integration testing with real GitHub API

## Requirements
- GitHub organization must enable: **Settings → Member privileges → Allow forking of private repositories**
- Token must have `repo` scope
- For org-level private forks: organization admin status required

## Related Issues
- Resolves: Support for private-to-private repository forking
- Addresses: GitHub's requirement for explicit permission to fork private repos

## Checklist
- [x] Code follows project style guidelines
- [x] Documentation updated
- [x] Backwards compatibility maintained
- [x] All parameters properly typed
- [x] Error handling in place
- [x] Examples provided

## Type of Change
- [x] New feature (non-breaking)
- [ ] Bug fix
- [ ] Documentation update
- [ ] Breaking change
