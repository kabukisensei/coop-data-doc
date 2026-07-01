"""Filesystem-safe page naming and link paths (Module 5).

Every link generator derives a node's page filename from :func:`slug`, a
pure function of the node id, so links stay consistent across the Markdown
and HTML renderers.
"""

from __future__ import annotations

import hashlib
import re

from coop_data_doc.graph.model import Node

# Characters illegal in Windows filenames (plus control chars). Names also
# can't end in a dot/space on Windows, and must stay well under MAX_PATH.
_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_COLLAPSE_DASH = re.compile(r"-{2,}")
_SLUG_MAX = 80  # readable portion; a short id-hash is always appended


def slug(node_id: str) -> str:
    """Filesystem-safe, length-bounded, collision-free page name for a node id.

    A readable portion (the id's name, with every filesystem-illegal
    character — ``< > : " / \\ | ? *`` and control chars — plus dots and
    spaces replaced by ``-``) is followed by a short deterministic hash of
    the full id. The hash guarantees uniqueness (two distinct ids never
    collide to one filename) and keeps names safe on Windows, where the
    original crashed on characters like ``|`` in DAX measure names. Pure
    function of the id, so every link generator stays consistent.
    """
    name_part = node_id.split(":", 1)[1] if ":" in node_id else node_id
    safe = _UNSAFE_CHARS.sub("-", name_part)
    safe = safe.replace(".", "-").replace(" ", "-")
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
