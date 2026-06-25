"""End-user-facing block messages.

A single home for the human text shown to the endpoint user when a request is
blocked. Two kinds of text exist:

* **Per-policy** reasons — the admin-editable ``user_message`` on each policy in
  ``analyzer/policies.yaml`` (hot-reloadable via ``dlp-ctl reload``). Used for
  ``policy_violation`` blocks. Not defined here.
* **Per-category** failure messages — the table below, used when analysis cannot
  complete (oversize / text-cap / unsupported-format / timeout / analysis-error /
  malformed). These are generic and reveal no internals. They live in code, so a
  wording change applies on the next **service restart** (consistent with the
  other orchestrator-side settings, which are not rebuilt by ``dlp-ctl reload``).

The dispatcher maps an outcome to one ``category`` token (logged to
``events.jsonl`` as ``reason``) and one user message; this module owns the
``category → message`` half. The category tokens are the same strings the
dispatcher records, so the audit log and the user message stay in lock-step.

Default language is English; edit the strings here to re-word or localize.
"""
from __future__ import annotations

# Generic fallback when a matched block policy has no ``user_message`` set.
GENERIC_POLICY_MESSAGE = "Sensitive data detected"

# category token -> end-user message. Keys mirror the dispatcher's event `reason`
# tokens for the analysis-failure paths (policy_violation is handled separately,
# from the policy's own user_message).
FAILURE_MESSAGES: dict[str, str] = {
    "oversize": "File exceeds the maximum allowed size",
    "text_cap": "Content is too large to scan",
    "unsupported_format": "File type is not supported",
    "timeout": "Scan timed out, please try again",
    "analysis_error": "Unable to scan the content",
    "malformed": "Invalid request",
}

# Last-resort message if an unknown category ever reaches here.
_UNKNOWN_FAILURE_MESSAGE = "Unable to scan the content"


def failure_message(category: str) -> str:
    """End-user message for an analysis-failure *category*. Unknown categories
    fall back to a safe generic message (a block must always carry SOME reason)."""
    return FAILURE_MESSAGES.get(category, _UNKNOWN_FAILURE_MESSAGE)
