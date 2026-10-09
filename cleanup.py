"""Explicit maintenance; preview by default. Stop all writers before --apply."""
import argparse
import json
from pathlib import Path
import re
import shutil
import time
from tts import ROOT

JOB_PATTERN = re.compile(r'\d{8}-\d{6}-[a-f0-9]{12}')
PCM_PATTERN = re.compile(r'(?:pl|ru)_[a-f0-9]{64}\.norm-v\d+\.wav')


def cleanup(root, days=30, apply=False, normalized_cache_only=False):
    if days < 1:
        raise ValueError('--days must be at least 1')
    cutoff = time.time() - days * 86400
    selected = []
    if normalized_cache_only:
        cache = root / 'cache'
        if not cache.is_dir() or cache.is_symlink():
            return selected
        for wav in sorted(cache.iterdir()):
            if not PCM_PATTERN.fullmatch(wav.name) or wav.is_symlink() or not wav.is_file():
                continue
            manifest = wav.with_suffix('.json')
            if manifest.is_symlink() or (manifest.exists() and not manifest.is_file()):
                continue
            # Age means last creation/rebuild, not last use. All writers must be stopped.
            files = [wav] + ([manifest] if manifest.exists() else [])
            if any(p.stat().st_mtime >= cutoff for p in files):
                continue
            selected.append(wav)
            size = sum(p.stat().st_size for p in files)
            print(('DELETE ' if apply else 'WOULD DELETE ') + wav.name + f' (+ manifest, {size} bytes)')
            if apply:
                for path in files:
                    path.unlink(missing_ok=True)
        return selected
    output = root / 'output'
    if not output.is_dir() or output.is_symlink():
        return selected
    for directory in sorted(output.iterdir()):
        if not directory.is_dir() or directory.is_symlink() or not JOB_PATTERN.fullmatch(directory.name):
            continue
        status = directory / 'status.json'
        try:
            data = json.loads(status.read_text())
            if data.get('state') not in ('done', 'failed', 'cancelled') or status.stat().st_mtime >= cutoff:
                continue
        except (OSError, ValueError, AttributeError):
            continue
        selected.append(directory)
        print(('DELETE ' if apply else 'WOULD DELETE ') + directory.name)
        if apply:
            shutil.rmtree(directory)
    return selected


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--days', type=int, default=30)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--normalized-cache-only', action='store_true',
                        help='Only old normalized WAV and their manifests; keep MP3, jobs and locks')
    args = parser.parse_args()
    if args.days < 1:
        parser.error('--days must be at least 1')
    cleanup(ROOT, args.days, args.apply, args.normalized_cache_only)


if __name__ == '__main__':
    main()
