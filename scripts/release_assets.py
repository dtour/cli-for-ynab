"""Build a versioned release and its Homebrew formula without publishing anything."""

import argparse
import hashlib
import subprocess
import tomllib
from pathlib import Path

from homebrew_formula import generate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    args = parser.parse_args()
    project = tomllib.loads(Path("pyproject.toml").read_text())
    version = project["project"]["version"]
    from ynab_cli import __version__

    if args.tag != f"v{version}" or __version__ != version:
        parser.error("Tag, project version, and CLI version must match")
    if not Path(f"docs/releases/{args.tag}.md").is_file():
        parser.error("Write versioned release notes before building a release")
    subprocess.run(["uv", "build"], check=True)
    wheel = Path(f"dist/cli_for_ynab-{version}-py3-none-any.whl")
    source = Path(f"dist/cli_for_ynab-{version}.tar.gz")
    formula = Path("dist/cli-for-ynab.rb")
    url = f"https://github.com/dtour/cli-for-ynab/releases/download/{args.tag}/{wheel.name}"
    formula.write_text(generate(wheel, url, tomllib.loads(Path("uv.lock").read_text())))
    checksums = "".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
        for path in (wheel, source, formula)
    )
    Path("dist/SHA256SUMS").write_text(checksums)
    print(f"Built {args.tag}: wheel, source archive, formula, SHA256SUMS")


if __name__ == "__main__":
    main()
