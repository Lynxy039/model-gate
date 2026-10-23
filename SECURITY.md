# Security policy

## Intended deployment

model-gate is a single-user macOS tool, not an Internet-facing inference service.
The gate listens only on `127.0.0.1` or `localhost`; schema validation rejects other
addresses. Inference requests have no authentication, so do not publish the API via
a reverse proxy, port forwarding, LAN, or Tailscale. Local processes are trusted.
There is no account/tenant isolation, TLS, or public-service rate limit.

The admin API additionally checks the peer loopback address, local Host and browser
Origin headers. Older unversioned configs with a wildcard gate listener migrate to
loopback in memory and produce a warning; migration does not overwrite the source.
Managed DS4 chooses a free port and binds to loopback. Custom process commands must
also bind their inference listener to loopback.

## Trusted inputs and private data

- Configured backend URLs, load/unload hooks, and process commands are trusted
  operator input and run with the gate user's privileges. Never accept config from
  an inference client.
- Configuration, locks and selected-model state are private and scoped by config
  path under `~/Library/Application Support/model-gate/`; logs are under
  `~/Library/Logs/model-gate/`, and cache under `~/Library/Caches/model-gate/`.
  Do not share these directories or config files casually.
- Prompt bodies and API keys are forwarded to the configured backend. The proxy
  does not follow backend redirects. Do not configure an untrusted backend.
- KV cache files may contain prompt text and model state. Keep the cache directory
  private; stop inference before deleting it. The panel asks gate to perform
  supported cleanup; it does not inspect or remove files itself.
- Logs expose model names, errors, paths and backend stderr. They are not anonymized;
  review them before posting an issue.
- A backend already running outside gate ownership is not adopted or stopped by
  managed lifecycle operations.

## Reporting

Please do not paste credentials, private prompts or cache files into public issues.
Private vulnerability reporting is enabled: use **Report a vulnerability** in the
repository's [Security tab](https://github.com/Lynxy039/model-gate/security).
For other questions, contact [@Lynxy039](https://github.com/Lynxy039).
