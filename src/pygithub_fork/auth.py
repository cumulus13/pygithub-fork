"""
pygithub_fork.auth
~~~~~~~~~~~~~~~~~~
Authentication management for multi-owner forking.
"""
from __future__ import annotations

from typing import Dict, Optional, Union
from github import Github, GithubException


class AuthManager:
    """
    Manages multiple GitHub authentication tokens for different owners/organizations.
    
    Enables forking from repositories owned by different users/organizations
    using owner-specific authentication tokens.
    
    Example::
    
        auth_map = {
            "org-a": "ghp_token_org_a",
            "org-b": "ghp_token_org_b",
            "default": "ghp_token_default",
        }
        auth_manager = AuthManager(auth_map)
        
        # Get appropriate client for owner
        gh = auth_manager.get_client("org-a")
        # Uses ghp_token_org_a
        
        gh = auth_manager.get_client("org-c")
        # Falls back to default token
    """
    
    def __init__(
        self,
        auth_map: Union[str, Dict[str, str]],
        default_token: Optional[str] = None,
    ) -> None:
        """
        Initialize authentication manager.
        
        Parameters
        ----------
        auth_map : str or dict
            Either:
            - A single GitHub token (str) - uses as default for all owners
            - A dict mapping owner names to tokens:
              {"owner-a": "ghp_token_a", "owner-b": "ghp_token_b"}
            - A dict with optional "default" key for fallback token
        default_token : str, optional
            Fallback token if owner not found in auth_map.
            If not provided and auth_map is a dict, no fallback is used.
        
        Example::
        
            # Single token (works for all owners)
            auth = AuthManager("ghp_single_token")
            
            # Multiple tokens with fallback
            auth = AuthManager(
                {"owner-a": "ghp_token_a"},
                default_token="ghp_token_default"
            )
            
            # Multiple tokens with built-in default
            auth = AuthManager({
                "owner-a": "ghp_token_a",
                "owner-b": "ghp_token_b",
                "default": "ghp_token_default",
            })
        """
        self._clients: Dict[str, Github] = {}
        self._tokens: Dict[str, str] = {}
        self._default_token: Optional[str] = None
        
        if isinstance(auth_map, str):
            # Single token - use for all owners
            self._default_token = auth_map
        elif isinstance(auth_map, dict):
            # Multiple tokens with optional default
            if "default" in auth_map:
                self._default_token = auth_map["default"]
                # Store all non-default tokens
                self._tokens = {k: v for k, v in auth_map.items() if k != "default"}
            else:
                self._tokens = dict(auth_map)
            
            # Override with explicit default_token if provided
            if default_token is not None:
                self._default_token = default_token
        else:
            raise TypeError(
                "auth_map must be a string (token) or dict (owner->token mapping)"
            )
    
    def get_client(self, owner: str) -> Github:
        """
        Get authenticated GitHub client for the specified owner.
        
        Returns owner-specific token if available, falls back to default token.
        Caches clients for reuse.
        
        Parameters
        ----------
        owner : str
            Repository owner (username or organization)
        
        Returns
        -------
        github.Github
            Authenticated PyGithub client
        
        Raises
        ------
        ValueError
            If no token available for owner and no default token set
        
        Example::
        
            auth = AuthManager({"org-a": "ghp_token_a"})
            
            # Get client for org-a
            gh_a = auth.get_client("org-a")
            
            # Get default client (if default token set)
            gh = auth.get_client("unknown-owner")
        """
        # Return cached client if available
        if owner in self._clients:
            return self._clients[owner]
        
        # Get token for this owner
        token = self._get_token_for_owner(owner)
        
        # Create and cache client
        client = Github(token)
        self._clients[owner] = client
        
        return client
    
    def _get_token_for_owner(self, owner: str) -> str:
        """
        Get token for the specified owner, with fallback to default.
        
        Parameters
        ----------
        owner : str
            Repository owner
        
        Returns
        -------
        str
            GitHub authentication token
        
        Raises
        ------
        ValueError
            If no token available for owner and no default token set
        """
        # Check owner-specific token first
        if owner in self._tokens:
            return self._tokens[owner]
        
        # Fall back to default token
        if self._default_token is not None:
            return self._default_token
        
        # No token available
        raise ValueError(
            f"No authentication token available for owner '{owner}'. "
            f"Available owners: {list(self._tokens.keys())}. "
            f"No default token configured."
        )
    
    def add_owner_token(self, owner: str, token: str) -> None:
        """
        Add or update authentication token for an owner.
        
        Invalidates cached client for this owner if it exists.
        
        Parameters
        ----------
        owner : str
            Repository owner (username or organization)
        token : str
            GitHub authentication token for this owner
        
        Example::
        
            auth = AuthManager({"org-a": "ghp_token_a"})
            
            # Add token for new owner
            auth.add_owner_token("org-b", "ghp_token_b")
        """
        self._tokens[owner] = token
        # Invalidate cached client
        if owner in self._clients:
            del self._clients[owner]
    
    def set_default_token(self, token: str) -> None:
        """
        Set or update the default fallback token.
        
        Invalidates all cached clients since default affects all owners.
        
        Parameters
        ----------
        token : str
            GitHub authentication token to use as default
        
        Example::
        
            auth = AuthManager({"org-a": "ghp_token_a"})
            auth.set_default_token("ghp_token_default")
        """
        self._default_token = token
        # Invalidate all cached clients
        self._clients.clear()
    
    def get_available_owners(self) -> list[str]:
        """
        Get list of explicitly configured owner tokens.
        
        Returns
        -------
        list[str]
            List of owners with explicit tokens (excludes default)
        
        Example::
        
            auth = AuthManager({
                "org-a": "ghp_token_a",
                "org-b": "ghp_token_b",
            })
            
            owners = auth.get_available_owners()
            # Returns: ["org-a", "org-b"]
        """
        return list(self._tokens.keys())
    
    def has_default_token(self) -> bool:
        """
        Check if default fallback token is configured.
        
        Returns
        -------
        bool
            True if default token is set
        
        Example::
        
            auth = AuthManager({"org-a": "ghp_token_a"})
            
            if not auth.has_default_token():
                print("No default token - will fail for unknown owners")
        """
        return self._default_token is not None
    
    def verify_owner_access(self, owner: str) -> bool:
        """
        Verify that token for owner can authenticate with GitHub.
        
        Attempts to fetch authenticated user info to validate token.
        
        Parameters
        ----------
        owner : str
            Repository owner to verify
        
        Returns
        -------
        bool
            True if token is valid and has GitHub access
        
        Example::
        
            auth = AuthManager({"org-a": "ghp_token_a"})
            
            if auth.verify_owner_access("org-a"):
                print("Token is valid")
            else:
                print("Token is invalid or expired")
        """
        try:
            gh = self.get_client(owner)
            # Try to fetch authenticated user
            gh.get_user().login
            return True
        except (GithubException, Exception):
            return False
    
    def clear_cache(self) -> None:
        """
        Clear cached GitHub client instances.
        
        Useful if token credentials have changed or expired.
        
        Example::
        
            auth = AuthManager({"org-a": "ghp_token_a"})
            auth.clear_cache()  # Force new clients on next get_client() call
        """
        self._clients.clear()
