"""Reconstruct accepted text edits and calibrated font aliases into a new overlay.

This is a source review/maintenance tool, not a standalone mod builder. See README.md.
"""
import argparse
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil

from hidpi.bmfont import read_fnt, write_fnt

DATA = Path(__file__).with_name('release-patches.json')


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def inside(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f'Path escapes root: {relative}')
    return path


def reconstruct(entry, original):
    expected = entry['source_sha256']
    if expected is not None and sha256(original) != expected:
        raise ValueError(f'Game file changed: {entry["path"]}; review this patch for the installed version')
    lines = original.decode('utf-8', 'surrogateescape').splitlines(True)
    parts = []
    for op in entry['ops']:
        if 'copy_lines' in op:
            start, stop = op['copy_lines']
            parts.extend(lines[start:stop])
        else:
            parts.append(op['text'])
    result = ''.join(parts).encode('utf-8', 'surrogateescape')
    if sha256(result) != entry['target_sha256']:
        raise ValueError(f'Reconstructed file does not match accepted output: {entry["path"]}')
    return result


def apply(game, out, base_mod=None, text_only=False):
    if out.exists():
        raise FileExistsError('Choose a new --out directory; existing directories are never overwritten')
    if not text_only and base_mod is None:
        raise ValueError('Supply --base-mod with the accepted font resources, or use --text-only')
    data = json.loads(DATA.read_text(encoding='utf-8'))
    text = []
    # Check all inputs before writing anything.
    for entry in data['text_files']:
        source = inside(game, entry['path'])
        if entry['source_sha256'] is None:
            if source.exists():
                raise ValueError(f'New mod file now exists in game: {entry["path"]}')
            original = b''
        else:
            original = source.read_bytes()
        text.append((entry, reconstruct(entry, original)))
    aliases = []
    if not text_only:
        for entry in data['font_aliases']:
            source = inside(base_mod, entry['source'])
            texture = source.with_suffix('.dds')
            if sha256(source.read_bytes()) != entry['source_sha256']:
                raise ValueError(f'Font layout differs from accepted release: {entry["source"]}')
            if sha256(texture.read_bytes()) != entry['texture_sha256']:
                raise ValueError(f'Font texture differs from accepted release: {entry["source"]}')
            font = read_fnt(source)
            font = replace(font, glyphs=[replace(g, yo=g.yo + entry['yoffset']) for g in font.glyphs])
            aliases.append((entry, font, texture))
    out.mkdir(parents=True)
    for entry, content in text:
        target = inside(out, entry['path'])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    for entry, font, texture in aliases:
        target = inside(out, entry['target'])
        target.parent.mkdir(parents=True, exist_ok=True)
        write_fnt(target, font, target.with_suffix('.dds').name,
                  int(font.common['scaleW']), int(font.common['scaleH']))
        # The accepted package was written on Windows. Preserve its bytes on other platforms too.
        content = target.read_bytes().replace(b'\r\n', b'\n')
        if sha256(content) != entry['target_sha256']:
            content = content.replace(b'\n', b'\r\n')
        if sha256(content) != entry['target_sha256']:
            raise ValueError(f'Font alias reconstruction failed: {entry["target"]}')
        target.write_bytes(content)
        shutil.copyfile(texture, target.with_suffix('.dds'))
    return len(text), len(aliases)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--game', type=Path, required=True, help='HOI4 1.19.3 install directory')
    ap.add_argument('--out', type=Path, required=True, help='new overlay output directory')
    ap.add_argument('--base-mod', type=Path, help='accepted 0.1.0-rc1 mod directory, used for font aliases')
    ap.add_argument('--text-only', action='store_true', help='export only shader/interface edits; not playable alone')
    args = ap.parse_args()
    text, aliases = apply(args.game, args.out, args.base_mod, args.text_only)
    print(f'Verified and wrote {text} text files and {aliases} font aliases to {args.out}')
    print('This overlay requires the base mod font resources; do not enable it as a standalone mod.')


if __name__ == '__main__':
    main()
