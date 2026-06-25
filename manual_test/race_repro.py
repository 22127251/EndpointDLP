"""
Analyzer determinism / concurrency reproduction harness.

Purpose
-------
The orchestrator analyzes files with ONE shared DLPEngine across several worker
threads (per-channel ThreadPoolExecutors). A reported intermittent bug — the
same file producing different keyword counts / with_context counts on the VM —
would, if real in the analyzer, show up as a *divergent result for identical
input bytes*. This script reproduces the orchestrator's concurrency (one shared
engine, many threads, the real extract+analyze path) WITHOUT the pipes, so it
can be run on the constrained 8 GB VM to answer one question directly:

    Given identical input bytes, does the analyzer EVER return different output?

It does NOT touch pipes, the service, the registry, or any system state — it
only reads the corpus files and computes in memory, so it is safe to run on the
dev box AND the VM (copy this file + the `analyzer/` folder + the corpus over,
or point --analyzer at the installed C:\\Program Files\\DLP\\analyzer).

How to read the result
----------------------
* "DETERMINISTIC (all N runs identical)" for every file  → the analyzer is NOT
  the source of the inconsistency; look at what bytes were delivered (see the
  orchestrator's DIAG sha8 line in dlp-agent.log).
* Any file showing ">1 distinct result" or errors           → the analyzer IS
  non-deterministic under concurrency on this machine — capture the output.

Usage (dev box, repo .venv):
  .venv\\Scripts\\python.exe manual_test\\race_repro.py
  .venv\\Scripts\\python.exe manual_test\\race_repro.py --threads 16 --iters 40 --channel peripheral_storage

Usage (VM, installed bundle — no repo, no venv):
  "C:\\Program Files\\DLP\\python\\python.exe" race_repro.py ^
      --analyzer "C:\\Program Files\\DLP\\analyzer" ^
      --policy   "C:\\Program Files\\DLP\\analyzer\\policies.yaml" ^
      --corpus   "C:\\path\\to\\deny"

Exit code 0 = deterministic on every file; 1 = a divergence (or error) was seen.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import sys
import threading
import time
from pathlib import Path

# Vietnamese context words crash a cp1252 console on print(); force UTF-8 so the
# harness itself never adds the very mojibake we are investigating.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
except Exception:  # very old Python / redirected stream
    pass

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent

# Extensions the analyzer knows how to extract (skip everything else).
_EXTS = (".csv", ".tsv", ".txt", ".md", ".docx", ".odt", ".xlsx", ".ods", ".pdf")


def _resolve_analyzer_dir(arg: str | None) -> Path:
    candidates = []
    if arg:
        candidates.append(Path(arg))
    candidates.append(_REPO / "analyzer")
    candidates.append(Path(r"C:\Program Files\DLP\analyzer"))
    for c in candidates:
        if (c / "engine.py").is_file():
            return c
    raise SystemExit(
        "Could not find the analyzer package (engine.py). Pass --analyzer <dir>. "
        f"Tried: {[str(c) for c in candidates]}"
    )


def _fingerprint(result) -> tuple:
    """Order-independent fingerprint of an AnalysisResult: per-policy
    (count, with_context, sorted context_words) + a count of '?' chars in any
    context word (real extracted-text corruption would surface here)."""
    rows = []
    qmarks = 0
    for v in result.violations:
        wc = sum(1 for m in v.matches if m.has_context)
        cws = tuple(sorted(v.context_words))
        qmarks += sum(cw.count("?") for cw in cws)
        rows.append((v.policy_id, len(v.matches), wc, cws))
    return (result.applied_action, tuple(sorted(rows)), qmarks)


def main() -> int:
    ap = argparse.ArgumentParser(description="Analyzer determinism / concurrency repro")
    ap.add_argument("--analyzer", default=None, help="path to the analyzer package dir (has engine.py)")
    ap.add_argument("--policy", default=None, help="policies.yaml (default: <analyzer>/policies.yaml)")
    ap.add_argument("--corpus", default=str(_REPO / "tmp" / "final-demo" / "deny"),
                    help="folder of files to analyze (searched recursively)")
    ap.add_argument("--channel", default="browser",
                    choices=["clipboard", "browser", "peripheral", "peripheral_storage"])
    ap.add_argument("--threads", type=int, default=12, help="concurrent worker threads")
    ap.add_argument("--iters", type=int, default=30, help="analyses per thread, per file")
    ap.add_argument("--max-chars", type=int, default=16_000_000,
                    help="extracted-text cap (mirrors analyzer.max_extracted_chars); <=0 disables")
    args = ap.parse_args()

    analyzer_dir = _resolve_analyzer_dir(args.analyzer)
    sys.path.insert(0, str(analyzer_dir))
    policy = args.policy or str(analyzer_dir / "policies.yaml")
    channel = "peripheral_storage" if args.channel == "peripheral" else args.channel
    max_chars = args.max_chars if args.max_chars and args.max_chars > 0 else None

    from engine import DLPEngine                                   # noqa: E402
    from extractor import (ExtractionTooLarge, extract_tabular,     # noqa: E402
                           extract_text, is_tabular)

    corpus = Path(args.corpus)
    files = sorted(p for p in corpus.rglob("*")
                   if p.is_file() and p.suffix.lower() in _EXTS and not p.name.startswith("~$"))
    if not files:
        print(f"No analyzable files under {corpus}", file=sys.stderr)
        return 1

    print(f"analyzer = {analyzer_dir}")
    print(f"policy   = {policy}")
    print(f"channel  = {channel}   threads = {args.threads}   iters/thread = {args.iters}")
    print(f"corpus   = {corpus}  ({len(files)} files)\n")

    engine = DLPEngine(policy)   # ONE shared engine — exactly like PolicyManager

    def analyze_file(p: Path):
        if is_tabular(p):
            return engine.analyze_tabular(extract_tabular(p, max_chars=max_chars), channel)
        return engine.analyze(extract_text(p, max_chars=max_chars), channel)

    overall_ok = True
    hdr = f"{'file':22s} {'in_sha8':10s} {'runs':>5s} {'distinct':>8s} {'errors':>6s} {'verdict'}"
    print(hdr)
    print("-" * len(hdr))

    for f in files:
        in_sha8 = hashlib.sha256(f.read_bytes()).hexdigest()[:8]

        # Files over the cap are an expected, deterministic BLOCK (reason=text_cap):
        # confirm the cap raises consistently rather than treating it as a result.
        try:
            analyze_file(f)
            capped = False
        except ExtractionTooLarge:
            capped = True
        except Exception as e:  # noqa: BLE001
            print(f"{f.name:22s} {in_sha8:10s} {'-':>5s} {'-':>8s} {'-':>6s} ERROR(setup): {e!r}")
            overall_ok = False
            continue

        results: collections.Counter = collections.Counter()
        errors: collections.Counter = collections.Counter()
        lock = threading.Lock()

        def worker(target=f, capped=capped):
            for _ in range(args.iters):
                try:
                    if capped:
                        try:
                            analyze_file(target)
                            outcome = ("UNEXPECTED_OK",)   # cap should have raised
                        except ExtractionTooLarge:
                            outcome = ("capped_block",)
                    else:
                        outcome = _fingerprint(analyze_file(target))
                    with lock:
                        results[outcome] += 1
                except Exception as e:  # noqa: BLE001
                    with lock:
                        errors[repr(e)[:90]] += 1

        threads = [threading.Thread(target=worker) for _ in range(args.threads)]
        t0 = time.perf_counter()
        for t in threads: t.start()
        for t in threads: t.join()
        dt = time.perf_counter() - t0

        runs = sum(results.values()) + sum(errors.values())
        distinct = len(results)
        ok = (distinct == 1) and not errors
        overall_ok &= ok
        verdict = "DETERMINISTIC" if ok else ("DIVERGENCE!" if distinct > 1 else "ERRORS!")
        tag = "capped" if capped else ("PASS" if ok else "FAIL")
        print(f"{f.name:22s} {in_sha8:10s} {runs:>5d} {distinct:>8d} {len(errors):>6d} {verdict}  [{tag}] {dt:.1f}s")

        if not ok:
            for outcome, cnt in results.most_common():
                print(f"    x{cnt}: {outcome}")
            for e, cnt in errors.most_common():
                print(f"    ERR x{cnt}: {e}")

    print("-" * len(hdr))
    print("ALL FILES DETERMINISTIC" if overall_ok else "DIVERGENCE OR ERRORS DETECTED")
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
