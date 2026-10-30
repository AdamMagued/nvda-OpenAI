"""Minimal client for the Pangram Labs REST API (AI detection, bulk, files, plagiarism).

Reference: https://docs.pangram.com/api-reference/introduction

Every call is blocking and meant to run on a worker thread. ``should_stop`` is an
optional callable polled between requests so long-running polls can be abandoned
when the tool dialog closes.
"""

import json
import mimetypes
import os
import time
import urllib.parse

from .apiclient import APIConnectionError, APIStatusError, build_multipart_body, http_request_json
from logHandler import log

TEXT_API_BASE_URL = "https://text.external-api.pangram.com"
FILE_API_BASE_URL = "https://file-external.api.pangram.com"
PLAGIARISM_API_BASE_URL = "https://plagiarism.api.pangram.com"

DEFAULT_MODEL = "default"
# File uploads always use Pangram's default model (the endpoint has no ``model`` field).
FILE_UPLOAD_EXTENSIONS = (".docx", ".pdf", ".rtf")

STAGE_SUCCESS = "STAGE_SUCCESS"
STAGE_FAILED = "STAGE_FAILED"
BULK_TERMINAL_STATUSES = ("succeeded", "failed", "partial")
BULK_PAGE_LIMIT = 1000

DEFAULT_POLL_INTERVAL = 1.0
DEFAULT_TIMEOUT = 600
_REQUEST_TIMEOUT = 180

# Documented status codes -> short explanation appended to the raw server message.
_STATUS_HINTS = {
	400: "Bad request",
	401: "Missing or invalid API key",
	402: "Insufficient credits",
	403: "Model not enabled for this API key, or resource not owned by this key",
	404: "Task or bulk job not found",
	413: "Payload too large",
	415: "Unsupported file type",
	422: "Invalid input text, file, or model selector",
	429: "Rate limit exceeded",
	500: "Pangram server error",
	503: "Selected model temporarily unavailable",
}


class PangramError(Exception):
	"""Raised for HTTP errors, failed tasks, cancellation, and timeouts."""

	def __init__(self, message: str, status_code: int = 0, body: str = ""):
		self.status_code = status_code
		self.body = body
		super().__init__(message)


class PangramCancelled(PangramError):
	pass


def _error_detail(body: str) -> str:
	if not body:
		return ""
	try:
		data = json.loads(body)
	except Exception:
		log.debug("Ignored error", exc_info=True)
		return body.strip()
	if isinstance(data, dict):
		detail = data.get("detail", data.get("error", data.get("message")))
		if isinstance(detail, list):
			parts = []
			for item in detail:
				if isinstance(item, dict):
					loc = ".".join(str(x) for x in item.get("loc", []) if x is not None)
					msg = item.get("msg", "")
					parts.append(f"{loc}: {msg}" if loc else str(msg))
				else:
					parts.append(str(item))
			return "; ".join(p for p in parts if p)
		if isinstance(detail, dict):
			return str(detail.get("message", detail))
		if detail:
			return str(detail)
	return body.strip()


def _request(method, url, api_key, *, payload=None, data=None, content_type=None):
	headers = {"x-api-key": (api_key or "").strip(), "Accept": "application/json"}
	if payload is not None:
		data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
		content_type = "application/json"
	if content_type:
		headers["Content-Type"] = content_type
	try:
		result = http_request_json(url, method=method, headers=headers, data=data, timeout=_REQUEST_TIMEOUT, allow_text=True)
	except APIStatusError as e:
		body = e.response_body or ""
		hint = _STATUS_HINTS.get(e.status_code, "")
		detail = _error_detail(body)
		message = f"HTTP {e.status_code}"
		if hint:
			message += f" ({hint})"
		if detail:
			message += f": {detail}"
		raise PangramError(message, status_code=e.status_code, body=body) from None
	except APIConnectionError as e:
		raise PangramError(str(e)) from None
	if isinstance(result, str):
		raise PangramError(f"Invalid JSON response: {result[:500]}")
	return result


def _check_stop(should_stop):
	if callable(should_stop) and should_stop():
		raise PangramCancelled("Cancelled")


def _sleep(seconds, should_stop):
	end = time.monotonic() + max(0.0, seconds)
	while time.monotonic() < end:
		_check_stop(should_stop)
		time.sleep(min(0.2, max(0.0, end - time.monotonic())))


def list_models(api_key: str) -> list:
	"""``GET /models``: ordered model selectors available to this API key."""
	data = _request("GET", f"{TEXT_API_BASE_URL}/models", api_key)
	models = data.get("models") if isinstance(data, dict) else None
	return [m for m in models if isinstance(m, str) and m] if isinstance(models, list) else []


def create_task(api_key: str, text: str, model: str = DEFAULT_MODEL, public_dashboard_link: bool = False) -> str:
	"""``POST /task``: create an async AI detection task and return its ``task_id``."""
	payload = {
		"text": text,
		"model": model or DEFAULT_MODEL,
		"public_dashboard_link": bool(public_dashboard_link),
	}
	data = _request("POST", f"{TEXT_API_BASE_URL}/task", api_key, payload=payload)
	task_id = data.get("task_id") if isinstance(data, dict) else None
	if not task_id:
		raise PangramError("Pangram did not return a task_id.")
	return task_id


def get_task(api_key: str, task_id: str) -> dict:
	"""``GET /task/{task_id}``: current stage or completed result."""
	url = f"{TEXT_API_BASE_URL}/task/{urllib.parse.quote(task_id, safe='')}"
	data = _request("GET", url, api_key)
	return data if isinstance(data, dict) else {}


def detect_text(
	api_key: str,
	text: str,
	model: str = DEFAULT_MODEL,
	public_dashboard_link: bool = False,
	*,
	poll_interval: float = DEFAULT_POLL_INTERVAL,
	timeout: float = DEFAULT_TIMEOUT,
	should_stop=None,
) -> dict:
	"""Create a task, then poll until ``STAGE_SUCCESS`` / ``STAGE_FAILED``."""
	task_id = create_task(api_key, text, model=model, public_dashboard_link=public_dashboard_link)
	deadline = time.monotonic() + timeout
	while True:
		_check_stop(should_stop)
		result = get_task(api_key, task_id)
		stage = result.get("stage")
		if stage == STAGE_SUCCESS:
			result.setdefault("task_id", task_id)
			return result
		if stage == STAGE_FAILED:
			raise PangramError(result.get("headline") or "AI detection task failed.", body=json.dumps(result))
		if time.monotonic() >= deadline:
			raise PangramError(f"Timed out waiting for task {task_id}.")
		_sleep(poll_interval, should_stop)


def submit_bulk(api_key: str, items: list, model: str = DEFAULT_MODEL) -> dict:
	"""``POST /bulk`` with the ``items`` shape (``{"id"?, "text"}`` objects)."""
	payload = {"items": items, "model": model or DEFAULT_MODEL}
	data = _request("POST", f"{TEXT_API_BASE_URL}/bulk", api_key, payload=payload)
	return data if isinstance(data, dict) else {}


def get_bulk(api_key: str, bulk_id: str) -> dict:
	url = f"{TEXT_API_BASE_URL}/bulk/{urllib.parse.quote(bulk_id, safe='')}"
	data = _request("GET", url, api_key)
	return data if isinstance(data, dict) else {}


def get_bulk_results(api_key: str, bulk_id: str, offset: int = 0, limit: int = BULK_PAGE_LIMIT) -> dict:
	query = urllib.parse.urlencode({"offset": int(offset), "limit": int(limit)})
	url = f"{TEXT_API_BASE_URL}/bulk/{urllib.parse.quote(bulk_id, safe='')}/results?{query}"
	data = _request("GET", url, api_key)
	return data if isinstance(data, dict) else {}


def detect_bulk(
	api_key: str,
	items: list,
	model: str = DEFAULT_MODEL,
	*,
	poll_interval: float = 2.0,
	timeout: float = DEFAULT_TIMEOUT,
	should_stop=None,
) -> dict:
	"""Submit a bulk job, poll until terminal, then collect every results page.

	Returns ``{"submission": ..., "status": ..., "items": [...], "failed_items": [...]}``.
	"""
	submission = submit_bulk(api_key, items, model=model)
	bulk_id = submission.get("bulk_id")
	if not bulk_id:
		raise PangramError("Pangram did not return a bulk_id.", body=json.dumps(submission))
	status = {"bulk_id": bulk_id, "status": submission.get("status")}
	deadline = time.monotonic() + timeout
	while status.get("status") not in BULK_TERMINAL_STATUSES:
		_sleep(poll_interval, should_stop)
		status = get_bulk(api_key, bulk_id)
		if time.monotonic() >= deadline and status.get("status") not in BULK_TERMINAL_STATUSES:
			raise PangramError(f"Timed out waiting for bulk job {bulk_id}.")
	results = []
	failed = []
	offset = 0
	total = None
	while True:
		_check_stop(should_stop)
		page = get_bulk_results(api_key, bulk_id, offset=offset)
		page_items = page.get("items") or []
		page_failed = page.get("failed_items") or []
		results.extend(page_items)
		failed.extend(page_failed)
		total = page.get("total_items", total)
		fetched = len(page_items) + len(page_failed)
		offset += BULK_PAGE_LIMIT
		if fetched == 0 or total is None or offset >= total:
			break
	return {"submission": submission, "status": status, "items": results, "failed_items": failed}


def _build_multipart(file_paths, fields):
	files = []
	for path in file_paths:
		mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
		with open(path, "rb") as f:
			files.append(("files", os.path.basename(path), f.read(), mime))
	return build_multipart_body(fields, files)


def detect_files(api_key: str, file_paths: list, public_dashboard_link: bool = False) -> list:
	"""``POST /`` on the file API: one result object per uploaded file (same order)."""
	body, content_type = _build_multipart(
		file_paths,
		{"public_dashboard_link": "true" if public_dashboard_link else "false"},
	)
	data = _request("POST", FILE_API_BASE_URL, api_key, data=body, content_type=content_type)
	if isinstance(data, dict):
		data = [data]
	return data if isinstance(data, list) else []


def check_plagiarism(api_key: str, text: str) -> dict:
	"""``POST /`` on the plagiarism API."""
	data = _request("POST", PLAGIARISM_API_BASE_URL, api_key, payload={"text": text})
	return data if isinstance(data, dict) else {}


def dashboard_link(result: dict) -> str:
	"""File uploads return ``public_dashboard_link``; text tasks return ``dashboard_link``."""
	if not isinstance(result, dict):
		return ""
	for key in ("dashboard_link", "public_dashboard_link"):
		value = result.get(key)
		if isinstance(value, str) and value.startswith(("http://", "https://")):
			return value
	return ""
