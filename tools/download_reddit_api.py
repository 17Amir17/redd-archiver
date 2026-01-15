#!/usr/bin/env python3
"""
Download Reddit data from Arctic Shift API.

Downloads posts and comments for specified subreddits within a date range.
Outputs .zst compressed files compatible with redd-archiver.

Usage:
    # Download from subreddits file, Oct 2024 to latest
    python download_reddit_api.py -f subreddits.json --start-date 2024-10-01 --limit 500 -o ./data/

    # Download single subreddit for testing
    python download_reddit_api.py -s gaming --start-date 2024-10-01 --limit 100 -o ./test_data/

    # Skip comments (faster)
    python download_reddit_api.py -s gaming --start-date 2024-10-01 --skip-comments -o ./data/

    # Resume interrupted download
    python download_reddit_api.py -f subreddits.json --start-date 2024-10-01 -o ./data/ --resume
"""

import requests
import json
import zstandard as zstd
import argparse
import time
import sys
import random
from datetime import datetime
from pathlib import Path


API_BASE = "https://arctic-shift.photon-reddit.com/api"
DEFAULT_DELAY = 0.5  # Seconds between requests
MAX_FETCH_FOR_RANDOM = 10000  # Max records to fetch before random sampling


def load_subreddits_from_file(filepath: str) -> list[str]:
    """Load subreddit list from a file (supports .txt, .json, or line-delimited)."""
    path = Path(filepath)
    content = path.read_text().strip()

    # Try JSON first
    if path.suffix == '.json' or content.startswith('['):
        try:
            subs = json.loads(content)
            if isinstance(subs, list):
                return [s.strip() for s in subs if isinstance(s, str)]
        except json.JSONDecodeError:
            pass

    # Fall back to line-delimited
    subs = []
    for line in content.splitlines():
        line = line.strip().strip('"').strip("'").strip(',')
        if line and not line.startswith('#'):
            subs.append(line)

    return subs


def download_content(
    endpoint: str,
    subreddit: str,
    start_ts: int,
    end_ts: int | None,
    limit: int,
    random_sample: bool = True,
    verbose: bool = True
) -> list[dict]:
    """
    Download posts or comments from Arctic Shift API with pagination.

    Args:
        endpoint: 'posts' or 'comments'
        subreddit: Subreddit name
        start_ts: Start timestamp (epoch seconds)
        end_ts: End timestamp (epoch seconds), None for latest
        limit: Max records to return
        random_sample: If True, fetch all records and randomly sample `limit`
        verbose: Print progress

    Returns:
        List of record dicts
    """
    records = []
    before = end_ts

    # If random sampling, we need to fetch more than limit to sample from
    fetch_limit = MAX_FETCH_FOR_RANDOM if random_sample else limit

    while len(records) < fetch_limit:
        params = {
            'subreddit': subreddit,
            'after': start_ts,
            'limit': 100,  # Always fetch max per request
            'sort': 'desc'
        }
        if before:
            params['before'] = before

        try:
            resp = requests.get(
                f"{API_BASE}/{endpoint}/search",
                params=params,
                timeout=30
            )

            # Check rate limit headers
            remaining = resp.headers.get('X-RateLimit-Remaining')
            if remaining:
                remaining = int(remaining)
                if remaining < 10:
                    if verbose:
                        print(f"  Rate limit low ({remaining}), backing off...")
                    time.sleep(5)

            # Handle rate limit response
            if resp.status_code == 429:
                reset = int(resp.headers.get('X-RateLimit-Reset', 60))
                if verbose:
                    print(f"  Rate limited, waiting {reset}s...")
                time.sleep(reset)
                continue

            resp.raise_for_status()
            data = resp.json().get('data', [])

            if not data:
                break

            records.extend(data)

            # Paginate backwards in time
            before = data[-1].get('created_utc')

            if verbose and len(records) % 100 == 0:
                if random_sample:
                    print(f"  {endpoint}: fetched {len(records)} (will sample {limit})", end='\r')
                else:
                    print(f"  {endpoint}: {len(records)}/{limit}", end='\r')

            time.sleep(DEFAULT_DELAY)

        except requests.exceptions.Timeout:
            if verbose:
                print(f"  Timeout, retrying...")
            time.sleep(2)
            continue
        except requests.exceptions.RequestException as e:
            if verbose:
                print(f"  Error: {e}")
            break

    # Random sampling
    if random_sample and len(records) > limit:
        if verbose:
            print(f"  {endpoint}: randomly sampling {limit} from {len(records)} records")
        records = random.sample(records, limit)
    elif len(records) > limit:
        records = records[:limit]

    return records


def write_zst(records: list[dict], output_path: Path):
    """Write records to a .zst compressed NDJSON file."""
    cctx = zstd.ZstdCompressor(level=3)

    with open(output_path, 'wb') as f:
        with cctx.stream_writer(f) as writer:
            for record in records:
                line = json.dumps(record, ensure_ascii=False) + '\n'
                writer.write(line.encode('utf-8'))


def load_progress(progress_file: Path) -> set[str]:
    """Load set of completed subreddits from progress file."""
    if progress_file.exists():
        try:
            data = json.loads(progress_file.read_text())
            return set(data.get('completed', []))
        except (json.JSONDecodeError, KeyError):
            pass
    return set()


def save_progress(progress_file: Path, completed: set[str]):
    """Save set of completed subreddits to progress file."""
    progress_file.write_text(json.dumps({
        'completed': list(completed),
        'updated': datetime.now().isoformat()
    }, indent=2))


def download_subreddit(
    subreddit: str,
    start_ts: int,
    end_ts: int | None,
    limit: int,
    output_dir: Path,
    skip_comments: bool = False,
    random_sample: bool = True,
    verbose: bool = True
) -> dict:
    """
    Download posts and comments for a subreddit.

    Returns:
        Stats dict with post/comment counts
    """
    stats = {'posts': 0, 'comments': 0}

    # Download posts
    if verbose:
        print(f"  Downloading posts...")
    posts = download_content('posts', subreddit, start_ts, end_ts, limit, random_sample, verbose)
    stats['posts'] = len(posts)

    if posts:
        posts_file = output_dir / 'submissions' / f'{subreddit}_submissions.zst'
        posts_file.parent.mkdir(parents=True, exist_ok=True)
        write_zst(posts, posts_file)

    # Download comments
    if not skip_comments:
        if verbose:
            print(f"  Downloading comments...")
        comments = download_content('comments', subreddit, start_ts, end_ts, limit, random_sample, verbose)
        stats['comments'] = len(comments)

        if comments:
            comments_file = output_dir / 'comments' / f'{subreddit}_comments.zst'
            comments_file.parent.mkdir(parents=True, exist_ok=True)
            write_zst(comments, comments_file)

    return stats


def main():
    parser = argparse.ArgumentParser(
        description='Download Reddit data from Arctic Shift API',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument('--subreddits', '-s', nargs='+', help='Subreddits to download')
    parser.add_argument('--subreddits-file', '-f', help='File with subreddit list')
    parser.add_argument('--start-date', default='2024-10-01', help='Start date (YYYY-MM-DD)')
    parser.add_argument('--end-date', help='End date (YYYY-MM-DD), default: now')
    parser.add_argument('--limit', '-l', type=int, default=500, help='Max posts per subreddit')
    parser.add_argument('--output-dir', '-o', default='./api_download', help='Output directory')
    parser.add_argument('--skip-comments', action='store_true', help='Skip downloading comments')
    parser.add_argument('--no-random', action='store_true', help='Disable random sampling (get newest posts instead)')
    parser.add_argument('--resume', action='store_true', help='Resume interrupted download')
    parser.add_argument('--quiet', '-q', action='store_true', help='Suppress progress output')

    args = parser.parse_args()

    # Load subreddits
    subreddits = []
    if args.subreddits:
        subreddits.extend(args.subreddits)
    if args.subreddits_file:
        subreddits.extend(load_subreddits_from_file(args.subreddits_file))

    if not subreddits:
        print("Error: No subreddits specified. Use -s or -f", file=sys.stderr)
        sys.exit(1)

    # Parse dates
    start_date = datetime.strptime(args.start_date, '%Y-%m-%d')
    start_ts = int(start_date.timestamp())

    end_ts = None
    if args.end_date:
        end_date = datetime.strptime(args.end_date, '%Y-%m-%d')
        end_ts = int(end_date.timestamp())

    # Setup output directory
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Load progress for resume
    progress_file = output_dir / 'progress.json'
    completed = load_progress(progress_file) if args.resume else set()

    # Filter out completed subreddits
    remaining = [s for s in subreddits if s not in completed]

    verbose = not args.quiet

    random_sample = not args.no_random

    if verbose:
        print(f"Subreddits: {len(subreddits)} total, {len(remaining)} remaining")
        print(f"Date range: {args.start_date} to {args.end_date or 'latest'}")
        print(f"Limit: {args.limit} posts/comments per subreddit")
        print(f"Sampling: {'random' if random_sample else 'newest first'}")
        print(f"Output: {output_dir}")
        print("-" * 50)

    # Download each subreddit
    total_posts = 0
    total_comments = 0

    for i, subreddit in enumerate(remaining, 1):
        if verbose:
            print(f"[{i}/{len(remaining)}] r/{subreddit}")

        try:
            stats = download_subreddit(
                subreddit,
                start_ts,
                end_ts,
                args.limit,
                output_dir,
                args.skip_comments,
                random_sample,
                verbose
            )

            total_posts += stats['posts']
            total_comments += stats['comments']

            if verbose:
                print(f"  Done: {stats['posts']} posts, {stats['comments']} comments")

            # Save progress after each subreddit
            completed.add(subreddit)
            save_progress(progress_file, completed)

        except KeyboardInterrupt:
            print("\nInterrupted. Progress saved. Use --resume to continue.")
            save_progress(progress_file, completed)
            sys.exit(0)
        except Exception as e:
            print(f"  Error downloading r/{subreddit}: {e}")
            continue

    # Summary
    if verbose:
        print("-" * 50)
        print(f"Complete!")
        print(f"Subreddits: {len(completed)}")
        print(f"Total posts: {total_posts:,}")
        print(f"Total comments: {total_comments:,}")
        print(f"Output: {output_dir}")


if __name__ == '__main__':
    main()
