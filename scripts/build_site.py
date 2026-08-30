from __future__ import annotations

import argparse
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
DATA = ROOT / "data"
DEFAULT_OUTPUT = ROOT / "_site"


def build(output: Path) -> None:
    output = output.resolve()
    shutil.rmtree(output, ignore_errors=True)
    output.mkdir(parents=True, exist_ok=True)

    for name in ("index.html", "app.js", "style.css"):
        shutil.copy2(FRONTEND / name, output / name)

    assets = FRONTEND / "assets"
    if assets.is_dir():
        shutil.copytree(assets, output / "assets")

    shutil.copytree(
        DATA,
        output / "data",
        ignore=shutil.ignore_patterns(
            "preview.png",
            "diagnostic.json",
            ".capture_*",
            ".current_old",
        ),
    )
    (output / ".nojekyll").write_text("", encoding="ascii")

    print(f"Built static site: {output}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()
    build(Path(args.output))


if __name__ == "__main__":
    main()
