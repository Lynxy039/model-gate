from __future__ import annotations

import argparse

from .proxy import serve


def main() -> None:
    parser = argparse.ArgumentParser(description="Безопасный proxy для DS4 и OMLX")
    parser.add_argument("--config", default="model-gate.json", help="путь к JSON-конфигурации")
    args = parser.parse_args()
    serve(args.config)


if __name__ == "__main__":
    main()
