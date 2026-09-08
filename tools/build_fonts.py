"""Build the subset web fonts embedded into rendered pages.

Development tool only (not used at runtime). Requires ``fonttools`` and
``brotli``:

    python -m pip install fonttools brotli
    python tools/build_fonts.py <dir-with-source-ttfs>

Source files expected in the input directory:

    NotoSansSC-Black.otf, NotoSansSC-Bold.otf, NotoSansSC-Regular.otf
        https://github.com/notofonts/noto-cjk  (Sans/SubsetOTF/SC,
        SIL Open Font License 1.1 — subsetting and redistribution allowed)
    Barlow-Bold.ttf, Barlow-SemiBold.ttf, Barlow-Medium.ttf,
    BarlowSemiCondensed-ExtraBold.ttf
        https://github.com/jpt/barlow  (SIL Open Font License 1.1)

Output goes to ``resources/common/fonts/*.woff2``. CJK fonts are reduced to
the full GB2312 set (6763 characters, covering every simplified-Chinese
operator/boss name seen so far) plus every character used by the templates;
glyphs outside that set fall back to system fonts.
"""

from __future__ import annotations

import string
import sys
from pathlib import Path

from fontTools import subset

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "resources" / "common" / "fonts"

LATIN_EXTRA = "×▸»«※—–·°‰•“”‘’…、，。：；！？（）【】《》〈〉「」［］"
CJK_FONTS = ("NotoSansSC-Black", "NotoSansSC-Bold", "NotoSansSC-Regular")
LATIN_FONTS = (
    "Barlow-Bold",
    "Barlow-SemiBold",
    "Barlow-Medium",
    "BarlowSemiCondensed-ExtraBold",
)


def gb2312_characters() -> set[str]:
    chars: set[str] = set()
    for row in range(16, 88):
        for col in range(1, 95):
            try:
                chars.add(bytes((row + 0xA0, col + 0xA0)).decode("gb2312"))
            except UnicodeDecodeError:
                continue
    return chars


def template_characters() -> set[str]:
    chars: set[str] = set()
    for pattern in ("resources/**/*.html", "core/help.py", "core/presentation.py"):
        for path in ROOT.glob(pattern):
            chars.update(path.read_text(encoding="utf-8"))
    return {c for c in chars if not c.isspace()}


def build(source_dir: Path) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    latin = set(string.printable) | set(LATIN_EXTRA)
    latin = {c for c in latin if not c.isspace()} | {" "}
    cjk = gb2312_characters() | template_characters() | latin

    jobs = [(name, cjk) for name in CJK_FONTS]
    jobs += [(name, latin) for name in LATIN_FONTS]
    for name, chars in jobs:
        source = next(
            (
                candidate
                for suffix in (".otf", ".ttf")
                if (candidate := source_dir / f"{name}{suffix}").is_file()
            ),
            source_dir / f"{name}.ttf",
        )
        if not source.is_file():
            raise SystemExit(f"missing source font: {source}")
        text_file = OUTPUT_DIR / f".{name}.txt"
        text_file.write_text("".join(sorted(chars)), encoding="utf-8")
        target = OUTPUT_DIR / f"{name}.woff2"
        subset.main(
            [
                str(source),
                f"--text-file={text_file}",
                "--flavor=woff2",
                f"--output-file={target}",
                "--layout-features=*",
                "--no-hinting",
                "--desubroutinize",
            ]
        )
        text_file.unlink()
        print(f"{target.name}: {target.stat().st_size / 1024:.0f} KiB")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    build(Path(sys.argv[1]))
