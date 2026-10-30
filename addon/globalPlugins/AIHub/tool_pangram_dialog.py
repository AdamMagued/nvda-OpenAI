"""Dedicated dialog for Pangram Labs AI detection and plagiarism checks."""

import json
import os
import re
import threading
import winsound

import addonHandler
import api
import ui
import wx
from logHandler import log

from . import pangramapi
from .conversations import ConversationFormat
from .consts import (
	Provider,
	SND_CHAT_RESPONSE_RECEIVED,
	SND_PROGRESS,
	stop_progress_sound,
	UI_DIALOG_BORDER_PX,
	UI_FORM_ROW_BORDER_PX,
	UI_SECTION_SPACING_PX,
)
from .mediastore import build_media_path
from .providertools_helpers import add_labeled_factory
from .thread_shutdown import stop_worker_thread
from .background import call_after_if_alive, start_background
from .tool_dialog_base import ToolDialogBase

addonHandler.initTranslation()

ACTION_DETECT_TEXT = "detect_text"
ACTION_DETECT_FILES = "detect_files"
ACTION_BULK = "bulk"
ACTION_PLAGIARISM = "plagiarism"
ACTION_LIST_MODELS = "list_models"

# Local text files whose content can be read and sent as bulk items.
BULK_TEXT_EXTENSIONS = (".txt", ".md", ".markdown", ".text", ".csv", ".html", ".htm", ".xml", ".json")
# A line containing only three or more dashes separates bulk items typed in the text field.
_BULK_SEPARATOR_RE = re.compile(r"^\s*-{3,}\s*$", re.MULTILINE)
_RESPONSE_PREVIEW_CHARS = 4000


def _percent(value) -> str:
	try:
		return "%.1f%%" % (float(value) * 100.0)
	except (TypeError, ValueError):
		return "-"


def _yes_no(value) -> str:
	# Translators: Pangram tool report — boolean value «yes».
	return _("yes") if value else _("no")


def _read_text_file(path: str) -> str:
	with open(path, "rb") as f:
		raw = f.read()
	for encoding in ("utf-8-sig", "cp1252"):
		try:
			return raw.decode(encoding)
		except UnicodeDecodeError:
			continue
	return raw.decode("utf-8", errors="replace")


def format_detection_result(result: dict, include_windows: bool = True) -> str:
	"""Human-readable report for one AI detection result (task, bulk item, or file)."""
	if not isinstance(result, dict):
		return ""
	lines = []
	if result.get("headline"):
		# Translators: Pangram tool report — line with the short classification headline; placeholder is e.g. «AI Assisted».
		lines.append(_("Headline: %s") % result["headline"])
	if result.get("prediction"):
		# Translators: Pangram tool report — line with the long-form classification sentence.
		lines.append(_("Prediction: %s") % result["prediction"])
	if result.get("prediction_short"):
		# Translators: Pangram tool report — line with the short prediction («AI», «Human» or «Mixed»).
		lines.append(_("Short prediction: %s") % result["prediction_short"])
	if any(k in result for k in ("fraction_ai", "fraction_ai_assisted", "fraction_human")):
		lines.append(
			# Translators: Pangram tool report — share of the text per class; placeholders are percentages (AI-generated, AI-assisted, human).
			_("AI-generated: {ai}, AI-assisted: {assisted}, human: {human}").format(
				ai=_percent(result.get("fraction_ai")),
				assisted=_percent(result.get("fraction_ai_assisted")),
				human=_percent(result.get("fraction_human")),
			)
		)
	if any(k in result for k in ("num_ai_segments", "num_ai_assisted_segments", "num_human_segments")):
		lines.append(
			# Translators: Pangram tool report — number of segments per class.
			_("Segments: {ai} AI-generated, {assisted} AI-assisted, {human} human").format(
				ai=result.get("num_ai_segments", 0),
				assisted=result.get("num_ai_assisted_segments", 0),
				human=result.get("num_human_segments", 0),
			)
		)
	if result.get("version"):
		# Translators: Pangram tool report — line with the Pangram model/API version (e.g. «4.0»).
		lines.append(_("Model version: %s") % result["version"])
	link = pangramapi.dashboard_link(result)
	if link:
		# Translators: Pangram tool report — line with the public dashboard URL.
		lines.append(_("Dashboard: %s") % link)
	windows = result.get("windows")
	if include_windows and isinstance(windows, list) and windows:
		lines.append("")
		# Translators: Pangram tool report — heading before the per-segment (window) analysis.
		lines.append(_("Segments detail:"))
		for idx, window in enumerate(windows, start=1):
			if not isinstance(window, dict):
				continue
			details = []
			if window.get("confidence"):
				# Translators: Pangram tool report — segment confidence («High», «Medium», «Low»).
				details.append(_("confidence %s") % window["confidence"])
			if window.get("ai_assistance_score") is not None:
				# Translators: Pangram tool report — segment AI assistance score as a percentage.
				details.append(_("AI assistance %s") % _percent(window["ai_assistance_score"]))
			if window.get("is_humanized") is not None:
				# Translators: Pangram tool report — whether the segment was classified as humanized, with the humanizer score.
				details.append(_("humanized: {value} ({score})").format(
					value=_yes_no(window.get("is_humanized")),
					score=_percent(window.get("humanizer_score")),
				))
			if window.get("word_count") is not None:
				# Translators: Pangram tool report — number of words in a segment.
				details.append(_("%s words") % window["word_count"])
			if window.get("start_index") is not None and window.get("end_index") is not None:
				# Translators: Pangram tool report — character range of a segment in the analyzed text.
				details.append(_("characters {start}-{end}").format(start=window["start_index"], end=window["end_index"]))
			# Translators: Pangram tool report — segment header; placeholders are the segment number and its label (e.g. «AI-Generated»).
			header = _("Segment {num}: {label}").format(num=idx, label=window.get("label") or "?")
			if details:
				header += " (" + ", ".join(details) + ")"
			lines.append(header)
			text = window.get("text")
			if isinstance(text, str) and text.strip():
				lines.append(text.strip())
			lines.append("")
	return "\n".join(lines).rstrip()


def format_plagiarism_result(result: dict) -> str:
	if not isinstance(result, dict):
		return ""
	lines = [
		# Translators: Pangram tool report — whether plagiarism was detected.
		_("Plagiarism detected: %s") % _yes_no(result.get("plagiarism_detected")),
	]
	if result.get("percent_plagiarized") is not None:
		try:
			percent = "%.1f%%" % float(result.get("percent_plagiarized"))
		except (TypeError, ValueError):
			percent = str(result.get("percent_plagiarized"))
		# Translators: Pangram tool report — percentage of sentences matching online content.
		lines.append(_("Plagiarized: %s") % percent)
	if result.get("total_sentences") is not None:
		lines.append(
			# Translators: Pangram tool report — number of plagiarized sentences out of the total.
			_("Plagiarized sentences: {count} of {total}").format(
				count=result.get("plagiarized_sentences", 0),
				total=result.get("total_sentences", 0),
			)
		)
	matches = result.get("plagiarized_content")
	if isinstance(matches, list) and matches:
		lines.append("")
		# Translators: Pangram tool report — heading before the list of matched online sources.
		lines.append(_("Matched sources:"))
		for idx, match in enumerate(matches, start=1):
			if not isinstance(match, dict):
				continue
			lines.append(
				# Translators: Pangram tool report — one matched source; placeholders are the match number, source URL and similarity percentage.
				_("{num}. {url} (similarity {score})").format(
					num=idx,
					url=match.get("source_url") or "?",
					score=_percent(match.get("similarity_score")),
				)
			)
			text = match.get("matched_text")
			if isinstance(text, str) and text.strip():
				lines.append(text.strip())
			lines.append("")
	return "\n".join(lines).rstrip()


def format_bulk_result(data: dict) -> str:
	if not isinstance(data, dict):
		return ""
	status = data.get("status") or {}
	lines = []
	if status.get("bulk_id"):
		# Translators: Pangram tool report — bulk job identifier.
		lines.append(_("Bulk job: %s") % status["bulk_id"])
	if status.get("status"):
		# Translators: Pangram tool report — bulk job terminal status («succeeded», «failed», «partial»).
		lines.append(_("Status: %s") % status["status"])
	if status.get("total_items") is not None:
		lines.append(
			# Translators: Pangram tool report — bulk counters.
			_("Items: {total} submitted, {succeeded} succeeded, {failed} failed").format(
				total=status.get("total_items", 0),
				succeeded=status.get("succeeded", 0),
				failed=status.get("failed", 0),
			)
		)
	entries = []
	for item in data.get("items") or []:
		if isinstance(item, dict):
			entries.append(item)
	for item in data.get("failed_items") or []:
		if isinstance(item, dict):
			entries.append(item)
	for item in (data.get("submission") or {}).get("failed_items") or []:
		if isinstance(item, dict) and not any(e.get("index") == item.get("index") for e in entries):
			entries.append(item)
	entries.sort(key=lambda e: e.get("index", 0) if isinstance(e.get("index"), int) else 0)
	for item in entries:
		lines.append("")
		name = item.get("id") or "#%s" % (item.get("index", 0) + 1 if isinstance(item.get("index"), int) else "?")
		# Translators: Pangram tool report — heading of one bulk item; placeholder is the item id or number.
		lines.append(_("Item %s") % name)
		result = item.get("result")
		if isinstance(result, dict) and result.get("stage") == pangramapi.STAGE_SUCCESS:
			lines.append(format_detection_result(result, include_windows=False))
		else:
			error = item.get("error") or (result or {}).get("headline") or item.get("stage") or ""
			# Translators: Pangram tool report — error for one bulk item; placeholder is the server message.
			lines.append(_("Failed: %s") % error)
	return "\n".join(lines).rstrip()


def format_files_result(file_paths: list, results: list) -> str:
	lines = []
	for idx, result in enumerate(results):
		if lines:
			lines.append("")
		name = ""
		if isinstance(result, dict):
			name = result.get("filename") or ""
		if not name and idx < len(file_paths):
			name = os.path.basename(file_paths[idx])
		# Translators: Pangram tool report — heading of one uploaded file result; placeholder is the file name.
		lines.append(_("File: %s") % (name or "#%d" % (idx + 1)))
		body = format_detection_result(result)
		if body:
			lines.append(body)
		else:
			# Translators: Pangram tool report — shown when the file endpoint returned no detection details (only a dashboard link, or nothing).
			lines.append(_("No detailed result returned. Open the dashboard link to see the analysis."))
	return "\n".join(lines).rstrip()


class PangramToolDialog(ToolDialogBase):
	ACTIONS = (
		# Translators: Pangram tool — Action drop-down entry: AI detection on the text typed or pasted in the dialog.
		(ACTION_DETECT_TEXT, _("AI detection (text)")),
		# Translators: Pangram tool — Action drop-down entry: AI detection on uploaded .docx, .pdf or .rtf documents.
		(ACTION_DETECT_FILES, _("AI detection (documents: DOCX, PDF, RTF)")),
		# Translators: Pangram tool — Action drop-down entry: asynchronous bulk AI detection on several texts at once.
		(ACTION_BULK, _("Bulk AI detection (several texts)")),
		# Translators: Pangram tool — Action drop-down entry: check the text for plagiarism against online sources.
		(ACTION_PLAGIARISM, _("Plagiarism check")),
		# Translators: Pangram tool — Action drop-down entry: list the detection models available to the API key.
		(ACTION_LIST_MODELS, _("List available models")),
	)

	def __init__(self, parent, conversationData=None, parentDialog=None, plugin=None):
		super().__init__(
			parent,
			# Translators: Window title of the AI-Hub Pangram tool dialog (AI detection and plagiarism check).
			title=_("Tool: Pangram"),
			provider=Provider.Pangram,
			size=(860, 800),
			parentDialog=parentDialog,
			plugin=plugin,
		)
		self._worker = None
		self._stopEvent = threading.Event()
		self._lastAction = ""
		self._reportPath = ""
		self._jsonPath = ""
		self._dashboardLinks = []

		dialogSizer = wx.BoxSizer(wx.VERTICAL)
		self.formPanel = wx.Panel(self)
		main = wx.BoxSizer(wx.VERTICAL)

		self.accountChoice = add_labeled_factory(
			self.formPanel,
			main,
			# Translators: Label before the Pangram account drop-down in the Pangram tool.
			_("&Account:"),
			lambda: self.build_account_choice(self.formPanel),
		)
		self.actionChoice = add_labeled_factory(
			self.formPanel,
			main,
			# Translators: Label before the drop-down selecting which Pangram operation to run.
			_("A&ction:"),
			lambda: wx.Choice(self.formPanel, choices=[label for _key, label in self.ACTIONS]),
		)
		self.actionChoice.SetSelection(0)
		self.actionChoice.Bind(wx.EVT_CHOICE, self.onActionChanged)

		self.modelLabel = wx.StaticText(
			self.formPanel,
			# Translators: Label before the editable Pangram model selector combo box («default» follows Pangram's current default model).
			label=_("&Model:"),
		)
		main.Add(self.modelLabel, 0, wx.LEFT | wx.RIGHT | wx.TOP, UI_FORM_ROW_BORDER_PX)
		self.modelCombo = wx.ComboBox(
			self.formPanel,
			value=pangramapi.DEFAULT_MODEL,
			choices=[pangramapi.DEFAULT_MODEL],
			style=wx.CB_DROPDOWN,
		)
		main.Add(self.modelCombo, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, UI_FORM_ROW_BORDER_PX)
		# Translators: Button that fetches the model selectors available to the selected Pangram API key.
		self.refreshModelsBtn = wx.Button(self.formPanel, label=_("&Refresh model list"))
		self.refreshModelsBtn.Bind(wx.EVT_BUTTON, self.onRefreshModels)
		main.Add(self.refreshModelsBtn, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, UI_FORM_ROW_BORDER_PX)

		self.inputLabel = wx.StaticText(self.formPanel, label="")
		main.Add(self.inputLabel, 0, wx.LEFT | wx.RIGHT | wx.TOP, UI_FORM_ROW_BORDER_PX)
		self.inputText = wx.TextCtrl(self.formPanel, style=wx.TE_MULTILINE, size=(-1, 160))
		main.Add(self.inputText, 1, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, UI_FORM_ROW_BORDER_PX)
		self.inputText.Bind(wx.EVT_TEXT, self.onInputTextChanged)
		self.wordCountLabel = wx.StaticText(self.formPanel, label="")
		main.Add(self.wordCountLabel, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, UI_FORM_ROW_BORDER_PX)

		inputButtons = wx.BoxSizer(wx.HORIZONTAL)
		# Translators: Button that loads the content of a local text file into the Pangram text field.
		self.loadTextBtn = wx.Button(self.formPanel, label=_("&Load text file..."))
		self.loadTextBtn.Bind(wx.EVT_BUTTON, self.onLoadTextFile)
		# Translators: Button that pastes the clipboard text into the Pangram text field.
		self.pasteBtn = wx.Button(self.formPanel, label=_("&Paste from clipboard"))
		self.pasteBtn.Bind(wx.EVT_BUTTON, self.onPasteClipboard)
		# Translators: Button that copies the last assistant response of the AI-Hub conversation into the Pangram text field.
		self.useLastResponseBtn = wx.Button(self.formPanel, label=_("Use last conversation r&esponse"))
		self.useLastResponseBtn.Bind(wx.EVT_BUTTON, self.onUseLastResponse)
		for btn in (self.loadTextBtn, self.pasteBtn, self.useLastResponseBtn):
			inputButtons.Add(btn, 0, wx.RIGHT, UI_SECTION_SPACING_PX)
		main.Add(inputButtons, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, UI_FORM_ROW_BORDER_PX)

		self.filesLabel = wx.StaticText(self.formPanel, label="")
		main.Add(self.filesLabel, 0, wx.LEFT | wx.RIGHT | wx.TOP, UI_FORM_ROW_BORDER_PX)
		self.filesList = wx.ListBox(self.formPanel, style=wx.LB_EXTENDED, size=(-1, 90))
		main.Add(self.filesList, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, UI_FORM_ROW_BORDER_PX)
		self.filesList.Bind(wx.EVT_KEY_DOWN, self.onFilesListKey)
		fileButtons = wx.BoxSizer(wx.HORIZONTAL)
		# Translators: Button that opens a file picker to add files to the Pangram file list.
		self.addFilesBtn = wx.Button(self.formPanel, label=_("A&dd files..."))
		self.addFilesBtn.Bind(wx.EVT_BUTTON, self.onAddFiles)
		# Translators: Button that removes the selected files from the Pangram file list.
		self.removeFilesBtn = wx.Button(self.formPanel, label=_("Re&move selected files"))
		self.removeFilesBtn.Bind(wx.EVT_BUTTON, self.onRemoveFiles)
		fileButtons.Add(self.addFilesBtn, 0, wx.RIGHT, UI_SECTION_SPACING_PX)
		fileButtons.Add(self.removeFilesBtn, 0, wx.RIGHT, UI_SECTION_SPACING_PX)
		main.Add(fileButtons, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, UI_FORM_ROW_BORDER_PX)

		# Translators: Checkbox asking Pangram to create a public dashboard link for the result.
		self.dashboardCheck = wx.CheckBox(self.formPanel, label=_("Create public &dashboard link"))
		main.Add(self.dashboardCheck, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, UI_FORM_ROW_BORDER_PX)

		self.resultText = add_labeled_factory(
			self.formPanel,
			main,
			# Translators: Label before the read-only multiline report produced by the last Pangram run.
			_("Re&sult:"),
			lambda: wx.TextCtrl(self.formPanel, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 200)),
		)

		resultButtons = wx.BoxSizer(wx.HORIZONTAL)
		# Translators: Button that opens the Pangram public dashboard link(s) of the last result in the browser.
		self.openDashboardBtn = wx.Button(self.formPanel, label=_("Open dashboard &link"))
		self.openDashboardBtn.Bind(wx.EVT_BUTTON, self.onOpenDashboard)
		# Translators: Button that copies the Pangram report text to the clipboard.
		self.copyResultBtn = wx.Button(self.formPanel, label=_("Cop&y result"))
		self.copyResultBtn.Bind(wx.EVT_BUTTON, self.onCopyResult)
		# Translators: Button that opens the raw JSON response saved from the last Pangram run.
		self.openJsonBtn = wx.Button(self.formPanel, label=_("Open &JSON result"))
		self.openJsonBtn.Bind(wx.EVT_BUTTON, self.onOpenJson)
		for btn in (self.openDashboardBtn, self.copyResultBtn, self.openJsonBtn):
			resultButtons.Add(btn, 0, wx.RIGHT, UI_SECTION_SPACING_PX)
		main.Add(resultButtons, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, UI_FORM_ROW_BORDER_PX)

		buttons = wx.BoxSizer(wx.HORIZONTAL)
		# Translators: Button that sends the request to Pangram for the selected action.
		self.runBtn = wx.Button(self.formPanel, label=_("R&un"))
		self.runBtn.Bind(wx.EVT_BUTTON, self.onRun)
		self.bind_ctrl_enter_submit(self.onRun)
		self.closeBtn = wx.Button(self.formPanel, id=wx.ID_CLOSE)
		self.closeBtn.Bind(wx.EVT_BUTTON, self.onClose)
		buttons.Add(self.runBtn, 0, wx.ALL, UI_SECTION_SPACING_PX)
		buttons.Add(self.closeBtn, 0, wx.ALL, UI_SECTION_SPACING_PX)
		main.Add(buttons, 0, wx.ALIGN_RIGHT | wx.ALL, UI_SECTION_SPACING_PX)

		self.formPanel.SetSizer(main)
		dialogSizer.Add(self.formPanel, 1, wx.EXPAND | wx.ALL, UI_DIALOG_BORDER_PX)
		self.SetSizer(dialogSizer)
		if parent:
			self.CentreOnParent(wx.BOTH)
		else:
			self.Centre(wx.BOTH)
		self._applyConversationData(conversationData)
		self._syncActionControls()
		self._syncResultButtons()
		self._updateWordCount()
		wx.CallAfter(self._focusInitialControl)

	# --- helpers -----------------------------------------------------------------

	def _currentAction(self) -> str:
		idx = self.actionChoice.GetSelection()
		if idx < 0 or idx >= len(self.ACTIONS):
			idx = 0
		return self.ACTIONS[idx][0]

	def _selectAction(self, action: str):
		for idx, (key, _label) in enumerate(self.ACTIONS):
			if key == action:
				self.actionChoice.SetSelection(idx)
				return

	def _selectedModel(self) -> str:
		return self.modelCombo.GetValue().strip() or pangramapi.DEFAULT_MODEL

	def _fileItems(self) -> list:
		return [self.filesList.GetString(i) for i in range(self.filesList.GetCount())]

	def _parentLastResponse(self) -> str:
		parent = self.parentDialog
		block = getattr(parent, "lastBlock", None) if parent else None
		text = getattr(block, "responseText", "") if block is not None else ""
		return text if isinstance(text, str) else ""

	def _focusInitialControl(self):
		if not self._isDialogAlive():
			return
		if self._currentAction() == ACTION_DETECT_FILES:
			self.filesList.SetFocus()
		elif self.inputText.IsShown():
			self.inputText.SetFocus()
		else:
			self.actionChoice.SetFocus()

	def _syncActionControls(self):
		action = self._currentAction()
		uses_model = action in (ACTION_DETECT_TEXT, ACTION_BULK)
		uses_text = action in (ACTION_DETECT_TEXT, ACTION_BULK, ACTION_PLAGIARISM)
		uses_files = action in (ACTION_DETECT_FILES, ACTION_BULK)
		uses_dashboard = action in (ACTION_DETECT_TEXT, ACTION_DETECT_FILES)
		if action == ACTION_BULK:
			# Translators: Label of the Pangram text field in bulk mode; items are separated by a line containing only «---».
			self.inputLabel.SetLabel(_("&Texts (separate items with a line containing only ---):"))
			# Translators: Label before the Pangram file list in bulk mode (each text file becomes one item).
			self.filesLabel.SetLabel(_("&Files (each text file is one item):"))
		else:
			# Translators: Label of the Pangram multiline text field.
			self.inputLabel.SetLabel(_("&Text:"))
			# Translators: Label before the Pangram file list in document mode (.docx, .pdf, .rtf).
			self.filesLabel.SetLabel(_("&Files (DOCX, PDF, RTF):"))
		for ctrl in (self.modelLabel, self.modelCombo):
			ctrl.Show(uses_model)
		self.refreshModelsBtn.Show(uses_model or action == ACTION_LIST_MODELS)
		for ctrl in (self.inputLabel, self.inputText, self.wordCountLabel, self.loadTextBtn, self.pasteBtn):
			ctrl.Show(uses_text)
		self.useLastResponseBtn.Show(uses_text and bool(self._parentLastResponse()))
		for ctrl in (self.filesLabel, self.filesList, self.addFilesBtn, self.removeFilesBtn):
			ctrl.Show(uses_files)
		self.dashboardCheck.Show(uses_dashboard)
		self.formPanel.Layout()
		self.Layout()

	def _syncResultButtons(self):
		self.openDashboardBtn.Show(bool(self._dashboardLinks))
		self.copyResultBtn.Enable(bool(self.resultText.GetValue().strip()))
		self.openJsonBtn.Show(bool(self._jsonPath))
		self.formPanel.Layout()
		self.Layout()

	def _updateWordCount(self):
		text = self.inputText.GetValue()
		words = len(text.split())
		# Translators: Status line under the Pangram text field; placeholders are word and character counts.
		self.wordCountLabel.SetLabel(_("{words} words, {chars} characters").format(words=words, chars=len(text)))

	def _setBusy(self, busy: bool):
		for ctrl in (
			self.accountChoice,
			self.actionChoice,
			self.modelCombo,
			self.refreshModelsBtn,
			self.inputText,
			self.loadTextBtn,
			self.pasteBtn,
			self.useLastResponseBtn,
			self.filesList,
			self.addFilesBtn,
			self.removeFilesBtn,
			self.dashboardCheck,
			self.openDashboardBtn,
			self.copyResultBtn,
			self.openJsonBtn,
			self.runBtn,
			self.closeBtn,
		):
			ctrl.Enable(not busy)

	def _setModelChoices(self, models):
		current = self.modelCombo.GetValue().strip()
		choices = list(models) or [pangramapi.DEFAULT_MODEL]
		self.modelCombo.Set(choices)
		self.modelCombo.SetValue(current if current else choices[0])

	# --- event handlers ----------------------------------------------------------

	def onActionChanged(self, evt):
		self._syncActionControls()

	def onInputTextChanged(self, evt):
		self._updateWordCount()
		evt.Skip()

	def onLoadTextFile(self, evt):
		dlg = wx.FileDialog(
			self,
			# Translators: Title of the file picker that loads a text file into the Pangram text field.
			message=_("Select a text file"),
			# Translators: File-type filter in the Pangram «Load text file» picker.
			wildcard=_("Text files (*.txt;*.md)|*.txt;*.md;*.markdown;*.text|All files (*.*)|*.*"),
			style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
		)
		try:
			if dlg.ShowModal() != wx.ID_OK:
				return
			path = dlg.GetPath()
		finally:
			dlg.Destroy()
		try:
			self.inputText.SetValue(_read_text_file(path))
		except Exception as err:
			# Translators: Error when the text file chosen in the Pangram tool cannot be read; placeholder is the OS error.
			wx.MessageBox(_("Unable to read file: %s") % err, "Pangram", wx.OK | wx.ICON_ERROR)
			return
		self.inputText.SetFocus()

	def onPasteClipboard(self, evt):
		try:
			text = api.getClipData()
		except Exception:
			text = ""
		if not text:
			# Translators: Message when the user pastes into the Pangram tool but the clipboard has no text.
			ui.message(_("Clipboard is empty or does not contain text"))
			return
		self.inputText.SetValue(text)
		self.inputText.SetFocus()

	def onUseLastResponse(self, evt):
		text = self._parentLastResponse()
		if not text:
			wx.Bell()
			return
		self.inputText.SetValue(text)
		self.inputText.SetFocus()

	def onAddFiles(self, evt):
		if self._currentAction() == ACTION_BULK:
			# Translators: File-type filter in the Pangram file picker for bulk mode (text files).
			wildcard = _("Text files (*.txt;*.md)|*.txt;*.md;*.markdown;*.text|All files (*.*)|*.*")
		else:
			# Translators: File-type filter in the Pangram file picker for document mode.
			wildcard = _("Documents (*.docx;*.pdf;*.rtf)|*.docx;*.pdf;*.rtf")
		dlg = wx.FileDialog(
			self,
			# Translators: Title of the file picker used to add files to the Pangram file list.
			message=_("Select files"),
			wildcard=wildcard,
			style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST | wx.FD_MULTIPLE,
		)
		try:
			if dlg.ShowModal() != wx.ID_OK:
				return
			paths = dlg.GetPaths()
		finally:
			dlg.Destroy()
		existing = set(self._fileItems())
		for path in paths:
			if path not in existing:
				self.filesList.Append(path)
				existing.add(path)
		self.filesList.SetFocus()

	def onRemoveFiles(self, evt):
		selections = sorted(self.filesList.GetSelections(), reverse=True)
		if not selections:
			wx.Bell()
			return
		for idx in selections:
			self.filesList.Delete(idx)
		if self.filesList.GetCount():
			self.filesList.SetSelection(min(selections[-1], self.filesList.GetCount() - 1))
			self.filesList.SetFocus()

	def onFilesListKey(self, evt):
		if evt.GetKeyCode() == wx.WXK_DELETE:
			self.onRemoveFiles(None)
			return
		evt.Skip()

	def onOpenDashboard(self, evt):
		for link in self._dashboardLinks:
			self.open_local_path(link, err_title="Pangram")

	def onCopyResult(self, evt):
		text = self.resultText.GetValue()
		if not text.strip():
			return
		api.copyToClip(text)
		# Translators: Message after the Pangram report is copied to the clipboard.
		ui.message(_("Result copied to clipboard"))

	def onOpenJson(self, evt):
		self.open_local_path(self._jsonPath, err_title="Pangram")

	def onRefreshModels(self, evt):
		self._startWorker(ACTION_LIST_MODELS, {})

	def onClose(self, evt):
		self._markClosing()
		self._stopEvent.set()
		stop_progress_sound()
		self.end_long_task()
		stop_worker_thread(self._worker)
		self._worker = None
		if isinstance(evt, wx.CloseEvent):
			evt.Skip()
			return
		self.Close()

	# --- run ---------------------------------------------------------------------

	def _buildBulkItems(self) -> list:
		items = []
		raw = self.inputText.GetValue()
		if raw.strip():
			for idx, chunk in enumerate(_BULK_SEPARATOR_RE.split(raw), start=1):
				chunk = chunk.strip()
				if chunk:
					items.append({"id": "text-%d" % idx, "text": chunk})
		used_ids = {item["id"] for item in items}
		for path in self._fileItems():
			if os.path.splitext(path)[1].lower() not in BULK_TEXT_EXTENSIONS:
				# Translators: Error when a non-text file is in the Pangram file list in bulk mode; placeholder is the file path.
				raise ValueError(_("Bulk mode only accepts text files: %s") % path)
			text = _read_text_file(path).strip()
			if not text:
				continue
			item_id = os.path.basename(path)
			suffix = 2
			while item_id in used_ids:
				item_id = "%s (%d)" % (os.path.basename(path), suffix)
				suffix += 1
			used_ids.add(item_id)
			items.append({"id": item_id, "text": text})
		return items

	def onRun(self, evt):
		if self._worker and self._worker.is_alive():
			return
		action = self._currentAction()
		params = {}
		try:
			if action in (ACTION_DETECT_TEXT, ACTION_PLAGIARISM):
				text = self.inputText.GetValue().strip()
				if not text:
					# Translators: Error when running a Pangram text action with an empty text field.
					wx.MessageBox(_("Please enter the text to analyze."), "Pangram", wx.OK | wx.ICON_ERROR)
					self.inputText.SetFocus()
					return
				params["text"] = text
			elif action == ACTION_DETECT_FILES:
				paths = self._fileItems()
				if not paths:
					# Translators: Error when running Pangram document detection without any file in the list.
					wx.MessageBox(_("Please add at least one document."), "Pangram", wx.OK | wx.ICON_ERROR)
					self.addFilesBtn.SetFocus()
					return
				for path in paths:
					if not os.path.isfile(path):
						# Translators: Error when a file in the Pangram list no longer exists; placeholder is the path.
						raise ValueError(_("File not found:\n%s") % path)
					if os.path.splitext(path)[1].lower() not in pangramapi.FILE_UPLOAD_EXTENSIONS:
						# Translators: Error when an unsupported file type is in the Pangram document list; placeholder is the path.
						raise ValueError(_("Unsupported document type (DOCX, PDF or RTF expected): %s") % path)
				params["paths"] = paths
			elif action == ACTION_BULK:
				items = self._buildBulkItems()
				if not items:
					# Translators: Error when running Pangram bulk detection without any text item.
					wx.MessageBox(_("Please enter texts or add text files to analyze."), "Pangram", wx.OK | wx.ICON_ERROR)
					self.inputText.SetFocus()
					return
				params["items"] = items
		except Exception as err:
			wx.MessageBox(str(err), "Pangram", wx.OK | wx.ICON_ERROR)
			return
		params["model"] = self._selectedModel()
		params["public_dashboard_link"] = self.dashboardCheck.IsChecked()
		self._startWorker(action, params)

	def _startWorker(self, action: str, params: dict):
		if self._worker and self._worker.is_alive():
			return
		acc_id = self.require_account(self.accountChoice)
		if not acc_id:
			return
		api_key = self.manager.get_api_key(account_id=acc_id)
		if not api_key:
			# Translators: Error when the selected Pangram account has no API key.
			wx.MessageBox(_("No API key available for the selected Pangram account."), "Pangram", wx.OK | wx.ICON_ERROR)
			return
		if self.conf["chatFeedback"]["sndTaskInProgress"]:
			winsound.PlaySound(SND_PROGRESS, winsound.SND_ASYNC | winsound.SND_LOOP)
		if action == ACTION_LIST_MODELS:
			# Translators: Status line on the progress window while the Pangram model list is being fetched.
			status = _("Fetching Pangram models...")
		elif action == ACTION_PLAGIARISM:
			# Translators: Status line on the progress window while Pangram checks the text for plagiarism.
			status = _("Checking for plagiarism...")
		else:
			# Translators: Status line on the progress window while Pangram analyzes the text or documents.
			status = _("AI detection in progress...")
		self._stopEvent.clear()
		self.begin_long_task(status, self._setBusy)
		self._worker = start_background(self._runThread, api_key, action, params)

	def _runThread(self, api_key, action, params):
		err = None
		payload = None
		should_stop = self._stopEvent.is_set
		try:
			if action == ACTION_LIST_MODELS:
				payload = {"models": pangramapi.list_models(api_key)}
			elif action == ACTION_DETECT_TEXT:
				payload = pangramapi.detect_text(
					api_key,
					params["text"],
					model=params["model"],
					public_dashboard_link=params["public_dashboard_link"],
					should_stop=should_stop,
				)
			elif action == ACTION_DETECT_FILES:
				payload = pangramapi.detect_files(
					api_key,
					params["paths"],
					public_dashboard_link=params["public_dashboard_link"],
				)
			elif action == ACTION_BULK:
				payload = pangramapi.detect_bulk(
					api_key,
					params["items"],
					model=params["model"],
					should_stop=should_stop,
				)
			elif action == ACTION_PLAGIARISM:
				payload = pangramapi.check_plagiarism(api_key, params["text"])
		except pangramapi.PangramCancelled:
			return
		except Exception as e:
			err = e
		call_after_if_alive(self, self._onThreadDone, action, params, payload, err)

	def _formatReport(self, action, params, payload) -> str:
		if action == ACTION_LIST_MODELS:
			models = payload.get("models") or []
			# Translators: Pangram tool report — heading before the list of model selectors available to the API key.
			header = _("Available models:")
			return "\n".join([header] + ["- %s" % m for m in models])
		if action == ACTION_DETECT_TEXT:
			return format_detection_result(payload)
		if action == ACTION_DETECT_FILES:
			return format_files_result(params.get("paths", []), payload)
		if action == ACTION_BULK:
			return format_bulk_result(payload)
		if action == ACTION_PLAGIARISM:
			return format_plagiarism_result(payload)
		return ""

	def _collectDashboardLinks(self, action, payload) -> list:
		results = []
		if action == ACTION_DETECT_TEXT:
			results = [payload]
		elif action == ACTION_DETECT_FILES:
			results = payload or []
		links = []
		for result in results:
			link = pangramapi.dashboard_link(result)
			if link and link not in links:
				links.append(link)
		return links

	def _onThreadDone(self, action, params, payload, err):
		stop_progress_sound()
		if not self._isDialogAlive():
			self._worker = None
			return
		if self.conf["chatFeedback"]["sndResponseReceived"]:
			winsound.PlaySound(SND_CHAT_RESPONSE_RECEIVED, winsound.SND_ASYNC)
		if not self.end_long_task(focus_ctrl=self.resultText):
			self._worker = None
			return
		self._worker = None
		if err is not None:
			log.error(f"Pangram {action} failed: {err}", exc_info=err)
			# Translators: Error body when a Pangram request fails; placeholder is the error returned by the API.
			wx.MessageBox(_("Pangram request failed: %s") % err, "Pangram", wx.OK | wx.ICON_ERROR)
			return
		report = self._formatReport(action, params, payload)
		if action == ACTION_LIST_MODELS:
			self._setModelChoices(payload.get("models") or [])
			self.resultText.SetValue(report)
			self._syncResultButtons()
			self.resultText.SetFocus()
			return
		self._lastAction = action
		self._dashboardLinks = self._collectDashboardLinks(action, payload)
		json_path = ""
		report_path = ""
		try:
			json_path = build_media_path("documents", ".json", prefix="pangram")
			with open(json_path, "w", encoding="utf-8") as f:
				json.dump(payload, f, ensure_ascii=False, indent=2)
			report_path = build_media_path("documents", ".txt", prefix="pangram")
			with open(report_path, "w", encoding="utf-8") as f:
				f.write(report or "")
		except Exception:
			log.error("Unable to save Pangram result files", exc_info=True)
		self._jsonPath = json_path
		self._reportPath = report_path
		self.resultText.SetValue(report)
		self.resultText.SetInsertionPoint(0)
		self._syncResultButtons()
		self.resultText.SetFocus()
		self._saveConversation(action, params, report)

	def _saveConversation(self, action, params, report):
		action_label = dict(self.ACTIONS).get(action, action)
		if action in (ACTION_DETECT_TEXT, ACTION_PLAGIARISM):
			prompt = params.get("text", "")
		elif action == ACTION_DETECT_FILES:
			prompt = "\n".join(params.get("paths", []))
		else:
			prompt = "\n\n---\n\n".join(item.get("text", "") for item in params.get("items", []))
		model = params.get("model", "") if action in (ACTION_DETECT_TEXT, ACTION_BULK) else ""
		self.save_tool_conversation(
			# Translators: Title stored on the «tool output» conversation after a Pangram run; placeholder is the action name.
			title=_("Tool output: Pangram — %s") % action_label,
			conversation_format=ConversationFormat.TOOL_PANGRAM,
			prompt=prompt,
			response_text=(report or "")[:_RESPONSE_PREVIEW_CHARS],
			model=model,
			format_data={
				"action": action,
				"model": model,
				"input_text": self.inputText.GetValue() if action != ACTION_DETECT_FILES else "",
				"files": list(params.get("paths", [])) if action == ACTION_DETECT_FILES else self._fileItems(),
				"public_dashboard_link": bool(params.get("public_dashboard_link")),
				"dashboard_links": list(self._dashboardLinks),
				"report_path": self._reportPath,
				"json_path": self._jsonPath,
				"report_preview": (report or "")[:_RESPONSE_PREVIEW_CHARS],
			},
		)

	def _applyConversationData(self, conversationData):
		if not isinstance(conversationData, dict):
			return
		fd = conversationData.get("formatData", {})
		if not isinstance(fd, dict):
			return
		action = fd.get("action")
		if isinstance(action, str) and action:
			self._selectAction(action)
		model = fd.get("model")
		if isinstance(model, str) and model:
			self.modelCombo.SetValue(model)
		text = fd.get("input_text")
		if isinstance(text, str) and text:
			self.inputText.SetValue(text)
		files = fd.get("files")
		if isinstance(files, list):
			for path in files:
				if isinstance(path, str) and path:
					self.filesList.Append(path)
		self.dashboardCheck.SetValue(bool(fd.get("public_dashboard_link", False)))
		links = fd.get("dashboard_links")
		if isinstance(links, list):
			self._dashboardLinks = [link for link in links if isinstance(link, str) and link]
		json_path = fd.get("json_path", "")
		if isinstance(json_path, str) and json_path and os.path.isfile(json_path):
			self._jsonPath = json_path
		report = ""
		report_path = fd.get("report_path", "")
		if isinstance(report_path, str) and report_path and os.path.isfile(report_path):
			self._reportPath = report_path
			try:
				report = _read_text_file(report_path)
			except Exception:
				report = ""
		if not report:
			preview = fd.get("report_preview", "")
			report = preview if isinstance(preview, str) else ""
		if report:
			self.resultText.SetValue(report)
