"""Load the add-on's pure modules outside NVDA.

NVDA-only modules (addonHandler, logHandler, globalVars, wx, winsound...) are
replaced by small stubs, and the ``AIHub`` package is registered without
running its ``__init__`` (which needs a live NVDA), so tests can import
``AIHub.apiclient``, ``AIHub.pricingutils`` and the like.
"""

import builtins
import logging
import os
import sys
import tempfile
import types
from unittest import mock

ADDON_PACKAGE_DIR = os.path.join(os.path.dirname(__file__), os.pardir, "addon", "globalPlugins", "AIHub")


def _stub(name, **attrs):
	module = types.ModuleType(name)
	module.__dict__.update(attrs)
	sys.modules.setdefault(name, module)
	return module


builtins._ = lambda text: text
builtins.ngettext = lambda singular, plural, count: singular if count == 1 else plural
_stub("addonHandler", initTranslation=lambda: None, getCodeAddon=mock.MagicMock())
_stub("logHandler", log=logging.getLogger("AIHub"))
_stub("globalVars", appArgs=types.SimpleNamespace(configPath=tempfile.mkdtemp(prefix="aihub-tests-")))
_stub("config", conf={})
if "winsound" not in sys.modules:
	try:
		import winsound  # noqa: F401
	except ImportError:
		_stub("winsound", PlaySound=lambda *a, **k: None, SND_ASYNC=1, SND_LOOP=8)
sys.modules.setdefault("wx", mock.MagicMock(name="wx"))

package = types.ModuleType("AIHub")
package.__path__ = [os.path.abspath(ADDON_PACKAGE_DIR)]
sys.modules.setdefault("AIHub", package)
