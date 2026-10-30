"""Persistent media helpers for OpenAI add-on."""

import datetime
import hashlib
import json
import os
import shutil
import uuid

from .consts import DATA_DIR, ensure_dir_exists
from logHandler import log

MEDIA_DIR = os.path.join(DATA_DIR, "media")
MEDIA_INDEX_PATH = os.path.join(MEDIA_DIR, "index.json")
MEDIA_INDEX_VERSION = 1


def ensure_media_dir():
	ensure_dir_exists(DATA_DIR)
	ensure_dir_exists(MEDIA_DIR)


def _atomic_write_json(path: str, payload):
	tmp = f"{path}.tmp"
	with open(tmp, "w", encoding="utf-8") as f:
		json.dump(payload, f, indent=2, ensure_ascii=False)
	os.replace(tmp, path)


def _load_media_index() -> dict:
	if not os.path.isfile(MEDIA_INDEX_PATH):
		return {"version": MEDIA_INDEX_VERSION, "entries": {}}
	try:
		with open(MEDIA_INDEX_PATH, "r", encoding="utf-8") as f:
			data = json.load(f)
		entries = data.get("entries", {})
		if not isinstance(entries, dict):
			entries = {}
		return {"version": MEDIA_INDEX_VERSION, "entries": entries}
	except Exception:
		log.debug("Ignored error", exc_info=True)
		return {"version": MEDIA_INDEX_VERSION, "entries": {}}


def _save_media_index(index_data: dict):
	_atomic_write_json(MEDIA_INDEX_PATH, index_data)


def _hash_file(path: str) -> str:
	digest = hashlib.sha256()
	with open(path, "rb") as f:
		for chunk in iter(lambda: f.read(1024 * 1024), b""):
			digest.update(chunk)
	return digest.hexdigest()


def _safe_ext(path, fallback=".bin"):
	ext = os.path.splitext(path or "")[1].lower()
	if ext:
		return ext
	return fallback


def build_media_path(category: str, ext: str, prefix: str = "asset") -> str:
	"""Return unique path in DATA_DIR/media/<category>."""
	ensure_media_dir()
	category_dir = os.path.join(MEDIA_DIR, category)
	ensure_dir_exists(category_dir)
	if not ext.startswith("."):
		ext = "." + ext
	now = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
	return os.path.join(category_dir, f"{prefix}_{now}_{uuid.uuid4().hex[:8]}{ext}")


def persist_local_file(path: str, category: str, prefix: str = "asset", fallback_ext=".bin") -> str:
	"""
	Copy local file into DATA_DIR/media/<category>.
	Returns original path for non-local URLs or already-persistent files.
	"""
	if not path:
		return path
	if path.startswith("http://") or path.startswith("https://"):
		return path
	if not os.path.isfile(path):
		return path
	abs_path = os.path.abspath(path)
	abs_media = os.path.abspath(MEDIA_DIR)
	try:
		if os.path.commonpath([abs_path, abs_media]) == abs_media:
			return path
	except ValueError:
		return path
	# Keep files already inside addon data storage as-is.
	abs_data = os.path.abspath(DATA_DIR)
	try:
		if os.path.commonpath([abs_path, abs_data]) == abs_data:
			return path
	except ValueError:
		return path
	ensure_media_dir()
	ext = _safe_ext(path, fallback_ext)
	file_size = os.path.getsize(path)
	file_hash = _hash_file(path)
	cache_key = f"{file_hash}:{file_size}:{ext}"
	index_data = _load_media_index()
	entries = index_data.get("entries", {})
	cached_path = entries.get(cache_key, "")
	if isinstance(cached_path, str) and cached_path and os.path.isfile(cached_path):
		return cached_path
	dst = build_media_path(category, ext, prefix=prefix)
	shutil.copy2(path, dst)
	entries[cache_key] = dst
	index_data["entries"] = entries
	_save_media_index(index_data)
	return dst
