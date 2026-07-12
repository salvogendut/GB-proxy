"""Thread-safe short-token registry for proxy-local resources."""

import hashlib
import threading
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ProxyResource:
	target: str
	content: Optional[bytes] = None


_KINDS = ("image", "url")
_resources = {kind: {} for kind in _KINDS}
_lock = threading.Lock()


def register_resource(kind, target, content=None):
	"""Register a target and return a compact, collision-safe token."""
	if kind not in _resources:
		raise ValueError(f"Unsupported resource kind: {kind}")

	resource = ProxyResource(target=target, content=content)
	digest = hashlib.sha256()
	digest.update(kind.encode("ascii"))
	digest.update(b"\0")
	digest.update(target.encode("utf-8"))
	if content is not None:
		digest.update(b"\0")
		digest.update(content)
	hexdigest = digest.hexdigest()

	with _lock:
		registry = _resources[kind]
		for length in range(10, len(hexdigest) + 1, 2):
			token = hexdigest[:length]
			existing = registry.get(token)
			if existing is None:
				registry[token] = resource
				return token
			if existing == resource:
				return token

	raise RuntimeError("Could not allocate a unique resource token")


def resolve_resource(kind, token):
	if kind not in _resources:
		return None
	with _lock:
		return _resources[kind].get(token)


def clear_resources():
	with _lock:
		for registry in _resources.values():
			registry.clear()
