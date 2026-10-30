"""Request-parameter helpers for chat completions: provider tools, stop sequences,
xAI Responses options and user-facing notices."""

import addonHandler
from .consts import Provider

addonHandler.initTranslation()


# Finish reasons meaning the provider stopped before or instead of answering:
# Anthropic stop_reason, OpenAI finish_reason / incomplete_details.reason,
# Gemini finishReason / promptFeedback.blockReason (lower-cased).
_LENGTH_FINISH_REASONS = frozenset({"length", "max_tokens", "max_output_tokens"})


_BLOCKED_FINISH_REASONS = frozenset({
	"refusal",
	"content_filter",
	"safety",
	"recitation",
	"blocklist",
	"prohibited_content",
	"spii",
	"image_safety",
	"image_prohibited_content",
	"image_other",
	"image_recitation",
	"no_image",
	"language",
	"other",
})


def _empty_response_notice(finish_reason) -> str:
	"""User-facing explanation when a response ends without text, or ``""``."""
	reason = str(finish_reason or "").lower()
	if reason in _LENGTH_FINISH_REASONS:
		# Translators: Shown as the reply when the model used all output tokens (often on reasoning) before writing any answer.
		return _("No answer: the output token limit was reached before any text was written. Increase the max tokens or lower the reasoning effort.")
	if reason in _BLOCKED_FINISH_REASONS:
		# Translators: Shown as the reply when the provider declined or blocked the response; {reason} is the provider's stop reason (e.g. refusal, safety).
		return _("No answer: the provider stopped the response ({reason}).").format(reason=reason)
	return ""


# Maximum stop sequences per provider (OpenAI chat-completions: 4; Gemini
# GenerationConfig.stopSequences: 5); others accept at least 16.
_STOP_SEQUENCE_CAPS = {Provider.OpenAI: 4, Provider.CustomOpenAI: 4, Provider.Google: 5}


# OpenRouter server tool: https://openrouter.ai/docs/guides/features/server-tools/web-search
_OPENROUTER_WEB_SEARCH_TOOL = {"type": "openrouter:web_search"}


# OpenAI Responses hosted web search: https://developers.openai.com/api/docs/guides/tools-web-search
_OPENAI_WEB_SEARCH_TOOL = {"type": "web_search"}


# xAI built-in tools (Responses API): https://docs.x.ai/developers/tools/overview
_XAI_CODE_INTERPRETER_TOOL = {"type": "code_interpreter"}


def _set_builtin_tool(params: dict, tool: dict) -> None:
	"""Replace any existing tool with the same ``type`` and append ``tool``."""
	ttype = tool.get("type")
	tools = [
		t for t in (params.get("tools") or [])
		if not (isinstance(t, dict) and t.get("type") == ttype)
	]
	tools.append(dict(tool))
	params["tools"] = tools


def _append_tool(params: dict, tool: dict) -> None:
	tools = list(params.get("tools") or [])
	ttype = tool.get("type")
	if ttype and any(isinstance(t, dict) and t.get("type") == ttype for t in tools):
		return
	tools.append(dict(tool))
	params["tools"] = tools


def _is_openai_chat_search_model(model_id: str) -> bool:
	"""True for Chat Completions search models (``web_search_options``, not Responses)."""
	mid = (model_id or "").lower()
	return "search-api" in mid or "search-preview" in mid


def _apply_web_search_settings(params: dict, model, wnd, provider: str) -> None:
	"""Apply provider-native and/or OpenRouter universal web search to the request."""
	native_on = (
		model.supports_web_search
		and hasattr(wnd, "webSearchCheckBox")
		and wnd.webSearchCheckBox.IsChecked()
	)
	or_cb = getattr(wnd, "openRouterWebSearchCheckBox", None)
	or_on = (
		provider == Provider.OpenRouter
		and getattr(model, "supports_openrouter_web_search", False)
		and or_cb is not None
		and or_cb.IsChecked()
	)
	if native_on:
		if provider == Provider.Anthropic:
			params["web_search_options"] = {}
		elif provider == Provider.Google:
			# Google Search grounding uses the native generateContent API; see
			# apiclient._google and https://ai.google.dev/gemini-api/docs/google-search
			params["web_search_options"] = {}
		elif provider == Provider.OpenAI:
			if _is_openai_chat_search_model(model.id):
				params["web_search_options"] = {}
			else:
				_append_tool(params, _OPENAI_WEB_SEARCH_TOOL)
		elif provider == Provider.OpenRouter:
			if getattr(model, "supports_openrouter_web_search", False):
				_append_tool(params, _OPENROUTER_WEB_SEARCH_TOOL)
			else:
				params["web_search_options"] = {}
		elif provider == Provider.xAI:
			from .xaitools import build_web_search_tool_from_wnd

			_set_builtin_tool(params, build_web_search_tool_from_wnd(wnd))
	if or_on:
		_append_tool(params, _OPENROUTER_WEB_SEARCH_TOOL)


def _apply_x_search_settings(params: dict, model, wnd, provider: str) -> None:
	"""Attach xAI ``x_search`` (X/Twitter) when the user enables it."""
	if provider != Provider.xAI:
		return
	if not getattr(model, "supports_x_search", False):
		return
	cb = getattr(wnd, "xSearchCheckBox", None)
	if cb is None or not cb.IsChecked():
		return
	from .xaitools import build_x_search_tool_from_wnd

	_set_builtin_tool(params, build_x_search_tool_from_wnd(wnd))


def _apply_code_interpreter_settings(params: dict, model, wnd, provider: str) -> None:
	"""Attach xAI ``code_interpreter`` when enabled."""
	if provider != Provider.xAI:
		return
	if not getattr(model, "supports_code_interpreter", False):
		return
	cb = getattr(wnd, "codeInterpreterCheckBox", None)
	if cb is None or not cb.IsChecked():
		return
	_set_builtin_tool(params, _XAI_CODE_INTERPRETER_TOOL)


def _apply_collections_search_settings(params: dict, model, wnd, provider: str) -> None:
	"""Attach xAI collections search (Responses ``file_search``) when enabled and ids are set."""
	if provider != Provider.xAI:
		return
	if not getattr(model, "supports_collections_search", False):
		return
	cb = getattr(wnd, "collectionsSearchCheckBox", None)
	if cb is None or not cb.IsChecked():
		return
	from .xaitools import build_collections_search_tool_from_wnd

	tool = build_collections_search_tool_from_wnd(wnd)
	if tool is not None:
		_set_builtin_tool(params, tool)


def _apply_xai_include_settings(
	params: dict,
	model,
	wnd,
	provider: str,
	reasoning_enabled: bool,
) -> None:
	"""Set xAI Responses ``include`` only for encrypted reasoning.

	Inline citations are returned by default on the Responses API; requesting
	``inline_citations`` via ``include`` is rejected by xAI (``Argument not
	supported: include``).
	"""
	if provider != Provider.xAI:
		return
	from .xaitools import xai_encrypted_reasoning_requested

	if xai_encrypted_reasoning_requested(wnd, reasoning_enabled):
		params["include"] = ["reasoning.encrypted_content"]
	else:
		params.pop("include", None)


def _messages_for_xai_responses(wnd, messages: list, is_regenerate: bool) -> list:
	"""Use ``previous_response_id`` state: send only the new user turn when possible."""
	if is_regenerate:
		return messages
	anchor = getattr(wnd, "lastBlock", None)
	prev_id = getattr(anchor, "xaiResponseId", None) if anchor is not None else None
	if not isinstance(prev_id, str) or not prev_id.strip():
		return messages
	system_msgs = [
		m for m in messages
		if isinstance(m, dict) and str(m.get("role", "")).lower() == "system"
	]
	user_msgs = [
		m for m in messages
		if isinstance(m, dict) and str(m.get("role", "")).lower() == "user"
	]
	if not user_msgs:
		return messages
	return system_msgs + [user_msgs[-1]]


def _apply_xai_previous_response_id(params: dict, wnd, is_regenerate: bool) -> None:
	if is_regenerate:
		return
	anchor = getattr(wnd, "lastBlock", None)
	prev_id = getattr(anchor, "xaiResponseId", None) if anchor is not None else None
	if isinstance(prev_id, str) and prev_id.strip():
		params["previous_response_id"] = prev_id.strip()


def _format_xai_citations_footer(citations: list, response_text: str) -> str:
	"""Append a sources list when the API returned citations not already inline."""
	if not citations:
		return ""
	text = response_text or ""
	unique = []
	seen: set[str] = set()
	for url in citations:
		if not isinstance(url, str) or not url.strip():
			continue
		u = url.strip()
		if u in seen:
			continue
		seen.add(u)
		if u in text:
			continue
		unique.append(u)
	if not unique:
		return ""
	lines = ["", "---", _("Sources:")]
	for idx, url in enumerate(unique, start=1):
		lines.append(f"{idx}. {url}")
	return "\n".join(lines)


def _apply_xai_response_metadata(block, response, response_text: str = "") -> str:
	"""Store xAI Responses metadata on ``block``; return optional footer text."""
	rid = getattr(response, "response_id", None)
	if isinstance(rid, str) and rid.strip():
		block.xaiResponseId = rid.strip()
	encrypted = getattr(response, "encrypted_reasoning", None)
	if isinstance(encrypted, list) and encrypted:
		block.xaiEncryptedReasoning = list(encrypted)
	citations = getattr(response, "citations", None) or []
	if citations:
		block.citations = list(citations)
		return _format_xai_citations_footer(citations, response_text)
	return ""


def _parse_stop_sequences(text: str, *, provider) -> list[str]:
	"""Non-empty lines from multiline stop UI, capped to the provider's limit."""
	seq = []
	for line in (text or "").replace("\r\n", "\n").split("\n"):
		s = line.strip()
		if s:
			seq.append(s)
	cap = _STOP_SEQUENCE_CAPS.get(provider, 16)
	return seq[:cap]


def _params_for_error_log(params):
	"""Subset of chat completion params suitable for NVDA logs (no prompt / message bodies)."""
	if not isinstance(params, dict):
		return params
	out = {k: v for k, v in params.items() if k != "messages"}
	msgs = params.get("messages")
	if isinstance(msgs, list):
		out["messages"] = "<%d message(s) omitted>" % len(msgs)
	else:
		out["messages"] = "<omitted>"
	return out
