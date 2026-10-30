"""Response verbosity control (OpenAI ``verbosity``, forwarded by OpenRouter)."""

from __future__ import annotations

import wx

import addonHandler

from ..consts import Provider, UI_SECTION_SPACING_PX
from ._base import ChromeSection

addonHandler.initTranslation()

# https://developers.openai.com/api/reference/resources/responses/methods/create (text.verbosity)
# https://openrouter.ai/docs/api/reference/parameters (verbosity)
VERBOSITY_VALUES = ("", "low", "medium", "high")
# Providers whose API honors ``verbosity``; others may list it in catalog
# metadata (OpenRouter-style parameter names) without accepting it.
_VERBOSITY_PROVIDERS = (Provider.OpenAI, Provider.OpenRouter)


def model_supports_verbosity(model) -> bool:
	return bool(
		model
		and getattr(model, "provider", None) in _VERBOSITY_PROVIDERS
		and model.allows_request_parameter("verbosity")
	)


def selected_verbosity(dialog, model) -> str:
	"""Selected verbosity for ``model``, or ``""`` to let the API default apply."""
	choice = getattr(dialog, "verbosityChoice", None)
	if choice is None or not model_supports_verbosity(model):
		return ""
	idx = choice.GetSelection()
	return VERBOSITY_VALUES[idx] if 0 <= idx < len(VERBOSITY_VALUES) else ""


class VerbosityChromeSection(ChromeSection):
	def build(self, sizer: wx.Sizer) -> None:
		d = self.dialog
		d.verbosityRow = wx.Panel(self.parent)
		row_sz = wx.BoxSizer(wx.VERTICAL)
		# Translators: Label for the combo box choosing how long and detailed model answers are.
		d.verbosityLabel = wx.StaticText(d.verbosityRow, label=_("&Verbosity:"))
		d.verbosityChoice = wx.Choice(
			d.verbosityRow,
			choices=[
				# Translators: Verbosity combo entry: do not send the parameter (API default).
				_("Default"),
				# Translators: Verbosity combo entry: concise answers.
				_("Low"),
				# Translators: Verbosity combo entry.
				_("Medium"),
				# Translators: Verbosity combo entry: detailed answers.
				_("High"),
			],
		)
		d.verbosityChoice.SetSelection(0)
		d.verbosityChoice.Bind(wx.EVT_CHOICE, self._edited)
		row_sz.Add(d.verbosityLabel, 0, wx.LEFT | wx.RIGHT | wx.TOP, UI_SECTION_SPACING_PX)
		row_sz.Add(d.verbosityChoice, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, UI_SECTION_SPACING_PX)
		d.verbosityRow.SetSizer(row_sz)
		sizer.Add(d.verbosityRow, 0, wx.EXPAND, 0)
		self._bind_preserve(d.verbosityChoice)

	def update_for_model(self, model) -> None:
		visible = model_supports_verbosity(model)
		self.dialog.verbosityRow.Show(visible)
		self.dialog.verbosityChoice.Enable(visible)

	def capture(self, st: dict, model) -> None:
		if model_supports_verbosity(model):
			st["verbosity"] = selected_verbosity(self.dialog, model)

	def apply(self, st: dict, model) -> None:
		value = st.get("verbosity")
		if value in VERBOSITY_VALUES:
			self.dialog.verbosityChoice.SetSelection(VERBOSITY_VALUES.index(value))
		self.update_for_model(model)
