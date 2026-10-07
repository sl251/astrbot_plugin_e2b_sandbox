import contextlib
import io
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from helpers import TestDirectory, load_main

main = load_main()


class FontSetupTests(unittest.TestCase):
    def setUp(self):
        self.directory = TestDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cache = Path(self.directory.name)
        self.font_path = self.cache / "NotoSansCJKsc-Regular.otf"
        self.valid_font = b"OTTO" + b"x" * 2048
        self.manager = types.SimpleNamespace(ttflist=[], addfont=Mock())
        self.manager.addfont.side_effect = lambda path: self.manager.ttflist.append(types.SimpleNamespace(fname=path))
        self.fm = types.ModuleType("matplotlib.font_manager")
        self.fm.fontManager = self.manager
        self.fm.findfont = Mock(side_effect=ValueError("not installed"))
        self.fm.FontProperties = Mock()
        self.plt = types.ModuleType("matplotlib.pyplot")
        self.plt.rcParams = {}
        matplotlib = types.ModuleType("matplotlib")
        matplotlib.use = Mock()
        matplotlib.font_manager, matplotlib.pyplot = self.fm, self.plt
        ft = types.ModuleType("matplotlib.ft2font")
        ft.FT2Font = self.parse_font
        self.modules = {
            "matplotlib": matplotlib, "matplotlib.font_manager": self.fm,
            "matplotlib.pyplot": self.plt, "matplotlib.ft2font": ft,
        }

    def parse_font(self, path):
        if Path(path).read_bytes() != self.valid_font:
            raise ValueError("invalid font")
        return types.SimpleNamespace(family_name="Noto Sans CJK SC", get_char_index=lambda code: 1)

    def run_setup(self, data=None, error=None):
        response = io.BytesIO(self.valid_font if data is None else data)
        response.headers = {}
        code = main.FONT_SETUP_CODE.replace("'/tmp/astrbot-fonts'", repr(str(self.cache)))
        stderr = io.StringIO()
        with patch.dict(sys.modules, self.modules), contextlib.redirect_stderr(stderr):
            with patch("urllib.request.urlopen", return_value=response, side_effect=error) as download:
                namespace = {}
                exec(code + "\nuser_code_ran = True", namespace)
        self.assertTrue(namespace["user_code_ran"])
        return download, stderr.getvalue()

    def test_corrupt_cache_is_replaced_and_reused(self):
        self.font_path.write_bytes(b"<html>rate limited</html>")
        download, warning = self.run_setup()
        download.assert_called_once()
        self.assertEqual(warning, "")
        self.assertEqual(self.font_path.read_bytes(), self.valid_font)
        download, _ = self.run_setup()
        download.assert_not_called()
        self.manager.addfont.assert_called_once()

    def test_html_and_broken_font_are_never_cached_and_next_run_recovers(self):
        for invalid in (b"<html>" + b"x" * 2048, b"OTTO" + b"broken" * 256, b"tiny"):
            with self.subTest(invalid=invalid[:10]):
                _, warning = self.run_setup(data=invalid)
                self.assertIn("will retry next execution", warning)
                self.assertEqual(list(self.cache.iterdir()), [])
        self.run_setup()
        self.assertEqual(self.font_path.read_bytes(), self.valid_font)

    def test_network_failure_does_not_prevent_user_code_or_poison_cache(self):
        _, warning = self.run_setup(error=TimeoutError("unavailable"))
        self.assertIn("unavailable", warning)
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_template_font_avoids_download(self):
        installed = self.cache / "installed.otf"
        installed.write_bytes(self.valid_font)
        self.fm.findfont.side_effect = None
        self.fm.findfont.return_value = str(installed)
        download, warning = self.run_setup()
        download.assert_not_called()
        self.assertEqual(warning, "")
        self.assertEqual(self.plt.rcParams["font.sans-serif"][0], "Noto Sans CJK SC")
