# Multi-Authentication Support Documentation

## Overview

This update adds comprehensive **multi-authentication support** to the `github-forker` library, enabling you to fork repositories from **multiple owners/organizations** using different GitHub tokens.

## Problem Solved

**Before**: You could only use a single token, limiting forks to repositories accessible by that token.

**After**: Different owners/organizations can use different tokens, allowing seamless forking across multiple GitHub accounts.

## New Features

### 1. **AuthManager Class**

A new authentication management system that:
- Maps repository owners to specific tokens
- Automatically selects the correct token per owner
- Falls back to a default token for unknown owners
- Caches clients for performance
- Validates token access

### 2. **Enhanced GitHubForker**

Now accepts:
- Single token string (backwards compatible)
- Dict of owner → token mappings
- AuthManager instance for advanced scenarios

## Usage Examples

### Single Token (Backwards Compatible)

```python
from pygithub_fork import GitHubForker

# Single token - works for all owners (existing behavior)
forker = GitHubForker("ghp_your_token")
result = forker.fork("octocat/Hello-World")
```

### Multi-Owner Setup

```python
from pygithub_fork import GitHubForker

# Multiple tokens for different owners
auth_map = {
    "org-a": "ghp_token_org_a",
    "org-b": "ghp_token_org_b",
    "org-c": "ghp_token_org_c",
    "default": "ghp_token_default",  # Fallback for unknown owners
}

forker = GitHubForker(auth_map)

# Each fork uses the appropriate token
result1 = forker.fork("org-a/private-repo")   # Uses ghp_token_org_a
result2 = forker.fork("org-b/private-repo")   # Uses ghp_token_org_b
result3 = forker.fork("org-c/private-repo")   # Uses ghp_token_org_c
result4 = forker.fork("unknown-org/repo")     # Uses ghp_token_default
```

### Dynamic Token Management

```python
from pygithub_fork import GitHubForker, AuthManager

# Create AuthManager
auth = AuthManager({
    "org-a": "ghp_token_a",
})

# Add tokens dynamically
auth.add_owner_token("org-b", "ghp_token_b")
auth.add_owner_token("org-c", "ghp_token_c")

# Set fallback
auth.set_default_token("ghp_token_default")

# Use with forker
forker = GitHubForker(auth)
result = forker.fork("org-a/repo", private=True)
```

### Bulk Forking from Multiple Owners

```python
from pygithub_fork import GitHubForker, ForkRequest

auth_map = {
    "team-a": "ghp_token_a",
    "team-b": "ghp_token_b",
    "team-c": "ghp_token_c",
    "default": "ghp_token_default",
}

forker = GitHubForker(auth_map)

# Fork from different owners in parallel
results = forker.fork_many([
    ForkRequest("team-a/private-service", organization="shared-org", private=True),
    ForkRequest("team-b/private-lib", organization="shared-org", private=True),
    ForkRequest("team-c/private-config", organization="shared-org", private=True),
    ForkRequest("unknown-team/public-repo", organization="shared-org"),
], parallel=True)

for result in results:
    if result.succeeded:
        print(f"✓ {result.source_full_name} → {result.fork.full_name}")
    else:
        print(f"✗ {result.source_full_name}: {result.error}")
```

### Token Verification

```python
from pygithub_fork import AuthManager

auth = AuthManager({
    "org-a": "ghp_token_a",
    "org-b": "ghp_token_b",
})

# Verify tokens work before forking
for owner in auth.get_available_owners():
    if auth.verify_owner_access(owner):
        print(f"✓ {owner}: Token is valid")
    else:
        print(f"✗ {owner}: Token is invalid or expired")

# Check for default token
if not auth.has_default_token():
    print("⚠ No default token - will fail for unknown owners")
```

## Architecture

### AuthManager Flow

```
┌─────────────────────────────────┐
│ GitHubForker.fork("org-a/repo") │
└──────────────┬──────────────────┘
               │
               ▼
        Extract Owner ("org-a")
               │
               ▼
    ┌──────────────────────┐
    │   AuthManager       │
    │  get_client(owner)  │
    └──────┬───────┬──────┘
           │       │
      Yes  │       │  No
           ▼       ▼
      ┌─────┐   ┌──────────────┐
      │Owner│   │Default Token?│
      │Token│   └──┬───────┬──┘
      │Found│      │       │
      └─────┘      │       │
           │      Yes      No
           │       │       │
           │       ▼       ▼
           │   ┌────────┐ ValueError
           │   │Default │
           │   │Token   │
           │   └────────┘
           │       │
           └───┬───┘
               ▼
    ┌─────────────────────┐
    │  Create/Get Client  │
    │  (Cache if needed)  │
    └─────────┬───────────┘
              ▼
        Github Instance
```

## Implementation Details

### Changes Made

1. **`auth.py`** (New File)
   - `AuthManager` class for token management
   - Single/multi-token initialization
   - Dynamic token management
   - Token verification
   - Client caching

2. **`forker.py`** (Enhanced)
   - Updated `__init__` to accept dict or AuthManager
   - `_get_github_client()` method to select token per owner
   - Enhanced `_resolve_source()` to use owner-specific client
   - Updated `_find_existing_fork()` to use correct client

### Backwards Compatibility

✅ **100% backwards compatible**
- Existing code with single token continues to work
- No changes needed to existing implementations
- New multi-auth is opt-in

## Error Handling

### Missing Token

```python
auth = AuthManager({"org-a": "ghp_token_a"})  # No default

try:
    gh = auth.get_client("org-unknown")
except ValueError as e:
    # "No authentication token available for owner 'org-unknown'.
    # Available owners: ['org-a']. No default token configured."
    print(e)
```

### Invalid Token

```python
auth = AuthManager({"org-a": "ghp_invalid_token"})

if not auth.verify_owner_access("org-a"):
    print("Token is invalid or expired")
    auth.add_owner_token("org-a", "ghp_new_token")
    auth.clear_cache()  # Force new client
```

## Configuration Patterns

### Pattern 1: Organization-Based

```python
auth = AuthManager({
    "engineering": "ghp_eng_token",
    "product": "ghp_prod_token",
    "data": "ghp_data_token",
})
```

### Pattern 2: Project-Based

```python
auth = AuthManager({
    "project-alpha": "ghp_alpha_token",
    "project-beta": "ghp_beta_token",
    "default": "ghp_fallback",
})
```

### Pattern 3: Environment-Based

```python
import os
from pygithub_fork import AuthManager

auth_map = {
    os.getenv("ORG_A"): os.getenv("TOKEN_A"),
    os.getenv("ORG_B"): os.getenv("TOKEN_B"),
    "default": os.getenv("DEFAULT_TOKEN"),
}

auth = AuthManager(auth_map)
forker = GitHubForker(auth)
```

## Testing

```python
from pygithub_fork import GitHubForker, AuthManager

def test_multi_owner_forking():
    """Test forking from multiple owners with different tokens."""
    auth = AuthManager({
        "org-a": "ghp_token_a",
        "org-b": "ghp_token_b",
        "default": "ghp_default",
    })
    
    # Verify all tokens
    assert auth.verify_owner_access("org-a")
    assert auth.verify_owner_access("org-b")
    
    forker = GitHubForker(auth)
    
    # Fork from different owners
    result_a = forker.fork("org-a/private-repo", private=True)
    assert result_a.succeeded
    
    result_b = forker.fork("org-b/private-repo", private=True)
    assert result_b.succeeded

def test_dynamic_token_management():
    """Test adding/updating tokens dynamically."""
    auth = AuthManager({"org-a": "ghp_token_a"})
    
    # Initially org-b fails
    assert not auth.verify_owner_access("org-b")
    
    # Add token for org-b
    auth.add_owner_token("org-b", "ghp_token_b")
    assert auth.verify_owner_access("org-b")
```

## Performance Considerations

### Client Caching

Clients are cached per owner to avoid recreating Github instances:

```python
auth = AuthManager({"org-a": "ghp_token_a"})

# First call: creates and caches client
gh1 = auth.get_client("org-a")

# Second call: returns cached client (fast)
gh2 = auth.get_client("org-a")

assert gh1 is gh2  # Same instance
```

### Clearing Cache

Clear cached clients if tokens change:

```python
auth.add_owner_token("org-a", "ghp_new_token")
# Cached client is automatically invalidated

auth.set_default_token("ghp_new_default")
auth.clear_cache()  # Invalidates all cached clients
```

## Migration Guide

### From Single Token to Multi-Auth

**Before:**
```python
forker = GitHubForker("ghp_single_token")
result = forker.fork("owner/repo")
```

**After (Option 1 - Keep existing):**
```python
forker = GitHubForker("ghp_single_token")  # Still works
result = forker.fork("owner/repo")
```

**After (Option 2 - Use multi-auth):**
```python
auth = AuthManager({
    "org-a": "ghp_token_org_a",
    "org-b": "ghp_token_org_b",
    "default": "ghp_single_token",  # Fallback
})
forker = GitHubForker(auth)
result = forker.fork("owner/repo")
```

## Troubleshooting

### Issue: "No authentication token available for owner"

**Solution**: Add a default token or owner-specific token

```python
# Option 1: Add default
auth.set_default_token("ghp_fallback_token")

# Option 2: Add owner token
auth.add_owner_token("unknown-org", "ghp_new_token")
```

### Issue: Token Expired

**Solution**: Verify and update tokens

```python
if not auth.verify_owner_access("org-a"):
    auth.add_owner_token("org-a", "ghp_new_token")
    auth.clear_cache()
```

## References

- [GitHub API Authentication](https://docs.github.com/en/rest/overview/authenticating-to-the-rest-api)
- [Personal Access Tokens](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/creating-a-personal-access-token)
- [PyGithub Documentation](https://pygithub.readthedocs.io/)
