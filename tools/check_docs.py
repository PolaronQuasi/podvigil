"""Check local documentation links and SVG validity without making network requests."""

from pathlib import Path
import re
import urllib.parse
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]


def main():
    errors = []
    paths = [*ROOT.glob("*.md"), *ROOT.glob("docs/**/*.md")]
    for path in paths:
        content = path.read_text(encoding="utf-8")
        for raw in re.findall(r"!?\[[^\]]*\]\(([^)]+)\)", content):
            target = raw.split(' "', 1)[0].strip("<>")
            parsed = urllib.parse.urlsplit(target)
            if parsed.scheme or not parsed.path:
                continue
            local = path.parent / urllib.parse.unquote(parsed.path)
            if not local.exists():
                errors.append(str(path.relative_to(ROOT)) + ": " + target)
    for svg in [*ROOT.glob("assets/*.svg"), *ROOT.glob("docs/diagrams/*.svg")]:
        ET.parse(svg)
    if errors:
        raise RuntimeError("Broken local links:\n" + "\n".join(errors))
    print("Documentation links and SVG syntax passed.")


if __name__ == "__main__":
    main()
