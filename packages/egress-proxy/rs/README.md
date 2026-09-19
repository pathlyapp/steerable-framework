Allow-listing CONNECT egress proxy. Empty `--allow` fails at startup.

```sh
cargo test --manifest-path packages/egress-proxy/rs/Cargo.toml
cargo run --manifest-path packages/egress-proxy/rs/Cargo.toml -- --bind 127.0.0.1:8899 --allow api.deepseek.com
```
