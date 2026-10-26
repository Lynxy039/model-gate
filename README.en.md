# model-gate

A local OpenAI-compatible proxy and model lifecycle controller for **macOS**, focused on DS4 and oMLX. One alias (`local-auto`) routes requests through a FIFO admission gate so only one model is active at a time. The terminal panel is an API client; lifecycle, cancellation, telemetry, and KV cleanup stay on the gate side.

- Python 3.11+, runtime standard library only.
- 128 GiB unified memory is the reference profile, not a hardware requirement or a promise that every model/context fits.
- No inference API authentication: keep the listener on loopback. Tailnet access is supported only through Tailscale Serve (never Funnel), with tailnet ACLs.
- DS4 binds to a free loopback port by default; fixed port override is available in JSON.
- Config is versioned JSON and is the single source of truth. `setup` is the first-run wizard; edit JSON directly for custom backends.

## Install and start

```bash
brew install pipx && pipx ensurepath
pipx install .
model-gate setup
model-gate doctor
mgrun --no-gpu
```

`model-gate setup --config PATH` creates an independent configuration. The schema is `model-gate.schema.json`; `model-gate.example.json` demonstrates DS4 + oMLX. `model-gate doctor` checks the schema, gate port, DS4 executable/model files, backend reachability, and the advisory memory profile.

## Backend contract

The built-in types are deliberately small:

- **`ds4`** — gate-managed DS4 process, GGUF/model options, automatic loopback port or a fixed port.
- **`omlx`** — resident oMLX server with load/unload hooks and discovery.
- **`process`** — a custom managed command; use `{port}` in the command for automatic port assignment.
- **`http`** — a resident OpenAI-compatible server.

Use `process` or `http` for custom backends. Managed commands must bind their API to loopback; a server already running outside gate ownership is never adopted or stopped. The gate does not follow backend redirects. See [SECURITY.md](SECURITY.md).

## Client

```bash
curl http://127.0.0.1:9000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"local-auto","messages":[{"role":"user","content":"Hello"}],"max_tokens":64}'
```

Use `base_url=http://127.0.0.1:9000/v1` and `model=local-auto`. Select a target in `mgpan` (`m`) or via `POST /admin/select`. The panel only calls the gate API; it does not inspect PIDs or access backend files.

## Gate API

- `GET /healthz`, `/status`, `/admin/status`, `/admin/stats`, `/admin/telemetry`
- `POST /admin/select` — `{"model":"..."}`
- `POST /admin/cancel` — `{"id":123}`; cancels queued or active requests
- `POST /admin/stop` / `/admin/clear-kv` — backend lifecycle operations
- `POST /admin/halt` / `/admin/resume` — close/reopen admission; halt cancels queued work and stops/unloads backends
- `POST /admin/shutdown` — graceful stop; `{"restart":true}` reloads config and execs the gate

Mutating admin methods are loopback-only. Read-only admin GETs can be allowed for named Tailscale users using Serve identity headers. Halt is reversible with resume; a client disconnect cancels its request without turning the backend failure count into an error.

## Tailscale access

Keep the gate bound to `127.0.0.1` and proxy it with **Tailscale Serve** (tailnet-only HTTPS); do not use Funnel. Verify with `tailscale serve status`, or configure with `tailscale serve --bg 9000`. Add an explicit read-only user allowlist to gate config:

```json
"tailscale_readonly_users": ["you@example.com"]
```

That user can read `/admin/stats`, `/admin/status`, and `/admin/telemetry`; remote POST admin actions remain blocked. Inference requests are available only to tailnet peers allowed by Tailscale ACLs.

On any tailnet device:

```bash
export MODEL_GATE_URL="https://<gate-device>.<tailnet-domain>.ts.net"
mgpan --url "$MODEL_GATE_URL" --read-only
```

Replace the placeholder in the example with the HTTPS MagicDNS URL shown by `tailscale serve status`. For Pi, configure an `openai-completions` provider at `https://<gate-device>.<tailnet-domain>.ts.net/v1`, with `apiKey: "none"` and model ID `local-auto`. Remote `mgpan` reads system telemetry from the gate host, not the client device; remote URLs automatically use read-only mode.

## State and tests

Config-scoped locks and selected model are in `~/Library/Application Support/model-gate/`; logs and caches are in `~/Library/Logs/model-gate/` and `~/Library/Caches/model-gate/`. State is separated by config path. Legacy unversioned JSON is migrated in memory, never overwritten.

```bash
python3.11 -m unittest discover -v
```

Tests use fake backends and loopback sockets; they need no model weights or network access. CI runs on macOS and Linux as a Python test matrix; macOS is the supported product platform.

MIT License — see [LICENSE](LICENSE).
