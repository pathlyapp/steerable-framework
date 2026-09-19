use std::sync::Arc;
use std::time::Duration;

use tokio::sync::Mutex;

use steerable_egress_proxy::{parse_bind, AllowList, EgressProxyServer, InjectRule, ProxyConfig};

struct Cli {
    bind: String,
    allow: Vec<String>,
    connect_timeout: f64,
    inject: Option<InjectRule>,
    record_requests: Option<String>,
    control_token: Option<String>,
    control_port: u16,
}

fn parse_args(argv: &[String]) -> Result<Cli, i32> {
    let mut bind = "127.0.0.1:8899".to_string();
    let mut allow = Vec::new();
    let mut connect_timeout = 10.0;
    let mut inject_host = None;
    let mut inject_secret_env = None;
    let mut inject_header = "Authorization".to_string();
    let mut inject_scheme = "https".to_string();
    let mut inject_port = None;
    let mut record_requests = None;
    let mut control_token_env = None;
    let mut control_port = 0u16;
    let mut i = 0;
    while i < argv.len() {
        match argv[i].as_str() {
            "--bind" => {
                i += 1;
                bind = argv.get(i).cloned().ok_or(2)?;
            }
            "--allow" => {
                i += 1;
                allow.push(argv.get(i).cloned().ok_or(2)?);
            }
            "--connect-timeout" => {
                i += 1;
                connect_timeout = argv.get(i).ok_or(2)?.parse().map_err(|_| 2)?;
            }
            "--inject-host" => {
                i += 1;
                inject_host = Some(argv.get(i).cloned().ok_or(2)?);
            }
            "--inject-secret-env" => {
                i += 1;
                inject_secret_env = Some(argv.get(i).cloned().ok_or(2)?);
            }
            "--inject-header" => {
                i += 1;
                inject_header = argv.get(i).cloned().ok_or(2)?;
            }
            "--inject-scheme" => {
                i += 1;
                inject_scheme = argv.get(i).cloned().ok_or(2)?;
            }
            "--inject-port" => {
                i += 1;
                inject_port = Some(argv.get(i).ok_or(2)?.parse().map_err(|_| 2)?);
            }
            "--record-requests" => {
                i += 1;
                record_requests = Some(argv.get(i).cloned().ok_or(2)?);
            }
            "--control-token-env" => {
                i += 1;
                control_token_env = Some(argv.get(i).cloned().ok_or(2)?);
            }
            "--control-port" => {
                i += 1;
                control_port = argv.get(i).ok_or(2)?.parse().map_err(|_| 2)?;
            }
            other => {
                eprintln!("error: unknown argument {other}");
                return Err(2);
            }
        }
        i += 1;
    }
    let inject = match (inject_host, inject_secret_env) {
        (None, None) => None,
        (Some(_), None) | (None, Some(_)) => {
            eprintln!("error: --inject-host and --inject-secret-env must come together");
            return Err(2);
        }
        (Some(host), Some(var)) => {
            let secret = std::env::var(&var).unwrap_or_default();
            if secret.is_empty() {
                eprintln!("error: inject secret env var {var:?} is empty or unset");
                return Err(2);
            }
            Some(
                InjectRule::new(host, secret, inject_header, inject_scheme, inject_port).map_err(
                    |err| {
                        eprintln!("error: {err}");
                        2
                    },
                )?,
            )
        }
    };
    if record_requests.is_some() && inject.is_none() {
        eprintln!("error: --record-requests requires --inject-host");
        return Err(2);
    }
    let control_token = match control_token_env {
        Some(var) => {
            let token = std::env::var(&var).unwrap_or_default();
            if token.is_empty() {
                eprintln!("error: control token env var {var:?} is empty or unset");
                return Err(2);
            }
            Some(token)
        }
        None => None,
    };
    Ok(Cli {
        bind,
        allow,
        connect_timeout,
        inject,
        record_requests,
        control_token,
        control_port,
    })
}

#[tokio::main(flavor = "current_thread")]
async fn main() {
    let argv: Vec<String> = std::env::args().skip(1).collect();
    let cli = match parse_args(&argv) {
        Ok(v) => v,
        Err(code) => std::process::exit(code),
    };
    let (bind_host, bind_port) = match parse_bind(&cli.bind) {
        Ok(v) => v,
        Err(err) => {
            eprintln!("error: {err}");
            std::process::exit(2);
        }
    };
    let allow = match AllowList::new(&cli.allow) {
        Ok(v) => v,
        Err(err) => {
            eprintln!("error: {err}");
            std::process::exit(2);
        }
    };
    let mut server = EgressProxyServer::new(ProxyConfig {
        allow: Arc::new(Mutex::new(allow)),
        bind_host,
        bind_port,
        connect_timeout: Duration::from_secs_f64(cli.connect_timeout),
        inject: cli.inject,
        record_requests: cli.record_requests,
        control_token: cli.control_token,
        control_port: cli.control_port,
    });
    if let Err(err) = server.bind().await {
        eprintln!("error: {err}");
        std::process::exit(1);
    }
    eprintln!(
        "egress proxy on {}:{} (rust)",
        cli.bind,
        server.bound_port()
    );
    if let Err(err) = server.serve().await {
        eprintln!("error: {err}");
        std::process::exit(1);
    }
}
