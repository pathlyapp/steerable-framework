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

- Only `CONNECT host:port` is served (HTTPS tunneling). Plain-HTTP
  forwarding and TLS interception are deliberately out of v1 scope.
- Bare `host` entries allow ports 443 and 80, mirroring the Seatbelt
  profile semantics so the two layers agree.
- Fail-closed by construction: an empty allow-list is a startup error,
  not "open"; targets off the list get `403`; non-CONNECT gets `405`.
- Request heads are capped at 16 KiB; upstream dials time out after 10s.

Wire-up with the sandbox: set the sidecar's egress allow-list to
`localhost:8899` only, and point the sidecar's HTTP stack at the proxy
(`HTTPS_PROXY=http://127.0.0.1:8899` — httpx honors it). The sandbox then
pins the process to the proxy and the proxy enforces the host list.
