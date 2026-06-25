"""
Standalone DLP analyzer tester — runs the analyzer over a corpus WITHOUT the
orchestrator/pipes, so you can validate detection on your dev machine before
deploying the whole agent.

Lives outside the `analyzer` package on purpose; it imports the analyzer modules
by adding ../analyzer to sys.path.

For each file it:
  * routes via is_tabular() and extracts + analyzes exactly like the real agent;
  * counts detected PII per TYPE (VISA / CCCD / phone) and compares them to an
    independent verify.py-style regex oracle on the same extracted text, and to
    the expected per-bracket count (b1=10 / b2=30 / b3=80) parsed from the name;
  * records timing.

Outputs (all UTF-8, under --out, default manual_test/iso_test_out/):
  * summary.txt          — the PASS/FAIL table (also printed to stdout)
  * matches.csv          — ONE file for the whole run; one row per match:
                           file, format, location, type, value, policy_id,
                           has_context, score, action
  * <stem>.annotated.txt — plain files (txt/md/pdf): the (normalized) extracted
                           text with  policy_id|context=yes/no  after each match
  * <stem>.extracted.txt — tabular files (csv/docx/xlsx/ods/odt): a line-per-match
                           rendering of the extracted cells with the same tag

Usage:
  .venv\\Scripts\\python.exe manual_test\\iso_test.py --corpus tmp\\final-demo\\deny
  .venv\\Scripts\\python.exe manual_test\\iso_test.py --corpus tmp\\final-demo\\deny --channel browser
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
sys.path.insert(0, str(_REPO / "analyzer"))

from engine import DLPEngine, normalize_ws            # noqa: E402
from extractor import (                                # noqa: E402
    ExtractionTooLarge, extract_tabular, extract_text, is_tabular)

# --- verify.py-style oracle (independent of the engine) ----------------------
RE_VISA = re.compile(r"(?<!\d)4\d{3} ?\d{4} ?\d{4} ?\d{4}(?!\d)")
RE_CCCD = re.compile(r"(?<!\d)\d{12}(?!\d)")
RE_PHONE = [
    re.compile(r"(?<!\d)0\d{2} \d{3} \d{4}(?!\d)"),
    re.compile(r"(?<!\d)\+84\d{9}(?!\d)"),
    re.compile(r"(?<!\d)0\d{9}(?!\d)"),
]
_WS = re.compile(r"\s+")

_EXPECTED = {"b1": 10, "b2": 30, "b3": 80}
_EXTS = (".csv", ".tsv", ".txt", ".md", ".docx", ".odt", ".xlsx", ".ods", ".pdf")


def oracle_counts(text: str) -> dict[str, int]:
    t = _WS.sub(" ", text)
    return {
        "VISA": len(RE_VISA.findall(t)),
        "CCCD": len(RE_CCCD.findall(t)),
        "PHONE": sum(len(p.findall(t)) for p in RE_PHONE),
    }


def pii_type(policy_id: str) -> str | None:
    if "visa" in policy_id:
        return "VISA"
    if "cccd" in policy_id:
        return "CCCD"
    if "phone" in policy_id:
        return "PHONE"
    return None  # confidential / other — not part of the V/C/P comparison


def _annotate_plain(text: str, violations, include_other: bool) -> str:
    tags = []
    for v in violations:
        if not include_other and pii_type(v.policy_id) is None:
            continue
        for m in v.matches:
            if m.start is not None:
                tags.append((m.start, m.end, v.policy_id, m.context_word))
    tags.sort(key=lambda t: t[0])
    out, prev = [], 0
    for s, e, pid, cw in tags:
        if s < prev:        # overlapping match (rare) — skip to keep offsets sane
            continue
        out.append(text[prev:e])
        out.append(f"⟦{pid}|context={cw or 'no'}⟧")
        prev = e
    out.append(text[prev:])
    return "".join(out)


def _render_tabular(violations, include_other: bool) -> str:
    lines = []
    for v in violations:
        if not include_other and pii_type(v.policy_id) is None:
            continue
        for m in v.matches:
            if m.start is not None:   # free-text body match (proximity), not a table cell
                loc = f"body | offset {m.start}-{m.end}"
            else:
                loc = f"sheet={m.sheet or '-'} | col={m.column_name or '(body)'} | row={m.row}"
            lines.append(f"[{loc}] {m.text}  ⟦{v.policy_id}|context={m.context_word or 'no'}⟧")
    lines.sort()
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Standalone DLP analyzer tester")
    ap.add_argument("--corpus", default=str(_REPO / "tmp" / "final-demo" / "deny"),
                    help="folder of files to analyze (searched recursively)")
    ap.add_argument("--out", default=str(_HERE / "iso_test_out"),
                    help="output folder for matches.csv / annotated files / summary.txt")
    ap.add_argument("--channel", default="browser",
                    choices=["clipboard", "browser", "peripheral", "peripheral_storage"])
    ap.add_argument("--policy", default=str(_REPO / "analyzer" / "policies.yaml"))
    ap.add_argument("--all", action="store_true",
                    help="include non-PII (confidential keyword) matches in the per-match "
                         "outputs; by default they are only counted in the summary to keep "
                         "matches.csv focused on VISA/CCCD/phone")
    ap.add_argument("--max-chars", type=int, default=None,
                    help="extracted-text cap (chars), mirroring analyzer.max_extracted_chars. "
                         "Files over it are refused at extraction and counted as a valid BLOCK "
                         "(not a parity check). Default: no cap (full-parity regression gate).")
    args = ap.parse_args()

    channel = "peripheral_storage" if args.channel == "peripheral" else args.channel
    corpus = Path(args.corpus)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    engine = DLPEngine(args.policy)
    files = sorted(p for p in corpus.rglob("*")
                   if p.is_file() and p.suffix.lower() in _EXTS and not p.name.startswith("~$"))
    if not files:
        print(f"No files found under {corpus}", file=sys.stderr)
        return 1

    csv_path = out / "matches.csv"
    summary_rows = []
    all_pass = True

    with open(csv_path, "w", encoding="utf-8", newline="") as cf:
        w = csv.writer(cf)
        w.writerow(["file", "format", "location", "type", "value",
                    "policy_id", "has_context", "context_word", "score", "action"])

        for f in files:
            fmt = f.suffix.lower().lstrip(".")
            t0 = time.perf_counter()
            try:
                if is_tabular(f):
                    td = extract_tabular(f, max_chars=args.max_chars)
                    result = engine.analyze_tabular(td, channel)
                    mode = "tabular"
                    oracle_text = "\n".join(
                        ["\n".join([c.header, *c.values]) for c in td.columns]
                        + list(td.body))
                else:
                    text = normalize_ws(extract_text(f, max_chars=args.max_chars))
                    result = engine.analyze(text, channel)
                    mode = "plain"
                    oracle_text = text
            except ExtractionTooLarge as e:
                # Over the cap → refused at extraction, exactly like the orchestrator.
                # The deny corpus expects a BLOCK, so a capped file is a valid PASS;
                # the parity check is skipped (no full analysis was performed).
                elapsed_ms = (time.perf_counter() - t0) * 1000
                mm = re.search(r"_(b[123])\.", f.name)
                expected = _EXPECTED.get(mm.group(1)) if mm else None
                zeros = {"VISA": 0, "CCCD": 0, "PHONE": 0}
                summary_rows.append((f.name, "capped", zeros, zeros, expected, e.char_count,
                                     elapsed_ms, "block", True, True, True, 0))
                continue
            elapsed_ms = (time.perf_counter() - t0) * 1000

            # analyzer per-type counts (+ confidential keyword count + total
            # with_context across ALL policies — mirrors the events.jsonl
            # `with_context` field so this tester can spot the same count drift)
            acounts = {"VISA": 0, "CCCD": 0, "PHONE": 0}
            confid = 0
            wctx = 0
            for v in result.violations:
                wctx += sum(1 for m in v.matches if m.has_context)
                ty = pii_type(v.policy_id)
                if ty:
                    acounts[ty] += len(v.matches)
                else:
                    confid += len(v.matches)
            ocounts = oracle_counts(oracle_text)

            mm = re.search(r"_(b[123])\.", f.name)
            expected = _EXPECTED.get(mm.group(1)) if mm else None

            parity = acounts == ocounts
            corpus_ok = expected is None or all(ocounts[k] == expected for k in ocounts)
            ok = parity and corpus_ok
            all_pass &= ok

            summary_rows.append((f.name, mode, acounts, ocounts, expected, confid,
                                 elapsed_ms, result.applied_action, ok, parity, corpus_ok, wctx))

            # matches.csv rows (PII only by default; --all adds confidential keywords)
            for v in result.violations:
                if not args.all and pii_type(v.policy_id) is None:
                    continue
                for m in v.matches:
                    if m.start is not None:
                        loc = f"offset {m.start}-{m.end}"
                    else:
                        loc = f"{m.sheet or '-'}|{m.column_name or '(body)'}|row{m.row}"
                    w.writerow([f.name, fmt, loc, pii_type(v.policy_id) or "OTHER",
                                m.text, v.policy_id, m.has_context, m.context_word or "",
                                f"{m.score:.2f}", m.action])

            # per-file annotated artifact
            if mode == "plain":
                (out / f"{f.stem}.annotated.txt").write_text(
                    _annotate_plain(oracle_text, result.violations, args.all), encoding="utf-8")
            else:
                (out / f"{f.stem}.extracted.txt").write_text(
                    _render_tabular(result.violations, args.all), encoding="utf-8")

    # summary table
    lines = []
    hdr = (f"{'file':18s} {'mode':7s} {'analyzer V/C/P':16s} {'oracle V/C/P':16s} "
           f"{'exp':>4s} {'confid':>6s} {'wctx':>6s} {'ms':>7s} {'verdict':9s} result")
    lines.append(hdr)
    lines.append("-" * len(hdr))
    for (name, mode, a, o, exp, confid, ms, verdict, ok, parity, corpus_ok, wctx) in summary_rows:
        astr = f"{a['VISA']}/{a['CCCD']}/{a['PHONE']}"
        ostr = f"{o['VISA']}/{o['CCCD']}/{o['PHONE']}"
        res = "PASS" if ok else ("FAIL(parity)" if not parity else "FAIL(corpus)")
        lines.append(f"{name:18s} {mode:7s} {astr:16s} {ostr:16s} "
                     f"{str(exp or '-'):>4s} {confid:6d} {wctx:6d} {ms:7.0f} {verdict:9s} {res}")
    lines.append("-" * len(hdr))
    lines.append("ALL PASS" if all_pass else "SOME FAILED")
    table = "\n".join(lines)

    (out / "summary.txt").write_text(table, encoding="utf-8")
    print(table)
    print(f"\nmatches.csv + annotated files written to: {out}")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
