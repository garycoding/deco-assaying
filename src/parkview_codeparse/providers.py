"""Hosting-provider dispatcher.

Each provider module (`github`, `gitlab`) exposes the same five names —
`NAME`, `TOKEN_ENV`, `parse_url`, `env_token`, `fetch_default_branch`,
`fetch_blob_sizes`, `fetch_blob_via_raw`. `for_url(url)` matches a
URL string against the registered providers and returns the resolved
provider module plus the parsed `(owner, repo)` tuple.

Adding a new provider (Bitbucket, Codeberg, self-hosted Gitea, ...) is
a matter of writing one module that exposes the same surface and
appending it to `_PROVIDERS` below.
"""

from __future__ import annotations

from types import ModuleType

from parkview_codeparse import github, gitlab

_PROVIDERS: tuple[ModuleType, ...] = (github, gitlab)


def for_url(url: str) -> tuple[ModuleType, str, str] | None:
    """If `url` belongs to a known provider, return `(module, owner, repo)`.

    `owner` may contain `/` for providers (like GitLab) that nest groups.
    """
    for provider in _PROVIDERS:
        match = provider.parse_url(url)
        if match is not None:
            owner, repo = match
            return provider, owner, repo
    return None


def is_repo_url(url: str) -> bool:
    """Cheap test: does any registered provider claim this URL?"""
    return for_url(url) is not None
