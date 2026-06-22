"""Supported-format gate.

A file whose extension is NOT in ``analyzer.supported_extensions`` is refused
BEFORE extraction (so an untested/binary type is never scanned as garbage text)
and follows the channel's ``failure_mode`` (``reason=unsupported_format``), just
like the oversize / text_cap paths. A supported extension is analyzed normally.

Only the FILE channels (browser, peripheral_storage) consult the gate; clipboard
text has no extension.
"""
from __future__ import annotations

import uuid

import pytest

from pipe_helpers import pipe_send

from orchestrator import messages

_FILE_CHANNELS = ("browser", "peripheral_storage")

# Flip the file channels to fail_open (peripheral via its nested transfer_agent
# subtree, mirroring test_failure_mode).
_FAIL_OPEN = {
    "browser": {"failure_mode": "fail_open"},
    "peripheral_storage": {"transfer_agent": {"failure_mode": "fail_open"}},
}


@pytest.mark.parametrize("fail_open,expected", [(False, "BLOCK"), (True, "ALLOW")])
def test_unsupported_extension_follows_failure_mode(make_orchestrator, fail_open, expected):
    orch = make_orchestrator(
        policies_fixture="permissive.yaml",
        config_overrides=(dict(_FAIL_OPEN) if fail_open else None),
    )
    bad = orch.tmp_dir / f"payload-{uuid.uuid4().hex}.exe"
    for channel in _FILE_CHANNELS:
        # The orchestrator deletes the analyzed temp file, so rewrite per channel.
        bad.write_bytes(b"MZ\x90\x00 not a document")
        decision, reason = pipe_send(
            orch.pipe_name,
            {"channel": channel, "kind": "file", "file_path": str(bad), "metadata": {}},
            timeout_seconds=8.0)
        assert decision == expected, f"{channel}: expected {expected}, got {decision!r}"
        if expected == "BLOCK":
            assert reason == messages.FAILURE_MESSAGES["unsupported_format"]


def test_unknown_extension_is_unsupported(make_orchestrator):
    # A genuinely unknown extension (not the textual-fallback set) is refused too
    # (the old behavior read any unknown extension as plaintext).
    orch = make_orchestrator(policies_fixture="permissive.yaml")
    weird = orch.tmp_dir / f"data-{uuid.uuid4().hex}.zzz"
    weird.write_text("credit card 4111 1111 1111 1111", encoding="utf-8")
    decision, reason = pipe_send(
        orch.pipe_name,
        {"channel": "browser", "kind": "file", "file_path": str(weird), "metadata": {}},
        timeout_seconds=8.0)
    assert decision == "BLOCK"   # fail_closed default
    assert reason == messages.FAILURE_MESSAGES["unsupported_format"]


def test_extensionless_file_is_analyzed_not_refused(make_orchestrator):
    # Gmail uploads arrive as "upload" with NO extension. An empty extension must
    # fall through to analysis (plaintext), NOT be refused as unsupported — else
    # every Gmail .txt/.csv/.md upload would be wrongly blocked. ALLOW under the
    # permissive policy proves it reached the analyzer (a refusal would BLOCK by
    # the fail_closed default).
    orch = make_orchestrator(policies_fixture="permissive.yaml")
    noext = orch.tmp_dir / "upload"   # no extension, like a Gmail upload temp
    noext.write_text("just some plain text, nothing sensitive", encoding="utf-8")
    decision, reason = pipe_send(
        orch.pipe_name,
        {"channel": "browser", "kind": "file", "file_path": str(noext), "metadata": {}},
        timeout_seconds=8.0)
    assert decision == "ALLOW", f"extensionless file should be analyzed, got {decision!r}/{reason!r}"


def test_extensionless_file_with_pii_is_blocked_by_policy(make_orchestrator):
    # The same extensionless upload carrying a card+context must still be caught
    # by policy (recall preserved via the plaintext path), blocked with the policy
    # reason — NOT the unsupported_format failure message.
    orch = make_orchestrator(policies_fixture="visa_block.yaml")
    noext = orch.tmp_dir / "upload"
    noext.write_text("credit card 4111 1111 1111 1111", encoding="utf-8")
    decision, reason = pipe_send(
        orch.pipe_name,
        {"channel": "browser", "kind": "file", "file_path": str(noext), "metadata": {}},
        timeout_seconds=8.0)
    assert decision == "BLOCK"
    assert reason != messages.FAILURE_MESSAGES["unsupported_format"]


def test_supported_extension_is_analyzed(make_orchestrator):
    # A supported .txt with benign content is ALLOWed under the permissive policy,
    # proving the gate does not block supported types.
    orch = make_orchestrator(policies_fixture="permissive.yaml")
    good = orch.tmp_dir / f"doc-{uuid.uuid4().hex}.txt"
    good.write_text("hello world, nothing sensitive here", encoding="utf-8")
    decision, _reason = pipe_send(
        orch.pipe_name,
        {"channel": "browser", "kind": "file", "file_path": str(good), "metadata": {}},
        timeout_seconds=8.0)
    assert decision == "ALLOW"
