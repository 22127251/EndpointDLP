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

Default language is Vietnamese (the endpoint users); edit the strings here to
re-word or localize.
"""
from __future__ import annotations

# Generic fallback when a matched block policy has no ``user_message`` set.
GENERIC_POLICY_MESSAGE = "Phát hiện dữ liệu nhạy cảm"

# category token -> end-user message. Keys mirror the dispatcher's event `reason`
# tokens for the analysis-failure paths (policy_violation is handled separately,
# from the policy's own user_message).
FAILURE_MESSAGES: dict[str, str] = {
    "oversize": "Tệp vượt quá kích thước cho phép",
    "text_cap": "Tệp quá lớn để quét nội dung",
    "unsupported_format": "Định dạng tệp không được hỗ trợ",
    "timeout": "Quá thời gian quét, vui lòng thử lại",
    "analysis_error": "Không thể quét tệp",
    "malformed": "Yêu cầu không hợp lệ",
}

# Last-resort message if an unknown category ever reaches here.
_UNKNOWN_FAILURE_MESSAGE = "Không thể quét tệp"


def failure_message(category: str) -> str:
    """End-user message for an analysis-failure *category*. Unknown categories
    fall back to a safe generic message (a block must always carry SOME reason)."""
    return FAILURE_MESSAGES.get(category, _UNKNOWN_FAILURE_MESSAGE)
