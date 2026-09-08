"""Compatibility metadata for the pip bundled with older macOS Python."""

from setuptools import setup
from pathlib import Path
import re


version = re.search(r'__version__ = "([^"]+)"', (Path(__file__).parent / "camol" / "_version.py").read_text(encoding="utf-8")).group(1)


setup(
    name="camol-harness",
    version=version,
    description="A persistent, plan-driven multi-agent orchestration harness.",
    python_requires=">=3.9",
    packages=["camol"],
    package_data={"camol": ["assets/*.txt", "assets/profiles/*.json"]},
    extras_require={"tui": ["textual>=8.0,<9"], "graph": ["tomli>=2,<3; python_version < '3.11'"]},
    entry_points={"console_scripts": ["camol=camol.cli:main"]},
)
