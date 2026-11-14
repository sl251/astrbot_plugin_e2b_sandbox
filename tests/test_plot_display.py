"""Verify headless show produces notebook images, without real E2B."""
import sys
import types
import unittest
from unittest.mock import Mock, patch

from helpers import load_main

main = load_main()


class PlotDisplayTests(unittest.TestCase):
    def _run_setup(self, namespace):
        pyplot = types.ModuleType('matplotlib.pyplot')
        pyplot.rcParams = {}
        pyplot.gcf = Mock(return_value=object())
        pyplot.show = Mock()
        pyplot.close = Mock()
        pyplot.get_fignums = Mock(return_value=[1, 2])
        pyplot.figure = Mock(side_effect=lambda i: types.SimpleNamespace(
            savefig=lambda stream, **kwargs: stream.write(('figure-' + str(i)).encode())))
        manager = types.ModuleType('matplotlib.font_manager')
        manager.findfont = Mock(return_value='/template/font.otf')
        manager.FontProperties = Mock()
        manager.fontManager = types.SimpleNamespace(ttflist=[], addfont=Mock())
        matplotlib = types.ModuleType('matplotlib')
        matplotlib.use = Mock()
        matplotlib.pyplot = pyplot
        matplotlib.font_manager = manager
        ft = types.ModuleType('matplotlib.ft2font')
        ft.FT2Font = Mock(return_value=types.SimpleNamespace(family_name='Noto Sans CJK SC',
                                                           get_char_index=lambda i: 1))
        modules = {'matplotlib': matplotlib, 'matplotlib.pyplot': pyplot,
                   'matplotlib.font_manager': manager, 'matplotlib.ft2font': ft}
        with patch.dict(sys.modules, modules):
            exec(main.FONT_SETUP_CODE, namespace)
        return pyplot

    def test_show_displays_every_open_figure_and_closes_them(self):
        display = Mock()
        pyplot = self._run_setup({'display': display})
        pyplot.show()
        import base64
        self.assertEqual([base64.b64decode(c.args[0]['image/png']).decode() for c in display.call_args_list],
                         ['figure-1', 'figure-2'])
        self.assertTrue(all(c.kwargs == {'raw': True} for c in display.call_args_list))
        pyplot.close.assert_called_once_with('all')

    def test_repeated_execution_does_not_wrap_show_recursively(self):
        display = Mock()
        namespace = {'display': display}
        pyplot = self._run_setup(namespace)
        pyplot.show()
        display.reset_mock()
        pyplot = self._run_setup(namespace)
        pyplot.show()
        self.assertEqual(display.call_count, 2)

    def test_non_notebook_environment_keeps_existing_show(self):
        pyplot = self._run_setup({})
        pyplot.show.assert_not_called()
        self.assertIsInstance(pyplot.show, Mock)
