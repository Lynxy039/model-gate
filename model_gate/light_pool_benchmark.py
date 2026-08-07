"""Measure concurrent OMLX prefill for the Coder Next + ThinkingCap pool.

Dry-run is the default.  --run sends large prompts to the OMLX server.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import time
from typing import Any
from urllib.request import Request, urlopen


DEFAULT_MODELS = (
    "mlx-community--Qwen3-Coder-Next-nvfp4",
    "ThinkingCap-Qwen3.6-27B-oQ6e-mtp",
)


def parse_contexts(value: str) -> list[int]:
    contexts = [int(item) for item in value.split(",")]
    if not contexts or any(item <= 0 for item in contexts):
        raise ValueError("contexts must be comma-separated positive integers")
    return contexts


def build_prompt(seed: str, units: int) -> str:
    """Make a deterministic, unique prompt so prefix cache cannot hide prefill."""
    return "Benchmark input; answer exactly OK.\n" + " ".join(
        f"{seed}-{index:08x}" for index in range(units)
    )


def request_json(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    request = Request(
        url,
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def status(base_url: str, timeout: float) -> dict[str, Any]:
    with urlopen(f"{base_url}/v1/models/status", timeout=timeout) as response:
        return json.loads(response.read())


def prompt_for_tokens(
    base_url: str,
    model: str,
    target: int,
    seed: str,
    timeout: float,
) -> tuple[str, int]:
    """Size a unique prompt using OMLX's tokenizer, within roughly 1%."""
    # OMLX tokenizes the unique identifiers much more densely than prose;
    # begin conservatively, then converge in both directions.
    units = max(16, target // 32)
    lower, upper = target * 0.99, target * 1.01
    for _ in range(12):
        prompt = build_prompt(seed, units)
        result = request_json(
            f"{base_url}/v1/messages/count_tokens",
            {"model": model, "messages": [{"role": "user", "content": prompt}]},
            timeout,
        )
        count = int(result["input_tokens"])
        if lower <= count <= upper:
            return prompt, count
        adjusted = round(units * target / max(count, 1))
        units = max(units + 1, adjusted) if count < lower else max(1, min(units - 1, adjusted))
    raise RuntimeError(f"could not size {target} tokens for {model}")


def completion(base_url: str, model: str, prompt: str, timeout: float) -> dict[str, Any]:
    started = time.monotonic()
    result = request_json(
        f"{base_url}/v1/chat/completions",
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 1,
            "stream": False,
            "temperature": 0,
        },
        timeout,
    )
    return {"seconds": round(time.monotonic() - started, 3), "response": result}


def parse_vm_stat(output: str) -> dict[str, int]:
    page_size = int(re.search(r"page size of (\d+)", output).group(1))
    pages = {
        match.group(1).lower().replace(" ", "_"): int(match.group(2))
        for match in re.finditer(r"Pages ([A-Za-z -]+):\s+(\d+)", output)
    }
    return {
        "page_size": page_size,
        "free_bytes": pages.get("free", 0) * page_size,
        "active_bytes": pages.get("active", 0) * page_size,
        "wired_bytes": pages.get("wired_down", 0) * page_size,
        "compressed_bytes": pages.get("occupied_by_compressor", 0) * page_size,
    }


def host_memory_summary() -> dict[str, Any]:
    """Best-effort macOS unified-memory sample; unsupported hosts return nulls."""
    result: dict[str, Any] = {"free_percent": None, "vm_stat": None, "omlx_rss_bytes": None}
    try:
        output = subprocess.run(
            ["memory_pressure", "-Q"], capture_output=True, text=True, check=True
        ).stdout
        match = re.search(r"free percentage:\s+(\d+)%", output)
        result["free_percent"] = int(match.group(1)) if match else None
    except (OSError, subprocess.CalledProcessError):
        pass
    try:
        output = subprocess.run(["vm_stat"], capture_output=True, text=True, check=True).stdout
        result["vm_stat"] = parse_vm_stat(output)
    except (AttributeError, OSError, subprocess.CalledProcessError):
        pass
    try:
        output = subprocess.run(
            ["ps", "-axo", "rss=,command="], capture_output=True, text=True, check=True
        ).stdout
        result["omlx_rss_bytes"] = sum(
            int(line.split(maxsplit=1)[0]) * 1024
            for line in output.splitlines()
            if "omlx-server" in line
        )
    except (OSError, subprocess.CalledProcessError):
        pass
    return result


def memory_summary(server_status: dict[str, Any], server_stats: dict[str, Any] | None = None) -> dict[str, Any]:
    pressure = (server_stats or {}).get("active_models", {}).get("memory_pressure", {})
    return {
        "loaded_count": server_status.get("loaded_count"),
        "current_model_memory": server_status.get("current_model_memory"),
        "pressure": pressure.get("pressure_level"),
        "current_bytes": pressure.get("current_bytes"),
        "soft_bytes": pressure.get("soft_bytes"),
        "hard_bytes": pressure.get("hard_bytes"),
        "host": host_memory_summary(),
    }


def capture_memory(base_url: str, timeout: float) -> dict[str, Any]:
    try:
        with urlopen(f"{base_url}/admin/api/stats", timeout=timeout) as response:
            stats = json.loads(response.read())
    except OSError:
        stats = None
    return memory_summary(status(base_url, timeout), stats)


def run_phase(
    base_url: str,
    models: tuple[str, str],
    context: int,
    overhead: int,
    timeout: float,
    poll_seconds: float,
) -> dict[str, Any]:
    target = context - overhead
    if target <= 0:
        raise ValueError("context must exceed overhead_tokens")
    prompts = {}
    counts = {}
    for model in models:
        prompts[model], counts[model] = prompt_for_tokens(
            base_url, model, target, f"{model}-{context}", timeout
        )

    samples = [capture_memory(base_url, timeout)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {
            model: pool.submit(completion, base_url, model, prompts[model], timeout)
            for model in models
        }
        while not all(future.done() for future in futures.values()):
            time.sleep(poll_seconds)
            samples.append(capture_memory(base_url, timeout))
        results = {model: future.result() for model, future in futures.items()}
    samples.append(capture_memory(base_url, timeout))
    return {
        "context_limit": context,
        "requested_user_tokens": target,
        "token_counts": counts,
        "results": results,
        "memory_samples": samples,
    }


def save_report(path: Path, report: dict[str, Any]) -> None:
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark concurrent Coder Next + ThinkingCap prefill")
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--contexts", default="25000,50000,100000,150000,200000")
    parser.add_argument("--overhead-tokens", type=int, default=20_000)
    parser.add_argument("--timeout", type=float, default=1_200)
    parser.add_argument("--poll-seconds", type=float, default=1)
    parser.add_argument("--report-dir", default="reports")
    parser.add_argument("--run", action="store_true", help="send the large prefill requests")
    args = parser.parse_args()

    contexts = parse_contexts(args.contexts)
    initial = status(args.base_url, min(args.timeout, 10))
    models = tuple(DEFAULT_MODELS)
    available = {model["id"]: model for model in initial["models"]}
    missing = [model for model in models if model not in available]
    if missing:
        raise SystemExit(f"models unavailable: {', '.join(missing)}")

    weights = {
        model: available[model].get("estimated_size")
        for model in models
    }
    plan = {
        "models": list(models),
        "contexts": contexts,
        "overhead_tokens": args.overhead_tokens,
        "estimated_weights_bytes": weights,
        "initial_memory": capture_memory(args.base_url, min(args.timeout, 10)),
    }
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    if not args.run:
        print("Dry run: add --run only when you are ready to prefill both models.")
        return

    report_dir = Path(args.report_dir)
    report_dir.mkdir(exist_ok=True)
    path = report_dir / f"light-pool-{datetime.now():%Y%m%d-%H%M%S}.json"
    report = {**plan, "started_at": datetime.now(timezone.utc).isoformat(), "phases": []}
    for context in contexts:
        print(f"Running {context:,} context tokens per model...", flush=True)
        try:
            phase = run_phase(
                args.base_url, models, context, args.overhead_tokens,
                args.timeout, args.poll_seconds,
            )
        except Exception as error:
            report["phases"].append({"context_limit": context, "error": str(error)})
            save_report(path, report)
            raise SystemExit(f"Benchmark stopped; partial report: {path}") from error
        report["phases"].append(phase)
        save_report(path, report)
    print(f"Report: {path}")


if __name__ == "__main__":
    main()
