"""Parallel ranged downloader for throttled-per-connection hosts.

Usage: python download_parallel.py <url> <out_path> [connections]

Writes each range to a .part file, then assembles. Re-running resumes
finished parts (skips complete ones). Verifies final size.
"""

from __future__ import annotations

import concurrent.futures
import sys
import time
from pathlib import Path

import httpx


def head_size(url: str) -> tuple[int, str]:
    with httpx.Client(follow_redirects=True, timeout=30) as client:
        response = client.head(url)
        if "content-length" in response.headers:
            return int(response.headers["content-length"]), str(response.url)
        # Fallback: range probe (some CDNs omit content-length on HEAD).
        probe = client.get(url, headers={"Range": "bytes=0-0"})
        content_range = probe.headers.get("content-range", "")
        if "/" not in content_range:
            raise RuntimeError(f"cannot determine size: {probe.status_code} {dict(probe.headers)}")
        return int(content_range.rsplit("/", 1)[1]), str(probe.url)


def fetch_range(url: str, start: int, end: int, part_path: Path, tries: int = 30) -> int:
    expected = end - start + 1
    for _attempt in range(tries):
        done = part_path.stat().st_size if part_path.exists() else 0
        if done >= expected:
            return done
        range_start = start + done
        try:
            with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(connect=10, read=30, write=30, pool=30)) as client:
                with client.stream("GET", url, headers={"Range": f"bytes={range_start}-{end}"}) as response:
                    response.raise_for_status()
                    with part_path.open("ab") as fh:
                        for chunk in response.iter_bytes(1 << 16):
                            fh.write(chunk)
        except (httpx.HTTPError, OSError):
            time.sleep(1)
    return part_path.stat().st_size if part_path.exists() else 0


def main() -> None:
    url, out = sys.argv[1], Path(sys.argv[2])
    connections = int(sys.argv[3]) if len(sys.argv) > 3 else 32

    size, final_url = head_size(url)
    print(f"size: {size/1e6:.1f} MB, final host: {final_url.split('/')[2]}, connections: {connections}", flush=True)

    part_dir = out.with_suffix(out.suffix + ".parts")
    part_dir.mkdir(exist_ok=True)
    chunk = (size + connections - 1) // connections
    ranges = [(i, min(i * chunk, size - 1), min((i + 1) * chunk - 1, size - 1)) for i in range(connections) if i * chunk < size]

    started = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=connections) as pool:
        futures = [pool.submit(fetch_range, final_url, start, end, part_dir / f"part{i:04d}") for i, start, end in ranges]
        done_bytes = 0
        while True:
            if all(f.done() for f in futures):
                break
            time.sleep(10)
            done_bytes = sum(p.stat().st_size for p in part_dir.glob("part*") if p.exists())
            rate = done_bytes / max(1e-9, time.time() - started) / 1000
            eta_min = (size - done_bytes) / max(rate * 1000, 1) / 60
            print(f"progress: {done_bytes/1e6:8.1f}/{size/1e6:.1f} MB  {rate:7.0f} KB/s  eta {eta_min:5.1f} min", flush=True)

    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("wb") as target:
        for i, _start, _end in ranges:
            part = part_dir / f"part{i:04d}"
            with part.open("rb") as src:
                while True:
                    buf = src.read(1 << 20)
                    if not buf:
                        break
                    target.write(buf)
    actual = out.stat().st_size
    if actual != size:
        print(f"SIZE MISMATCH: {actual} != {size}", flush=True)
        sys.exit(1)
    for p in part_dir.glob("part*"):
        p.unlink()
    part_dir.rmdir()
    print(f"DONE: {out} ({actual/1e6:.1f} MB in {time.time()-started:.0f}s)", flush=True)


if __name__ == "__main__":
    main()
