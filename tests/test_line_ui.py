import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

from camol.app import CommandResponse
from camol.line_ui import run_line_ui


class LineUiTests(unittest.TestCase):
    def test_control_c_during_native_login_detaches_cleanly(self):
        controller = Mock()
        controller.handle.return_value = CommandResponse(
            login_argv=("claude", "auth", "login"),
            login_provider="claude",
        )
        output = StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            with (
                patch("camol.line_ui.InteractiveController", return_value=controller),
                patch("builtins.input", return_value="/login claude"),
                patch("camol.line_ui.subprocess.run", side_effect=KeyboardInterrupt),
                redirect_stdout(output),
            ):
                result = run_line_ui(Path(temporary), show_boot=False)

        self.assertEqual(result, 0)
        self.assertIn("Provider login cancelled; client detached", output.getvalue())


if __name__ == "__main__":
    unittest.main()
