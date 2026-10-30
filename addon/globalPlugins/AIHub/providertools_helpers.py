"""Helpers for provider tools dialogs."""

import wx

from .consts import UI_FORM_ROW_BORDER_PX
from logHandler import log


def safe_int(value, default=None):
	try:
		return int(str(value).strip())
	except Exception:
		log.debug("Ignored error", exc_info=True)
		return default


def safe_float(value, default=None):
	try:
		return float(str(value).strip())
	except Exception:
		log.debug("Ignored error", exc_info=True)
		return default


def extract_audio_b64(data):
	"""Best effort extractor for base64 audio payload."""
	if isinstance(data, dict):
		for key in ("audio", "audioBytes", "audio_bytes", "data", "inlineData"):
			value = data.get(key)
			if isinstance(value, str) and len(value) > 128:
				return value
			if isinstance(value, dict):
				found = extract_audio_b64(value)
				if found:
					return found
		for value in data.values():
			found = extract_audio_b64(value)
			if found:
				return found
	elif isinstance(data, list):
		for item in data:
			found = extract_audio_b64(item)
			if found:
				return found
	return None


def extract_ocr_text(payload):
	if not isinstance(payload, dict):
		return ""
	lines = []
	pages = payload.get("pages")
	if isinstance(pages, list):
		for page in pages:
			if not isinstance(page, dict):
				continue
			markdown = page.get("markdown")
			text = page.get("text")
			if isinstance(markdown, str) and markdown.strip():
				lines.append(markdown.strip())
			elif isinstance(text, str) and text.strip():
				lines.append(text.strip())
	if not lines:
		for key in ("markdown", "text", "content"):
			val = payload.get(key)
			if isinstance(val, str) and val.strip():
				lines.append(val.strip())
				break
	return "\n\n".join(lines).strip()


def add_labeled_factory(parent, sizer, label_text, control_factory, border=UI_FORM_ROW_BORDER_PX, expand=True):
	"""
	Create label first, then control, then add to sizer.
	Useful when SR association depends on creation order.
	"""
	label = wx.StaticText(parent, label=label_text)
	sizer.Add(label, 0, wx.LEFT | wx.RIGHT | wx.TOP, border)
	control = control_factory()
	flags = wx.LEFT | wx.RIGHT | wx.BOTTOM
	if expand:
		flags |= wx.EXPAND
	proportion = 1 if expand and isinstance(control, wx.TextCtrl) and control.IsMultiLine() else 0
	sizer.Add(control, proportion, flags, border)
	return control


_MODELFILE_TEXT_FIELDS = {"FROM": "from", "SYSTEM": "system", "TEMPLATE": "template", "LICENSE": "license"}


def _modelfile_number(value: str):
	for cast in (int, float):
		try:
			return cast(value)
		except ValueError:
			pass
	return value


def parse_modelfile(text: str) -> dict:
	"""Convert Modelfile text into the structured ``/api/create`` body fields.

	Ollama's create endpoint takes ``from``, ``system``, ``template``,
	``license``, ``parameters`` and ``messages`` instead of raw Modelfile text:
	https://docs.ollama.com/api/create
	Values may be quoted with triple double quotes to span several lines.
	"""
	fields: dict = {}
	parameters: dict = {}
	messages: list = []
	lines = (text or "").replace("\r\n", "\n").split("\n")
	i = 0
	while i < len(lines):
		line = lines[i].strip()
		i += 1
		if not line or line.startswith("#"):
			continue
		keyword, _, rest = line.partition(" ")
		keyword = keyword.upper()
		rest = rest.strip()
		if rest.startswith('"""'):
			body = rest[3:]
			chunks = []
			while '"""' not in body and i < len(lines):
				chunks.append(body)
				body = lines[i]
				i += 1
			chunks.append(body.split('"""', 1)[0])
			rest = "\n".join(chunks).strip("\n")
		if keyword in _MODELFILE_TEXT_FIELDS:
			fields[_MODELFILE_TEXT_FIELDS[keyword]] = rest
		elif keyword == "PARAMETER":
			name, _, value = rest.partition(" ")
			value = _modelfile_number(value.strip().strip('"'))
			if name in parameters:
				existing = parameters[name]
				parameters[name] = (existing if isinstance(existing, list) else [existing]) + [value]
			elif name == "stop":
				parameters[name] = [value]
			else:
				parameters[name] = value
		elif keyword == "MESSAGE":
			role, _, content = rest.partition(" ")
			messages.append({"role": role.lower(), "content": content.strip()})
	if parameters:
		fields["parameters"] = parameters
	if messages:
		fields["messages"] = messages
	return fields
