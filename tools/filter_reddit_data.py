#!/usr/bin/env python3
"""
Filter Reddit .zst dumps by subreddit and/or date range.

Downloads from Arctic Shift monthly torrents contain ALL subreddits in one file.
This script streams through the file and extracts only the subreddits you need.

Usage:
    # Filter for specific subreddits
    python filter_reddit_data.py RC_2024-01.zst filtered.zst -s gaming AskReddit pics

    # Filter with date range
    python filter_reddit_data.py RC_2024-01.zst filtered.zst -s gaming --start-date 2024-01-01 --end-date 2024-01-07

    # Filter with limit (for testing)
    python filter_reddit_data.py RC_2024-01.zst filtered.zst -s gaming --limit 100

    # Load subreddits from file (one per line or JSON array)
    python filter_reddit_data.py RC_2024-01.zst filtered.zst --subreddits-file subreddits.txt
"""

import zstandard as zstd
import json
import argparse
import sys
import io
from datetime import datetime
from pathlib import Path


def load_subreddits_from_file(filepath: str) -> set[str]:
    """Load subreddit list from a file (supports .txt, .json, or line-delimited)."""
    path = Path(filepath)
    content = path.read_text().strip()

    # Try JSON first
    if path.suffix == '.json' or content.startswith('['):
        try:
            subs = json.loads(content)
            if isinstance(subs, list):
                return {s.lower().strip() for s in subs if isinstance(s, str)}
        except json.JSONDecodeError:
            pass

    # Fall back to line-delimited
    subs = set()
    for line in content.splitlines():
        line = line.strip().strip('"').strip("'").strip(',')
        if line and not line.startswith('#'):
            subs.add(line.lower())

    return subs


def filter_zst(
    input_path: str,
    output_path: str,
    subreddits: set[str] | None = None,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    limit: int | None = None,
    verbose: bool = True
) -> dict:
    """
    Stream filter a .zst file by subreddit, date, and limit.

    Returns statistics dict with matched/total counts.
    """
    # Convert dates to timestamps
    start_ts = int(start_date.timestamp()) if start_date else None
    end_ts = int(end_date.timestamp()) if end_date else None

    dctx = zstd.ZstdDecompressor()
    cctx = zstd.ZstdCompressor(level=3)

    stats = {
        'total': 0,
        'matched': 0,
        'skipped_subreddit': 0,
        'skipped_date': 0,
        'errors': 0
    }

    with open(input_path, 'rb') as fin:
        with open(output_path, 'wb') as fout:
            with cctx.stream_writer(fout) as writer:
                with dctx.stream_reader(fin) as reader:
                    text_reader = io.TextIOWrapper(reader, encoding='utf-8', errors='replace')

                    for line in text_reader:
                        stats['total'] += 1

                        try:
                            record = json.loads(line)

                            # Filter by subreddit
                            if subreddits:
                                sub = record.get('subreddit', '').lower()
                                if sub not in subreddits:
                                    stats['skipped_subreddit'] += 1
                                    continue

                            # Filter by date
                            created = record.get('created_utc', 0)
                            if isinstance(created, str):
                                created = int(float(created))

                            if start_ts and created < start_ts:
                                stats['skipped_date'] += 1
                                continue
                            if end_ts and created > end_ts:
                                stats['skipped_date'] += 1
                                continue

                            # Write matching record
                            writer.write(line.encode('utf-8'))
                            stats['matched'] += 1

                            # Check limit
                            if limit and stats['matched'] >= limit:
                                if verbose:
                                    print(f"\nReached limit of {limit} records")
                                break

                        except json.JSONDecodeError:
                            stats['errors'] += 1
                            continue

                        # Progress reporting
                        if verbose and stats['total'] % 1_000_000 == 0:
                            pct = (stats['matched'] / stats['total'] * 100) if stats['total'] > 0 else 0
                            print(f"Processed {stats['total']:,} | Matched {stats['matched']:,} ({pct:.2f}%)")

    return stats


def main():
    parser = argparse.ArgumentParser(
        description='Filter Reddit .zst dumps by subreddit and date',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument('input', help='Input .zst file (e.g., RC_2024-01.zst)')
    parser.add_argument('output', help='Output .zst file')
    parser.add_argument('--subreddits', '-s', nargs='+', help='Subreddits to include')
    parser.add_argument('--subreddits-file', '-f', help='File with subreddit list (txt/json)')
    parser.add_argument('--start-date', help='Start date (YYYY-MM-DD)')
    parser.add_argument('--end-date', help='End date (YYYY-MM-DD)')
    parser.add_argument('--limit', '-l', type=int, help='Max records to output')
    parser.add_argument('--quiet', '-q', action='store_true', help='Suppress progress output')

    args = parser.parse_args()

    # Load subreddits
    subreddits = set()
    if args.subreddits:
        subreddits.update(s.lower() for s in args.subreddits)
    if args.subreddits_file:
        subreddits.update(load_subreddits_from_file(args.subreddits_file))

    if not subreddits:
        print("Warning: No subreddits specified. Will match ALL subreddits.", file=sys.stderr)
        subreddits = None
    else:
        print(f"Filtering for {len(subreddits)} subreddits")

    # Parse dates
    start = datetime.strptime(args.start_date, '%Y-%m-%d') if args.start_date else None
    end = datetime.strptime(args.end_date, '%Y-%m-%d') if args.end_date else None

    if start:
        print(f"Start date: {start.date()}")
    if end:
        print(f"End date: {end.date()}")
    if args.limit:
        print(f"Limit: {args.limit} records")

    print(f"\nInput: {args.input}")
    print(f"Output: {args.output}")
    print("-" * 50)

    # Run filter
    stats = filter_zst(
        args.input,
        args.output,
        subreddits,
        start,
        end,
        args.limit,
        verbose=not args.quiet
    )

    # Print summary
    print("-" * 50)
    print(f"Total processed: {stats['total']:,}")
    print(f"Matched: {stats['matched']:,}")
    print(f"Skipped (wrong subreddit): {stats['skipped_subreddit']:,}")
    print(f"Skipped (outside date range): {stats['skipped_date']:,}")
    print(f"Errors: {stats['errors']:,}")

    # Output file size
    output_size = Path(args.output).stat().st_size
    input_size = Path(args.input).stat().st_size
    reduction = (1 - output_size / input_size) * 100 if input_size > 0 else 0
    print(f"\nOutput size: {output_size / 1024 / 1024:.2f} MB ({reduction:.1f}% reduction)")


if __name__ == '__main__':
    main()
