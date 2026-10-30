"""Estimate request cost from SigmaNight / OpenRouter catalog ``pricing``.

Rates are USD per token, except ``request`` (per request) and ``web_search``
(per search). ``overrides`` holds long-context tiers that replace the base
rates once the prompt reaches ``min_prompt_tokens``.
"""

from __future__ import annotations

from typing import Any

from .apiclient._usage import uncached_input_tokens


def _to_float(value: Any) -> float:
	try:
		return float(value or 0.0)
	except (TypeError, ValueError):
		return 0.0


def _to_int(value: Any) -> int:
	try:
		return int(value or 0)
	except (TypeError, ValueError):
		return 0


def effective_pricing(pricing: dict, prompt_tokens: int) -> dict:
	"""Return ``pricing`` with the matching long-context tier applied."""
	rates = {k: v for k, v in pricing.items() if k != "overrides"}
	tiers = pricing.get("overrides")
	if not isinstance(tiers, list):
		return rates
	best = None
	for tier in tiers:
		if not isinstance(tier, dict):
			continue
		threshold = _to_int(tier.get("min_prompt_tokens"))
		if threshold <= 0 or prompt_tokens < threshold:
			continue
		if best is None or threshold > _to_int(best.get("min_prompt_tokens")):
			best = tier
	if best:
		rates.update({k: v for k, v in best.items() if k != "min_prompt_tokens"})
	return rates


def estimate_usage_cost(usage: dict, pricing: dict) -> float:
	"""Estimated USD cost of one call from normalized ``usage`` and catalog ``pricing``."""
	if not isinstance(usage, dict) or not isinstance(pricing, dict) or not pricing:
		return 0.0
	input_tokens = _to_int(usage.get("input_tokens")) or _to_int(usage.get("prompt_tokens"))
	output_tokens = _to_int(usage.get("output_tokens")) or _to_int(usage.get("completion_tokens"))
	cached_read = _to_int(usage.get("cached_input_tokens"))
	cache_write = _to_int(usage.get("cache_creation_input_tokens"))
	cache_write_1h = min(cache_write, _to_int(usage.get("cache_creation_1h_input_tokens")))
	rates = effective_pricing(pricing, input_tokens)

	prompt_rate = _to_float(rates.get("prompt"))
	completion_rate = _to_float(rates.get("completion"))
	# Catalog entries often omit cache rates. Bill those tokens at the prompt
	# rate rather than $0 after subtracting them from regular input.
	cache_read_rate = _to_float(rates.get("input_cache_read")) or prompt_rate
	cache_write_rate = _to_float(rates.get("input_cache_write")) or prompt_rate
	cache_write_1h_rate = _to_float(rates.get("input_cache_write_1h")) or cache_write_rate

	cost = (
		uncached_input_tokens(input_tokens, cached_read, cache_write) * prompt_rate
		+ output_tokens * completion_rate
		+ cached_read * cache_read_rate
		+ (cache_write - cache_write_1h) * cache_write_rate
		+ cache_write_1h * cache_write_1h_rate
	)
	# Modality and reasoning tokens are already inside the input/output totals:
	# only the difference to the text rate is added when the catalog prices them.
	audio_rate = _to_float(rates.get("audio"))
	if audio_rate:
		cost += _to_int(usage.get("input_audio_tokens")) * (audio_rate - prompt_rate)
	audio_output_rate = _to_float(rates.get("audio_output"))
	if audio_output_rate:
		cost += _to_int(usage.get("output_audio_tokens")) * (audio_output_rate - completion_rate)
	image_output_rate = _to_float(rates.get("image_output"))
	if image_output_rate:
		cost += _to_int(usage.get("output_image_tokens")) * (image_output_rate - completion_rate)
	reasoning_rate = _to_float(rates.get("internal_reasoning"))
	if reasoning_rate:
		cost += _to_int(usage.get("reasoning_tokens")) * (reasoning_rate - completion_rate)
	cost += _to_int(usage.get("web_search_requests")) * _to_float(rates.get("web_search"))
	cost += _to_float(rates.get("request"))
	return max(0.0, cost)
