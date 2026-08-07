"""Model-to-backend routing."""

from __future__ import annotations


class ModelRouter:
    def __init__(self, backends: dict[str, dict]):
        self._routes: list[tuple[str, str]] = []
        for backend, config in backends.items():
            for model in config.get("models", []):
                self._routes.append((model, backend))
            for prefix in config.get("prefixes", []):
                self._routes.append((prefix, backend))
        self._routes.sort(key=lambda route: len(route[0]), reverse=True)

    def backend_for(self, model: str) -> str:
        for pattern, backend in self._routes:
            if model == pattern or model.startswith(pattern):
                return backend
        raise ValueError(f"no backend configured for model: {model}")
