"""Server-Sent Events line iteration.

Implements the subset of the SSE spec we need:

* ``data:VALUE`` and ``data: VALUE`` are both accepted (Mistral omits the space).
* ``event:``, ``id:``, ``retry:`` and ``:comment`` lines are skipped.
* ``\n``, ``\r\n`` and bare ``\r`` line endings are tolerated.
* Multiple ``data:`` lines forming a single event are joined with ``\n`` per spec.
* Events are dispatched on a blank line.

This module exposes only one public iterator so every stream parser uses the
exact same SSE handling — no more split implementations to keep in sync.
"""
from __future__ import annotations

import json
from typing import Any, Iterable, Iterator, Optional


# Sentinel returned for ``data: [DONE]`` so callers don't have to string-compare.
DONE = object()


def iter_sse_data_blocks(resp) -> Iterator[bytes]:
	"""Yield the raw payload bytes of each SSE event's ``data:`` field.

	Each yielded value is the *joined* payload (multi-line ``data:`` fields are
	concatenated with ``\n`` per spec). Empty events and non-data fields are
	filtered out so callers only see actual payloads.
	"""
	buf = b""
	current: list[bytes] = []
	for chunk in resp:
		if not chunk:
			continue
		buf += chunk
		while True:
			# Find the next end-of-line: prefer "\r\n", then "\n", then bare "\r".
			nl = _find_line_end(buf)
			if nl is None:
				break
			line, buf = _split_line(buf, nl)
			if not line:
				if current:
					yield b"\n".join(current)
					current = []
				continue
			# Skip comment lines (start with ":").
			if line[:1] == b":":
				continue
			field, _, value = line.partition(b":")
			if field != b"data":
				# Ignore event:/id:/retry: and any other field types.
				continue
			# Spec: a single optional space after the colon is stripped.
			if value.startswith(b" "):
				value = value[1:]
			current.append(value)
	# A last line without a line terminator is still part of the final event.
	if buf:
		field, _, value = buf.partition(b":")
		if field == b"data":
			current.append(value[1:] if value.startswith(b" ") else value)
	# Yield any final event that wasn't terminated by a blank line.
	if current:
		yield b"\n".join(current)


def _find_line_end(buf: bytes) -> Optional[tuple[int, int]]:
	"""Return ``(end_offset, after_offset)`` for the next line terminator, or None."""
	# Look at \r and \n separately so we can collapse a stray \r\n into one break.
	cr = buf.find(b"\r")
	nl = buf.find(b"\n")
	if cr < 0 and nl < 0:
		return None
	if cr < 0:
		return (nl, nl + 1)
	if nl < 0:
		return (cr, cr + 1)
	if cr < nl:
		# Possible \r\n pair: if next byte is \n, consume both.
		if cr + 1 == nl:
			return (cr, nl + 1)
		return (cr, cr + 1)
	return (nl, nl + 1)


def _split_line(buf: bytes, ends: tuple[int, int]) -> tuple[bytes, bytes]:
	end_offset, after_offset = ends
	return buf[:end_offset], buf[after_offset:]


def decode_sse_payload(payload: bytes) -> Any:
	"""Decode one SSE data payload to a JSON value, ``DONE``, or ``None``.

	* ``b"[DONE]"`` returns the ``DONE`` sentinel.
	* Empty payloads return ``None``.
	* Anything else is parsed as JSON; ``None`` is returned on parse errors so
	  callers can simply skip malformed/heartbeat-style chunks.
	"""
	if not payload:
		return None
	stripped = payload.strip()
	if not stripped:
		return None
	if stripped == b"[DONE]":
		return DONE
	try:
		return json.loads(stripped.decode("utf-8"))
	except (UnicodeDecodeError, json.JSONDecodeError):
		return None


def iter_sse_events(resp) -> Iterable[Any]:
	"""High-level helper: yield decoded JSON objects (dict/list) and the DONE sentinel.

	Malformed/empty chunks are silently dropped — they're typically heartbeats.
	"""
	for payload in iter_sse_data_blocks(resp):
		value = decode_sse_payload(payload)
		if value is None:
			continue
		yield value
