"""Responsive plaintext boot composition for real terminals."""

from importlib import resources
from typing import List


def load_asset(name: str) -> str:
    if "/" in name or ".." in name:
        raise ValueError("boot asset name is invalid")
    return resources.files("camol").joinpath("assets", name).read_text(encoding="utf-8").rstrip("\n")


def _right(lines: List[str], width: int) -> List[str]:
    return [(" " * max(0, width - len(line))) + line for line in lines]


def compose_boot(width: int, height: int) -> str:
    """Return a top-left wordmark and right-biased camel for the terminal size."""
    width = max(20, int(width))
    height = max(6, int(height))
    wide_mark = load_asset("wordmark.txt").splitlines()
    compact_mark = load_asset("wordmark-compact.txt").splitlines()

    if width >= 146 and height >= 21:
        camel = load_asset("camel-wide.txt").splitlines()
        gap = 3
        left_width = max(len(line) for line in wide_mark)
        available = width - left_width - gap
        right = _right(camel, available)
        rows = []
        for index in range(max(len(wide_mark), len(right))):
            left = wide_mark[index] if index < len(wide_mark) else ""
            camel_line = right[index] if index < len(right) else ""
            rows.append(left.ljust(left_width) + (" " * gap) + camel_line)
    elif width >= 70 and height >= 22:
        camel = _right(load_asset("camel-medium.txt").splitlines(), width)
        rows = wide_mark + [""] + camel
    else:
        camel = _right(load_asset("camel-compact.txt").splitlines(), width)
        mark = compact_mark if width >= 34 else ["CAMOL"]
        rows = mark + [""] + camel

    footer = "orchestrate  •  prove  •  refine"
    if len(rows) + 2 <= height:
        rows += ["", footer[:width]]
    return "\n".join(line[:width].rstrip() for line in rows[:height])
