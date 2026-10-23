# Security policy

## Intended deployment

model-gate is a single-user local tool, not an Internet-facing inference service.
Keep the default loopback listener. If you bind to a LAN/Tailscale address, restrict
access with a firewall/ACL or use an authenticated reverse proxy. Inference API
requests do not validate API keys. A client with network access can consume GPU
resources, select a real model in its request, and hold request slots.

The admin API checks the peer's loopback address, local Host and browser-origin
headers. Do not publish it through a reverse proxy: the proxy's loopback connection
would otherwise make remote requests appear local. Local processes are trusted.
There is no account/tenant isolation, TLS termination, or public-service rate limit.

## Trusted inputs and private data

- Backend URLs, load/unload hooks and process commands are trusted operator config.
  They execute with the gate user's privileges. Never load config from a client.
- Backend processes started outside the gate are not generally under its control.
  Use direct binary commands (or shell wrappers ending in `exec`).
- Prompt bodies and API keys are forwarded to the configured backend. The proxy
  does not follow backend redirects. Do not configure an untrusted backend.
- KV cache files may contain full prompt text and model state. Keep the cache
  directory private; never commit or share it. Stop inference before deleting it.
- Logs expose model names, errors, paths and backend stderr. Do not assume logs
  are anonymized. Review them before posting an issue.
- `/tmp/model-gate.pid` and `/tmp/model-gate.selected` currently serve one instance
  per machine. Do not use them to manage multiple independent gate instances.

## Reporting

Please do not paste credentials, private prompts or cache files into public issues.
Private vulnerability reporting is enabled: use **Report a vulnerability** in the
repository's [Security tab](https://github.com/Lynxy039/model-gate/security).
For other questions, contact [@Lynxy039](https://github.com/Lynxy039).
