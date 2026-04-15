"""
Standalone test CLI for the DLP analyzer.

Usage
-----
  # Analyze a plain-text string:
  python test_cli.py --text "4111111111111111 credit card" --channel clipboard

  # Analyze a file:
  python test_cli.py --file path/to/file.docx --channel peripheral

  # Show timing information:
  python test_cli.py --text "nội bộ" --debug
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running from the analyzer directory or from the repo root.
_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))

from engine import DLPEngine
from extractor import extract_text

_POLICY_FILE = _HERE / "policies.yaml"

_ACTION_COLOR = {
    "block":     "\033[31m",   # red
    "allow_log": "\033[33m",   # yellow
    "allow":     "\033[32m",   # green
}
_RESET = "\033[0m"


def _colorize(action: str) -> str:
    color = _ACTION_COLOR.get(action, "")
    return f"{color}{action.upper()}{_RESET}"


def _print_result(result, text: str, debug: bool) -> None:
    print(f"\nApplied action: {_colorize(result.applied_action)}")
    if debug:
        print(f"Elapsed       : {result.elapsed_ms:.2f} ms")

    if not result.violations:
        print("No violations found.")
        return

    print(f"\nViolations ({len(result.violations)}):")
    for v in result.violations:
        print(f"\n  [{_colorize(v.action)}] {v.policy_name} ({v.policy_id})")
        print(f"  Matches: {len(v.matches)}")
        for m in v.matches:
            snippet = text[max(0, m.start - 20):m.end + 20].replace("\n", " ")
            print(f"    [{m.start}:{m.end}] …{snippet}…")
            print(f"           matched: {repr(m.text)}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="DLP Analyzer – manual test interface",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--text", metavar="TEXT", help="Plain text to analyze")
    group.add_argument("--file", metavar="PATH", help="File to extract and analyze")

    parser.add_argument(
        "--channel",
        choices=["clipboard", "browser", "peripheral"],
        default="clipboard",
        help="Interceptor channel (default: clipboard)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Show timing and extra diagnostic info",
    )
    parser.add_argument(
        "--policy",
        metavar="PATH",
        default=str(_POLICY_FILE),
        help="Path to policies.yaml (default: analyzer/policies.yaml)",
    )

    args = parser.parse_args()

    # Load engine
    engine = DLPEngine(args.policy)

    # Get text
    if args.text is not None:
        text = args.text
        source_label = "<inline text>"
    else:
        file_path = Path(args.file)
        if not file_path.exists():
            print(f"Error: file not found: {file_path}", file=sys.stderr)
            sys.exit(1)
        if args.debug:
            print(f"Extracting text from: {file_path}")
        text = extract_text(file_path)
        source_label = str(file_path)
        if args.debug:
            print(f"Extracted {len(text)} characters")

    print(f"Source : {source_label}")
    print(f"Channel: {args.channel}")

    result = engine.analyze(text, args.channel)
    _print_result(result, text, args.debug)


if __name__ == "__main__":
    main()
