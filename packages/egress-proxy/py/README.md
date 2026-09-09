# steerable-egress-proxy

Optional Steerable component: a local, allow-listing `CONNECT` egress proxy.

## Why it exists

The sidecar's OS sandboxes cannot do per-host egress on their own:
macOS Seatbelt degrades hostnames to ports (`*:443`), and Linux bwrap can
only drop the whole network namespace. The remedy on both is the same —
confine the sidecar to `localhost:<proxy port>` and let this proxy own the
host list. See `docs/spec/safety.md` ("Egress allow-list") for the full
threat model.

## Usage

```sh
steerable-egress-proxy --bind 127.0.0.1:8899 \
    --allow api.deepseek.com \
    --allow localhost:11434
```

- Bare `host` entries allow ports 443 and 80, mirroring the Seatbelt
  profile semantics so the two layers agree.
- Fail-closed by construction: an empty allow-list is a startup error,
  not "open"; targets off the list get `403`; non-CONNECT gets `405`
  (unless credential-broker mode is on, below).
- Request heads are capped at 16 KiB; upstream dials time out after 10s.

Wire-up with the sandbox: set the sidecar's egress allow-list to
`localhost:8899` only, and point the sidecar's HTTP stack at the proxy
(`HTTPS_PROXY=http://127.0.0.1:8899` — httpx honors it). The sandbox then
pins the process to the proxy and the proxy enforces the host list.

## Credential broker mode (W2.2.2)

```sh
STEERABLE_EGRESS_SECRET='Bearer sk-...' \
steerable-egress-proxy --bind 127.0.0.1:8899 \
    --allow api.deepseek.com \
    --inject-host api.deepseek.com \
    --inject-secret-env STEERABLE_EGRESS_SECRET
```

With an inject rule, plain-HTTP requests naming that host in the absolute
URI are forwarded to the host over TLS with the credential header
injected. The sandboxed sidecar then uses `http://api.deepseek.com` as its
provider `baseUrl` (note: *http*) with the proxy as `HTTP_PROXY`, and
never holds the real token — the secret exists only in the proxy process
(env var, never argv).

Rules: off-host plain-HTTP → `403`; no rule → non-CONNECT stays `405`;
client-supplied credential headers are stripped, never forwarded; chunked
request bodies → `501`. One inject rule per proxy. `--inject-header`
switches the header (e.g. `x-api-key`); `--inject-scheme http` exists for
loopback test upstreams only. TLS is never intercepted — the CONNECT path
stays opaque byte plumbing.

## Control endpoint (session-scoped widening)

```sh
STEERABLE_EGRESS_CONTROL_TOKEN='random-token' \
steerable-egress-proxy --bind 127.0.0.1:8899 \
    --allow api.deepseek.com \
    --control-port 0 \
    --control-token-env STEERABLE_EGRESS_CONTROL_TOKEN
```

With a control token configured, the proxy also serves one loopback-only
endpoint, `POST /allow {"host": "example.com[:port]"}` with
`Authorization: Bearer <token>`, which adds a **session-scoped** entry to
the allow-list (it dies with the process; the baseline list stays
immutable). The ephemeral port is reported on stdout as
`EGRESS_CONTROL_PORT=<port>`.

This exists for the ask-the-user flow: a denied `CONNECT` gets a `403`
whose reason phrase names the target (`... egress denied for host:port` —
the only metadata channel a CONNECT client can see), the sidecar's web
tools parse that and ask the host UI for a widening decision, and an allow
is relayed here before the fetch retries once.

The bearer token is the whole authorization story. Pass it by env, never
argv (visible in `ps`); the sidecar's sandboxed children run with a
scrubbed environment that excludes it, so a confined process cannot widen
its own egress. Without `--control-token-env` there is no control plane
at all.
