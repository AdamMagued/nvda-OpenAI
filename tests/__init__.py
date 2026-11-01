"""Tests package for AIHub addon."""

import enum

if not hasattr(enum, "StrEnum"):
	class StrEnum(str, enum.Enum):
		def _generate_next_value_(name, start, count, last_values):
			return name.lower()

		def __str__(self):
			return str(self.value)

	enum.StrEnum = StrEnum

from . import conftest  # noqa: F401
