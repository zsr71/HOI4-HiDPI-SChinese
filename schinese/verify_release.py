"""Check runtime file hashes against the accepted 0.1.0-rc1 package."""
import argparse
import hashlib
import json
from pathlib import Path

from .apply_release_patches import inside


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('mod', type=Path, help='mod root containing gfx/ and interface/')
    args = ap.parse_args()
    expected = json.loads(Path(__file__).with_name('accepted-payload.json').read_text(encoding='utf-8'))
    bad = []
    for rel, digest in expected['files'].items():
        path = inside(args.mod, rel)
        if not path.is_file():
            bad.append(f'MISSING {rel}')
            continue
        with path.open('rb') as stream:
            actual = hashlib.file_digest(stream, 'sha256').hexdigest()
        if actual != digest:
            bad.append(f'CHANGED {rel}')
    actual_files = {p.relative_to(args.mod).as_posix() for folder in ('gfx', 'interface')
                    for p in (args.mod / folder).rglob('*') if p.is_file()}
    bad.extend(f'EXTRA {rel}' for rel in sorted(actual_files - expected['files'].keys()))
    if bad:
        for error in bad:
            print(error)
        raise SystemExit(1)
    print(f'All {len(expected["files"])} runtime files match the accepted release.')


if __name__ == '__main__':
    main()
