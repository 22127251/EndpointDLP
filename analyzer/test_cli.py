"""
Standalone test CLI for the DLP analyzer.

Usage
-----
  # Analyze a plain-text string:
  python test_cli.py --text "4111111111111111 credit card" --channel clipboard

  # Analyze a file (auto-detects tabular vs plain-text):
  python test_cli.py --file path/to/file.docx --channel peripheral
  python test_cli.py --file path/to/data.csv --channel browser
  python test_cli.py --file path/to/report.pdf --channel peripheral

  # Show timing and extraction details:
  python test_cli.py --file data.xlsx --debug
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running from the analyzer directory or from the repo root.
_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))

from engine import AnalysisResult, DLPEngine, Match
from extractor import extract_tabular, extract_text, is_tabular

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


def _format_match(m: Match, plain_text: str | None) -> str:
    """Return a single formatted line describing one match."""
    if m.column_name is not None:
        # Tabular match — show location by sheet/column/row
        parts: list[str] = []
        if m.sheet:
            parts.append(f"Sheet: {m.sheet}")
        parts.append(f"Col: {m.column_name}" if m.column_name else "Col: (body)")
        if m.row is not None:
            parts.append(f"Row: {m.row}")
        location = " | ".join(parts)
        return f"    [{location}] → {repr(m.text)}"
    else:
        # Plain-text match — show character span and snippet
        snippet = ""
        if plain_text is not None and m.start is not None and m.end is not None:
            s = max(0, m.start - 20)
            snippet = f"  …{plain_text[s:m.end + 20].replace(chr(10), ' ')}…"
        return f"    [{m.start}:{m.end}]{snippet}\n           matched: {repr(m.text)}"


def _print_result(result: AnalysisResult, plain_text: str | None, debug: bool) -> None:
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
            print(_format_match(m, plain_text))


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

    engine = DLPEngine(args.policy)

    plain_text: str | None = None
    source_label: str

    if args.text is not None:
        plain_text = args.text
        source_label = "<inline text>"
        result = engine.analyze(plain_text, args.channel)

    else:
        file_path = Path(args.file)
        if not file_path.exists():
            print(f"Error: file not found: {file_path}", file=sys.stderr)
            sys.exit(1)
        source_label = str(file_path)

        if is_tabular(file_path):
            if args.debug:
                print(f"Mode   : tabular extraction ({file_path.suffix})")
            tabular = extract_tabular(file_path)
            if args.debug:
                print(f"Columns: {len(tabular.columns)}")
            result = engine.analyze_tabular(tabular, args.channel)
        else:
            if args.debug:
                print(f"Mode   : plain-text extraction ({file_path.suffix})")
            plain_text = extract_text(file_path)
            if args.debug:
                print(f"Extracted {len(plain_text)} characters")
            result = engine.analyze(plain_text, args.channel)

    print(f"Source : {source_label}")
    print(f"Channel: {args.channel}")

    _print_result(result, plain_text, args.debug)


if __name__ == "__main__":
    main()
