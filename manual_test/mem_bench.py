"""
Repeatable PROCESS-RSS memory benchmark for the analyzer.

Why this exists
---------------
tracemalloc only sees allocations made through CPython's own allocator, so it
under-reports real memory: it is blind to re2's internal UTF-8 encode of the
scanned text, the str.lower() copy, lxml's C-level extraction tree, and any
memory the allocator has not yet returned to the OS. This tool instead reads the
Windows **working set** via GetProcessMemoryInfo, so the numbers line up with
what Task Manager shows.

Two metrics per process:
  * peak_wset  — PeakWorkingSetSize: the high-water-mark RSS over the process'
                 whole life (what you watch climb in Task Manager).
  * rss_now    — WorkingSetSize: current RSS at the moment of the read.

Modes
-----
Per-file (default): spawn ONE fresh subprocess per file so each file's peak is
  isolated and reproducible, and print a table + a 7-worker worst-case estimate.
  A baseline subprocess (engine loaded, empty analysis) measures the fixed
  interpreter+engine cost so per-file deltas exclude it.

Single-process (--single-process): analyze every file in ONE long-lived process
  and report the single peak — this mirrors `iso_test.py` and reproduces the
  number you see watching Task Manager during an iso_test run.

Usage
-----
  .venv\\Scripts\\python.exe manual_test\\mem_bench.py --corpus tmp\\final-demo\\deny
  .venv\\Scripts\\python.exe manual_test\\mem_bench.py --file tmp\\final-demo\\deny\\docx\\docx_b3.docx --repeat 3
  .venv\\Scripts\\python.exe manual_test\\mem_bench.py --corpus tmp\\final-demo\\deny --single-process
"""

from __future__ import annotations

import argparse
import ctypes
import gc
import json
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
sys.path.insert(0, str(_REPO / "analyzer"))

from engine import DLPEngine                                   # noqa: E402
from extractor import extract_tabular, extract_text, is_tabular  # noqa: E402

_EXTS = (".csv", ".tsv", ".txt", ".md", ".docx", ".odt", ".xlsx", ".ods", ".pdf")
_DEFAULT_POLICY = str(_REPO / "analyzer" / "policies.yaml")
_MB = 1024 * 1024


# --- Windows working-set readout (no third-party deps) -----------------------

class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_psapi = ctypes.WinDLL("psapi", use_last_error=True)
_kernel32.GetCurrentProcess.restype = wintypes.HANDLE
_psapi.GetProcessMemoryInfo.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(_PROCESS_MEMORY_COUNTERS), wintypes.DWORD]
_psapi.GetProcessMemoryInfo.restype = wintypes.BOOL


def _mem_counters() -> _PROCESS_MEMORY_COUNTERS:
    c = _PROCESS_MEMORY_COUNTERS()
    c.cb = ctypes.sizeof(c)
    h = _kernel32.GetCurrentProcess()
    if not _psapi.GetProcessMemoryInfo(h, ctypes.byref(c), c.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    return c


def _peak_mb() -> float:
    return _mem_counters().PeakWorkingSetSize / _MB


def _rss_mb() -> float:
    return _mem_counters().WorkingSetSize / _MB


# --- the analysis itself (identical routing to policy_manager) ---------------

def _analyze_one(engine: DLPEngine, path: Path, channel: str) -> int:
    """Analyze a single file exactly like the orchestrator does. Returns total
    match count so the work cannot be optimized away."""
    if is_tabular(path):
        result = engine.analyze_tabular(extract_tabular(path), channel)
    else:
        result = engine.analyze(extract_text(path), channel)
    return sum(len(v.matches) for v in result.violations)


def _extract_only(path: Path) -> int:
    """Extraction without analysis — isolates the extractor's memory cost from
    the scan-copy cost. Returns the extracted char count (work not elided)."""
    if is_tabular(path):
        td = extract_tabular(path)
        return sum(len(c.header) + sum(len(v) for v in c.values) for c in td.columns) \
            + sum(len(p) for p in td.body)
    return len(extract_text(path))


# --- worker process: measures one file (or the engine baseline) --------------

def _worker(args) -> int:
    engine = DLPEngine(args.policy)
    ws_after_load = _rss_mb()

    if args.baseline:
        engine.analyze("", args.channel)  # touch the analysis path with no data
        out = {"name": "(engine baseline)", "mode": "-", "matches": 0,
               "elapsed_ms": 0.0, "ws_after_load": ws_after_load,
               "peak_wset": _peak_mb(), "rss_now": _rss_mb(), "rss_trend": []}
        print("RESULT_JSON " + json.dumps(out))
        return 0

    path = Path(args.file)
    if args.extract_only:
        nchars = _extract_only(path)
        out = {"name": path.name, "mode": "extract", "matches": nchars,
               "elapsed_ms": 0.0, "ws_after_load": ws_after_load,
               "peak_wset": _peak_mb(), "rss_now": _rss_mb(), "rss_trend": []}
        print("RESULT_JSON " + json.dumps(out))
        return 0

    mode = "tabular" if is_tabular(path) else "plain"
    elapsed_total = 0.0
    matches = 0
    rss_trend: list[float] = []
    for _ in range(args.repeat):
        t0 = time.perf_counter()
        matches = _analyze_one(engine, path, args.channel)
        elapsed_total += (time.perf_counter() - t0) * 1000
        gc.collect()                 # force steady state so rss_trend shows leaks
        rss_trend.append(round(_rss_mb(), 1))

    out = {
        "name": path.name, "mode": mode, "matches": matches,
        "elapsed_ms": elapsed_total / args.repeat,
        "ws_after_load": ws_after_load,
        "peak_wset": _peak_mb(),     # high-water mark over load + all repeats
        "rss_now": _rss_mb(),
        "rss_trend": rss_trend,      # current RSS after each repeat (growth = leak)
    }
    print("RESULT_JSON " + json.dumps(out))
    return 0


# --- driver process: spawns one fresh worker per file ------------------------

def _run_worker_subprocess(extra: list[str], policy: str, channel: str) -> dict:
    cmd = [sys.executable, str(Path(__file__).resolve()),
           "--worker", "--policy", policy, "--channel", channel, *extra]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    for line in proc.stdout.splitlines():
        if line.startswith("RESULT_JSON "):
            return json.loads(line[len("RESULT_JSON "):])
    raise RuntimeError(f"worker produced no result:\n{proc.stdout}\n{proc.stderr}")


def _collect_files(corpus: Path) -> list[Path]:
    return sorted(p for p in corpus.rglob("*")
                  if p.is_file() and p.suffix.lower() in _EXTS
                  and not p.name.startswith("~$"))


def _driver(args) -> int:
    files = [Path(args.file)] if args.file else _collect_files(Path(args.corpus))
    if not files:
        print("No files found.", file=sys.stderr)
        return 1

    base = _run_worker_subprocess(["--baseline"], args.policy, args.channel)
    base_peak = base["peak_wset"]

    rows = []
    for f in files:
        r = _run_worker_subprocess(["--file", str(f), "--repeat", str(args.repeat)],
                                   args.policy, args.channel)
        rows.append(r)

    hdr = (f"{'file':18s} {'mode':7s} {'matches':>7s} {'ms':>7s} "
           f"{'peak_MB':>8s} {'delta_MB':>8s} {'rss_end_MB':>10s} rss_trend")
    print()
    print(f"engine+interpreter baseline peak: {base_peak:7.1f} MB "
          f"(rss after load {base['ws_after_load']:.1f} MB)")
    print(hdr)
    print("-" * len(hdr))
    worst = None
    for r in rows:
        delta = r["peak_wset"] - base_peak
        if worst is None or delta > worst[1]:
            worst = (r["name"], delta)
        trend = "->".join(f"{x:.0f}" for x in r["rss_trend"])
        print(f"{r['name']:18s} {r['mode']:7s} {r['matches']:7d} {r['elapsed_ms']:7.0f} "
              f"{r['peak_wset']:8.1f} {delta:8.1f} {r['rss_now']:10.1f} {trend}")
    print("-" * len(hdr))

    workers = args.workers
    worst_name, worst_delta = worst
    # One shared engine + N concurrent per-file transients (deltas above the
    # already-loaded engine). Worst case = N copies of the largest delta.
    est = base_peak + workers * worst_delta
    print(f"worst per-file transient: {worst_delta:.1f} MB ({worst_name})")
    print(f"{workers}-worker worst-case estimate: {base_peak:.1f} + {workers}x{worst_delta:.1f} "
          f"= {est:.1f} MB ({est/1024:.2f} GB)")
    return 0


def _single_process(args) -> int:
    """Mirror iso_test: analyze every file in ONE process; report the one peak."""
    files = [Path(args.file)] if args.file else _collect_files(Path(args.corpus))
    if not files:
        print("No files found.", file=sys.stderr)
        return 1
    engine = DLPEngine(args.policy)
    print(f"engine loaded; rss {_rss_mb():.1f} MB, peak {_peak_mb():.1f} MB")
    for f in files:
        m = _analyze_one(engine, f, args.channel)
        print(f"  {f.name:18s} matches={m:4d}  rss {_rss_mb():7.1f} MB  peak {_peak_mb():7.1f} MB")
    print(f"\nSINGLE-PROCESS PEAK: {_peak_mb():.1f} MB ({_peak_mb()/1024:.2f} GB) "
          f"over {len(files)} files")
    return 0


def _concurrent(args) -> int:
    """Production-shape worst case: ONE process, ONE shared engine, N threads
    each analyzing the same file at once (re2/lxml release the GIL, so the
    transients really do stack). This is what the orchestrator's 7-way thread
    pools do, so the peak here is the agent's worst-case RSS for that file."""
    import threading
    engine = DLPEngine(args.policy)
    base = _rss_mb()
    path = Path(args.file)
    nchars = _extract_only(path)
    errors: list[BaseException] = []

    def work() -> None:
        try:
            _analyze_one(engine, path, args.channel)
        except BaseException as e:  # noqa: BLE001 — surface worker failures
            errors.append(e)

    threads = [threading.Thread(target=work) for _ in range(args.concurrent)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = (time.perf_counter() - t0) * 1000
    if errors:
        print(f"WORKER ERRORS: {errors[:3]}")
    print(f"{args.concurrent}x concurrent  {path.name}  ({nchars/1e6:.1f}M chars)")
    print(f"  engine base {base:.1f} MB -> PEAK {_peak_mb():.1f} MB "
          f"({_peak_mb()/1024:.2f} GB)   elapsed {elapsed:.0f} ms")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corpus", default=str(_REPO / "tmp" / "final-demo" / "deny"))
    ap.add_argument("--file", help="benchmark a single file instead of a corpus")
    ap.add_argument("--channel", default="browser",
                    choices=["clipboard", "browser", "peripheral", "peripheral_storage"])
    ap.add_argument("--policy", default=_DEFAULT_POLICY)
    ap.add_argument("--repeat", type=int, default=1,
                    help="analyze each file N times per worker (peak across all; "
                         "rss_trend shows steady-state growth = leak)")
    ap.add_argument("--workers", type=int, default=7,
                    help="concurrency for the worst-case estimate (default 7)")
    ap.add_argument("--single-process", action="store_true",
                    help="analyze all files in one process (mirrors iso_test)")
    ap.add_argument("--concurrent", type=int, default=0, metavar="N",
                    help="ONE shared engine, N threads analyzing --file at once "
                         "(production-shape worst case; needs --file)")
    ap.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--baseline", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--extract-only", action="store_true",
                    help="measure extraction RSS only (no analysis)")
    args = ap.parse_args()
    args.channel = "peripheral_storage" if args.channel == "peripheral" else args.channel

    if args.worker:
        return _worker(args)
    if args.concurrent:
        if not args.file:
            ap.error("--concurrent needs --file")
        return _concurrent(args)
    if args.single_process:
        return _single_process(args)
    return _driver(args)


if __name__ == "__main__":
    sys.exit(main())
