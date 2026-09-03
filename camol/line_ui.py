"""Dependency-light terminal fallback when the Textual extra is unavailable."""

import shutil
import subprocess
from pathlib import Path
from typing import Optional

from .app import InteractiveController
from .boot import compose_boot


def run_line_ui(workspace: Path, *, state_root: Optional[Path] = None, show_boot: bool = True) -> int:
    controller = InteractiveController(workspace, state_root=state_root)
    size = shutil.get_terminal_size((100, 30))
    if show_boot:
        print(compose_boot(size.columns, size.lines - 2))
    print("Camol Product V0 line mode. Install `camol-harness[tui]` for the full windowed terminal client.")
    print("No work starts from conversation alone. Type /help or /grill GOAL.")
    while True:
        try:
            text = input("camol> ")
        except (EOFError, KeyboardInterrupt):
            print("\ndetached")
            return 0
        response = controller.handle(text)
        for message in response.messages:
            print(message)
        if response.login_argv:
            completed = subprocess.run(list(response.login_argv), check=False)
            print("provider login exited {}; run /connections to refresh".format(completed.returncode))
        if response.exit_client:
            return 0
