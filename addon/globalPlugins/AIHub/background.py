"""Run blocking work off the UI thread and hand results back safely.

Every tool dialog and the model list follow the same pattern: start a daemon
thread for a network call, then deliver its result on the wx main thread.
The result must be dropped if the window was closed meanwhile, otherwise the
callback touches a destroyed wx object.
"""

from __future__ import annotations

import threading
from typing import Any, Callable

import wx
from logHandler import log


def window_alive(window) -> bool:
	"""True while ``window`` exists and is not being destroyed."""
	try:
		return bool(window) and not window.IsBeingDeleted()
	except RuntimeError:
		# wrapped C++ object has been deleted
		return False


def call_after_if_alive(window, fn: Callable, *args: Any, **kwargs: Any) -> None:
	"""``wx.CallAfter(fn, ...)``, skipped if ``window`` was closed in the meantime."""

	def _deliver():
		if not window_alive(window):
			log.debug("Dropped background result for a closed window: %s", getattr(fn, "__name__", fn))
			return
		fn(*args, **kwargs)

	wx.CallAfter(_deliver)


def start_background(target: Callable, *args: Any, name: str | None = None) -> threading.Thread:
	"""Start ``target(*args)`` in a daemon thread and return the thread."""
	thread = threading.Thread(target=target, args=args, name=name, daemon=True)
	thread.start()
	return thread
