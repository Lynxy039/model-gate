"""Model-to-backend routing."""

from __future__ import annotations


class ModelRouter:
    def __init__(self, backends: dict[str, dict]):
        self._models: dict[str, str] = {}
        self._routes: list[tuple[str, str]] = []
        for backend, config in backends.items():
            for model in set(config.get("models", [])) | set(config.get("model_commands", {})):
                if model in self._models and self._models[model] != backend:
                    raise ValueError(f"model configured in multiple backends: {model}")
                self._models[model] = backend
            for prefix in config.get("prefixes", []):
                self._routes.append((prefix, backend))
        self._routes.sort(key=lambda route: len(route[0]), reverse=True)

    def backend_for(self, model: str) -> str:
        if model in self._models:
            return self._models[model]
        for pattern, backend in self._routes:
            if model.startswith(pattern):
                return backend
        raise ValueError(f"no backend configured for model: {model}")
