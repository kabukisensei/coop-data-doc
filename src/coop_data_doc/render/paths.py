"""Filesystem-safe page naming and link paths (Module 5).

Every link generator derives a node's page filename from :func:`slug`, a
pure function of the node id, so links stay consistent across the Markdown
and HTML renderers.
"""

from __future__ import annotations

import hashlib
import re

from coop_data_doc.graph.model import Node

# The readable slug portion is narrowed to a single URL- AND filesystem-safe
# class: only ``[a-z0-9_-]`` survives (everything else, including dots and
# spaces, becomes ``-``). This subsumes the Windows-illegal set
# (``< > : " / \ | ? *`` and control chars, which originally crashed the page
# writer on ``|`` in DAX measure names) AND the URL-hostile set
# (``% + ( ) & # ?``): a raw ``%`` is an invalid percent-escape a strict server
# rejects, an unbalanced ``)`` closes a Markdown link target early, and ``+``
# flips to a space through query-string contexts. Node ids are already
# lowercased by ``normalize_identifier``, so real slugs never lose case here.
_UNSAFE_SLUG_CHARS = re.compile(r"[^a-z0-9_-]")
_COLLAPSE_DASH = re.compile(r"-{2,}")
_SLUG_MAX = 80  # readable portion; a short id-hash is always appended


def slug(node_id: str) -> str:
    """Filesystem- and URL-safe, length-bounded, collision-free page name.

    A readable portion (the id's name, with every character outside
    ``[a-z0-9_-]`` — filesystem-illegal ``< > : " / \\ | ? *`` and control
    chars, URL-hostile ``% + ( ) & # ?``, plus dots and spaces — replaced by
    ``-``) is followed by a short deterministic hash of the full id. The hash
    guarantees uniqueness (two distinct ids never collide to one filename) and
    keeps names safe on Windows and in the built HTML/Markdown links. Pure
    function of the id, so every link generator stays consistent.
    """
    name_part = node_id.split(":", 1)[1] if ":" in node_id else node_id
    safe = _UNSAFE_SLUG_CHARS.sub("-", name_part)
    safe = _COLLAPSE_DASH.sub("-", safe).strip("-. ")
    if len(safe) > _SLUG_MAX:
        safe = safe[:_SLUG_MAX].strip("-. ")
    if not safe:
        safe = node_id.split(":", 1)[0]  # fall back to the node type
    digest = hashlib.sha1(node_id.encode("utf-8")).hexdigest()[:8]
    return f"{safe}-{digest}"


def doc_relpath(node: Node) -> str:
    """Markdown-link path of a node's page, relative to another node's page.

    Used in Markdown link syntax `[x](...)`, which MkDocs rewrites to the
    built URL — so this keeps the `.md` suffix.
    """
    return f"../{node.node_type.value}/{slug(node.id)}.md"
