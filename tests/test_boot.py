import unittest

from camol.boot import compose_boot, load_asset


class BootTests(unittest.TestCase):
    def test_wide_layout_starts_with_wordmark_and_biases_camel_right(self):
        rendered = compose_boot(180, 32)
        lines = rendered.splitlines()
        self.assertTrue(lines[0].startswith("      ___"))
        self.assertIn("====", rendered)
        self.assertLessEqual(max(map(len, lines)), 180)

    def test_common_layout_is_bounded_and_not_centered(self):
        for width, height in ((120, 32), (80, 24), (50, 18), (24, 10)):
            with self.subTest(size=(width, height)):
                lines = compose_boot(width, height).splitlines()
                self.assertLessEqual(len(lines), height)
                self.assertLessEqual(max(map(len, lines)), width)
                self.assertTrue(lines[0].lstrip().startswith(("___", "____", "CAMOL")))

    def test_asset_path_traversal_is_rejected(self):
        with self.assertRaises(ValueError):
            load_asset("../secret")


if __name__ == "__main__":
    unittest.main()
