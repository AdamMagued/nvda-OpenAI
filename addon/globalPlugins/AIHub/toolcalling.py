"""Local tools the model may call, and the request loop that runs them.

Tools are declared in the Chat Completions ``tools`` shape; the API client
converts them (and the resulting tool turns) for the Responses API, Anthropic
and Gemini. Tool rounds are non-streaming: the model asks for calls, the
add-on runs them locally and sends the results back, until the model answers
or ``MAX_TOOL_ROUNDS`` is reached.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from typing import Any, Callable

import addonHandler
from logHandler import log

addonHandler.initTranslation()

MAX_TOOL_ROUNDS = 5
_MAX_RESULT_CHARS = 20000


def _current_datetime(_args: dict) -> str:
	now = datetime.now().astimezone()
	return json.dumps({
		"iso": now.isoformat(timespec="seconds"),
		"weekday": now.strftime("%A"),
		"timezone": now.tzname() or "",
	})


def _run_on_main_thread(fn: Callable[[], Any], timeout: float = 5.0) -> Any:
	"""Run ``fn`` on the wx main thread (NVDA's) and wait for its result."""
	import wx

	if wx.IsMainThread():
		return fn()
	done = threading.Event()
	box: dict = {}

	def _call():
		try:
			box["value"] = fn()
		except Exception as err:
			box["error"] = err
		finally:
			done.set()

	wx.CallAfter(_call)
	if not done.wait(timeout):
		raise TimeoutError("The NVDA main thread did not answer in time.")
	if "error" in box:
		raise box["error"]
	return box.get("value")


def _clipboard_text(_args: dict) -> str:
	import api

	text = _run_on_main_thread(api.getClipData) or ""
	return text[:_MAX_RESULT_CHARS]


# name -> (description, JSON schema of the arguments, handler)
LOCAL_TOOLS: dict[str, tuple[str, dict, Callable[[dict], str]]] = {
	"get_current_datetime": (
		"Get the user's current local date, time, weekday and time zone.",
		{"type": "object", "properties": {}},
		_current_datetime,
	),
	"get_clipboard_text": (
		"Read the text currently on the user's Windows clipboard.",
		{"type": "object", "properties": {}},
		_clipboard_text,
	),
}


def tool_definitions() -> list[dict]:
	"""Local tools in the Chat Completions ``tools`` format."""
	return [
		{"type": "function", "function": {"name": name, "description": desc, "parameters": schema}}
		for name, (desc, schema, _handler) in LOCAL_TOOLS.items()
	]


def run_tool(name: str, arguments: str) -> str:
	"""Run one local tool; errors are returned to the model as text."""
	entry = LOCAL_TOOLS.get(name)
	if entry is None:
		return f"Error: unknown tool {name!r}."
	try:
		args = json.loads(arguments or "{}")
	except ValueError:
		args = {}
	try:
		return str(entry[2](args if isinstance(args, dict) else {}))
	except Exception as err:
		log.debug("Local tool %s failed", name, exc_info=True)
		return f"Error: {err}"


def _merge_usage(total: dict, usage: dict) -> dict:
	for key, value in (usage or {}).items():
		if isinstance(value, (int, float)) and not isinstance(value, bool):
			total[key] = total.get(key, 0) + value
	return total


def run_tool_loop(create: Callable[[dict], Any], params: dict, provider) -> Any:
	"""Call ``create(params)`` until the model stops requesting tools.

	Each assistant tool turn keeps its provider-native form (``_raw_turn``) so
	thinking blocks, thought signatures and reasoning items round-trip. Usage
	is summed over every round on the returned response.
	"""
	messages = list(params.get("messages") or [])
	usage: dict = {}
	response = None
	for _round in range(MAX_TOOL_ROUNDS):
		response = create(dict(params, messages=messages, stream=False))
		_merge_usage(usage, getattr(response, "usage", None) or {})
		choices = getattr(response, "choices", None) or []
		message = choices[0].message if choices else None
		calls = list(getattr(message, "tool_calls", None) or [])
		if not calls:
			break
		messages.append({
			"role": "assistant",
			"content": message.content or "",
			"tool_calls": [
				{"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"]}}
				for c in calls
			],
			"_raw_turn": message.raw_turn,
			"_raw_provider": provider,
		})
		for call in calls:
			log.debug("Running local tool %s", call["name"])
			messages.append({
				"role": "tool",
				"tool_call_id": call["id"],
				"name": call["name"],
				"content": run_tool(call["name"], call["arguments"]),
			})
	else:
		log.warning("Stopped after %d tool rounds without a final answer", MAX_TOOL_ROUNDS)
	if response is not None and usage:
		response.usage = usage
	return response
