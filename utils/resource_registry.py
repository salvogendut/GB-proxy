"""Thread-safe short-token registry for proxy-local resources."""

import hashlib
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ProxyResource:
	target: str
	content: Optional[bytes] = None


@dataclass
class _RegistryEntry:
	resource: ProxyResource
	last_accessed: float


_KINDS = ("image", "url")
_resources = {kind: OrderedDict() for kind in _KINDS}
_lock = threading.Lock()
_max_entries = 4096
_ttl_seconds = 3600
_max_content_bytes = 2 * 1024 * 1024


def _purge_expired(registry, now):
	while registry:
		token, entry = next(iter(registry.items()))
		if now - entry.last_accessed <= _ttl_seconds:
			break
		del registry[token]


def _trim_registry(registry):
	while len(registry) > _max_entries:
		registry.popitem(last=False)


def configure_resources(max_entries=4096, ttl_seconds=3600, max_content_bytes=2 * 1024 * 1024):
	"""Configure per-kind LRU capacity and idle expiry for short resources."""
	if not isinstance(max_entries, int) or max_entries < 1:
		raise ValueError("max_entries must be a positive integer")
	if not isinstance(ttl_seconds, (int, float)) or ttl_seconds <= 0:
		raise ValueError("ttl_seconds must be positive")
	if not isinstance(max_content_bytes, int) or max_content_bytes < 0:
		raise ValueError("max_content_bytes must be a non-negative integer")

	global _max_entries, _ttl_seconds, _max_content_bytes
	with _lock:
		_max_entries = max_entries
		_ttl_seconds = ttl_seconds
		_max_content_bytes = max_content_bytes
		now = time.monotonic()
		for registry in _resources.values():
			_purge_expired(registry, now)
			_trim_registry(registry)


def register_resource(kind, target, content=None):
	"""Register a target and return a compact, collision-safe token."""
	if kind not in _resources:
		raise ValueError(f"Unsupported resource kind: {kind}")
	if content is not None and len(content) > _max_content_bytes:
		raise ValueError(
			f"Inline resource exceeds the {_max_content_bytes}-byte registry limit"
		)

	resource = ProxyResource(target=target, content=content)
	digest = hashlib.sha256()
	digest.update(kind.encode("ascii"))
	digest.update(b"\0")
	digest.update(target.encode("utf-8"))
	if content is not None:
		digest.update(b"\0")
		digest.update(content)
	hexdigest = digest.hexdigest()

	now = time.monotonic()
	with _lock:
		registry = _resources[kind]
		_purge_expired(registry, now)
		for length in range(10, len(hexdigest) + 1, 2):
			token = hexdigest[:length]
			existing = registry.get(token)
			if existing is None:
				while len(registry) >= _max_entries:
					registry.popitem(last=False)
				registry[token] = _RegistryEntry(resource, now)
				return token
			if existing.resource == resource:
				existing.last_accessed = now
				registry.move_to_end(token)
				return token

	raise RuntimeError("Could not allocate a unique resource token")


def resolve_resource(kind, token):
	if kind not in _resources:
		return None
	now = time.monotonic()
	with _lock:
		registry = _resources[kind]
		_purge_expired(registry, now)
		entry = registry.get(token)
		if entry is None:
			return None
		entry.last_accessed = now
		registry.move_to_end(token)
		return entry.resource


def clear_resources():
	with _lock:
		for registry in _resources.values():
			registry.clear()
