"""
fiverr_presence.cli — command-line interface.

Usage
-----
  python -m fiverr_presence <url_or_username>                 # single check
  python -m fiverr_presence --batch sellers.txt --json        # batch from file
  python -m fiverr_presence <username> --watch 60             # poll every 60 s
  python -m fiverr_presence serve --port 8399                 # self-hosted API

Output
------
Human-readable by default; pass --json for machine-readable output.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time

from .core import (
    CheckerConfig,
    FiverrPresenceChecker,
    STATUS_OFFLINE,
    STATUS_ONLINE,
    STATUS_UNKNOWN,
)

ICON = {STATUS_ONLINE: "\u001b[32m● ONLINE\u001b[0m", STATUS_OFFLINE: "\u001b[90m○ OFFLINE\u001b[0m", STATUS_UNKNOWN: "\u001b[33m? UNKNOWN\u001b[0m"}
ICON_PLAIN = {STATUS_ONLINE: "ONLINE", STATUS_OFFLINE: "OFFLINE", STATUS_UNKNOWN: "UNKNOWN"}


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fiverr_presence",
        description="Check whether a Fiverr seller profile is publicly shown Online or Offline.",
        epilog="Examples:\n"
        "  python -m fiverr_presence https://www.fiverr.com/some_seller\n"
        "  python -m fiverr_presence --batch sellers.txt --csv -o report.csv\n"
        "  python -m fiverr_presence some_seller --watch 300\n"
        "  python -m fiverr_presence --serve --port 8399\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("target", nargs="?", help="profile URL or username (omit when using --batch)")
    p.add_argument("--batch", metavar="FILE", help="text file with one URL/username per line (# comments ok)")
    p.add_argument("--json", action="store_true", help="JSON output (stdout or --output file)")
    p.add_argument("--csv", action="store_true", help="CSV output (implies batch-style rows)")
    p.add_argument("-o", "--output", metavar="FILE", help="also write results to FILE")
    p.add_argument("--no-cache", action="store_true", help="bypass the disk cache")
    p.add_argument("--cache-ttl", type=float, default=None, help="cache TTL seconds (default 60, 0 disables)")
    p.add_argument("--delay", type=float, default=None, help="min seconds between requests in batch mode (default 1.5)")
    p.add_argument("--retries", type=int, default=None, help="retries per check (default 3)")
    p.add_argument("--timeout", type=float, default=None, help="per-request timeout seconds (default 15)")
    p.add_argument("--watch", metavar="SECONDS", type=float, default=None, help="re-check target(s) forever every N seconds")
    p.add_argument("--quiet", action="store_true", help="suppress progress lines in batch mode")
    p.add_argument("--no-color", action="store_true", help="plain status words instead of coloured icons")
    p.add_argument("--serve", action="store_true", help="run the self-hosted HTTP API + mini dashboard (ignores targets)")
    p.add_argument("--host", default="127.0.0.1", help="serve: bind address (default 127.0.0.1)")
    p.add_argument("--port", type=int, default=8399, help="serve: port (default 8399)")
    return p


def _make_checker(args) -> FiverrPresenceChecker:
    cfg_kwargs = {}
    if args.cache_ttl is not None:
        cfg_kwargs["cache_ttl"] = args.cache_ttl
    if args.retries is not None:
        cfg_kwargs["retries"] = args.retries
    if args.timeout is not None:
        cfg_kwargs["timeout"] = args.timeout
    return FiverrPresenceChecker(CheckerConfig(**cfg_kwargs))


def _print_result(res, as_json: bool, plain: bool) -> None:
    if as_json:
        print(json.dumps(res.to_dict(), ensure_ascii=False))
        return
    icon = ICON_PLAIN if plain else ICON
    line = f"{icon[res.status]:<12} {res.username}"
    if res.error:
        line += f"  ({res.error})"
    elif res.cached:
        line += "  (cached)"
    print(line)


def _write_outputs(results, args) -> None:
    rows = [r.to_dict() for r in results]
    if args.output:
        with open(args.output, "w", encoding="utf-8", newline="") as fh:
            if args.csv:
                writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
                writer.writeheader()
                writer.writerows(rows)
            else:
                json.dump(rows, fh, ensure_ascii=False, indent=2)


def _run_single(checker, target, args) -> int:
    try:
        result = checker.check(target, use_cache=not args.no_cache)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    _print_result(result, args.json, args.no_color)
    _write_outputs([result], args)
    return 0 if result.status != STATUS_UNKNOWN else 2


def _load_targets(path: str) -> list[str]:
    targets = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if line:
                targets.append(line)
    return targets


def _run_batch(checker, args) -> int:
    targets = _load_targets(args.batch)
    if not targets:
        print("batch file contains no targets", file=sys.stderr)
        return 2

    def progress(done, total, result):
        if not args.quiet:
            print(f"[{done}/{total}] ", end="", flush=True, file=sys.stderr)
            _print_result(result, False, args.no_color)

    results = checker.check_many(targets, delay=args.delay if args.delay is not None else 1.5,
                                 use_cache=not args.no_cache, progress=progress)

    if args.json or args.csv:
        rows = [r.to_dict() for r in results]
        payload = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) if not args.csv else None
        if args.csv:
            _write_outputs(results, args)
            print(f"wrote CSV to {args.output or '(no --output given; use -o)'}", file=sys.stderr)
        else:
            text = json.dumps(rows, ensure_ascii=False, indent=2)
            if args.output:
                with open(args.output, "w", encoding="utf-8") as fh:
                    fh.write(text)
            else:
                print(text)
    else:
        for r in results:
            _print_result(r, False, args.no_color)
    if args.output and not args.csv and not args.json:
        _write_outputs(results, args)
    return 0


def _run_watch(checker, targets, args) -> int:
    interval = max(10.0, args.watch or 60.0)
    plain = args.no_color
    try:
        while True:
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            results = checker.check_many(targets, use_cache=False)
            for r in results:
                icon = ICON_PLAIN if plain else ICON
                suffix = f"  ({r.error})" if r.error else ""
                print(f"{stamp}  {icon[r.status]:<12} {r.username}{suffix}", flush=True)
            _write_outputs(results, args)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)
        return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.serve:
        from .server import serve
        serve(host=args.host, port=args.port)
        return 0

    checker = _make_checker(args)

    if args.batch:
        return _run_batch(checker, args)
    if not args.target:
        _build_parser().print_help()
        return 2
    # serve flag parsed only for help text; handled above
    if args.watch:
        return _run_watch(checker, [args.target], args)
    return _run_single(checker, args.target, args)


if __name__ == "__main__":
    raise SystemExit(main())
