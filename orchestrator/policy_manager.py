from __future__ import annotations

import logging
import os
from pathlib import Path

from analyzer.engine import DLPEngine
from analyzer.extractor import extract_tabular, extract_text, is_tabular
from orchestrator.config import OrchestratorConfig

log = logging.getLogger(__name__)


class PolicyManager:
    def __init__(self, config: OrchestratorConfig) -> None:
        repo_root = Path(__file__).parent.parent
        policies_path = repo_root / config.policies_file
        log.info("Loading policies from %s", policies_path)
        self._engine = DLPEngine(str(policies_path))
        log.info("DLPEngine ready.")

    def get_engine(self) -> DLPEngine:
        return self._engine

    def analyze(
        self,
        channel: str,
        kind: str,
        text: str | None = None,
        file_path: str | None = None,
    ) -> tuple[str, list]:
        if kind == "text":
            result = self._engine.analyze(text or "", channel)
        elif kind == "file":
            if not file_path:
                log.error("kind=file but no file_path provided; failing closed.")
                return "BLOCK", []
            try:
                if is_tabular(file_path):
                    result = self._engine.analyze_tabular(extract_tabular(file_path), channel)
                else:
                    result = self._engine.analyze(extract_text(file_path), channel)
            finally:
                try:
                    os.unlink(file_path)
                except OSError as e:
                    log.warning("Could not delete temp file %s: %s", file_path, e)
        else:
            log.error("Unknown kind=%r; failing closed.", kind)
            return "BLOCK", []

        action = result.applied_action
        if action == "block":
            log.warning(
                "BLOCK | channel=%s | elapsed=%.1fms | violations=%s",
                channel, result.elapsed_ms, _fmt_violations(result.violations),
            )
            return "BLOCK", result.violations
        elif action == "allow_log":
            log.info(
                "ALLOW (logged) | channel=%s | elapsed=%.1fms | violations=%s",
                channel, result.elapsed_ms, _fmt_violations(result.violations),
            )
            return "ALLOW", result.violations
        else:
            log.debug("ALLOW | channel=%s | elapsed=%.1fms", channel, result.elapsed_ms)
            return "ALLOW", result.violations


def _fmt_violations(violations: list) -> str:
    return ", ".join(f"{v.policy_id}({v.action})" for v in violations)
