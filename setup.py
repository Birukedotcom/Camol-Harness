"""Compatibility metadata for the pip bundled with older macOS Python."""

from setuptools import setup


setup(
    name="camol-harness",
    version="0.2.0",
    description="A persistent, plan-driven multi-agent orchestration harness.",
    python_requires=">=3.9",
    packages=["camol"],
    package_data={"camol": ["assets/*.txt", "assets/profiles/*.json"]},
    extras_require={"tui": ["textual>=8.0,<9"]},
    entry_points={"console_scripts": ["camol=camol.cli:main"]},
)
