//! Fail-closed CONNECT allow-list (Python `steerable_egress_proxy.proxy`).

use std::collections::HashSet;

const BARE_HOST_PORTS: [u16; 2] = [443, 80];

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct AllowEntry {
    pub host: String,
    pub ports: Vec<u16>,
}

impl AllowEntry {
    pub fn allows(&self, host: &str, port: u16) -> bool {
        self.host == host.to_ascii_lowercase() && self.ports.contains(&port)
    }
}

pub fn parse_allow_entry(raw: &str) -> Result<AllowEntry, String> {
    let text = raw.trim();
    if text.is_empty() {
        return Err(format!(
            "invalid allow entry {raw:?}: expected host or host:port"
        ));
    }
    let (host_raw, port) = match text.rsplit_once(':') {
        Some((host, port_s))
            if !host.starts_with('[')
                && !port_s.is_empty()
                && port_s.chars().all(|c| c.is_ascii_digit()) =>
        {
            let port: u16 = port_s
                .parse()
                .map_err(|_| format!("invalid allow entry {raw:?}: port out of range"))?;
            if port == 0 {
                return Err(format!("invalid allow entry {raw:?}: port out of range"));
            }
            (host, Some(port))
        }
        Some(_) => {
            return Err(format!(
                "invalid allow entry {raw:?}: expected host or host:port"
            ));
        }
        None => (text, None),
    };
    let mut host = host_raw.to_ascii_lowercase();
    if host.starts_with('[') && host.ends_with(']') {
        host = host[1..host.len() - 1].to_string();
    }
    if !host
        .chars()
        .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-' | ':'))
        || host.is_empty()
    {
        return Err(format!(
            "invalid allow entry {raw:?}: expected host or host:port"
        ));
    }
    let ports = match port {
        Some(port) => vec![port],
        None => BARE_HOST_PORTS.to_vec(),
    };
    Ok(AllowEntry { host, ports })
}

#[derive(Clone, Debug)]
pub struct AllowList {
    entries: Vec<AllowEntry>,
    session: HashSet<AllowEntry>,
}

impl AllowList {
    pub fn new(entries: &[String]) -> Result<Self, String> {
        if entries.is_empty() {
            return Err(
                "allow-list is empty: an egress proxy with no entries is either a mistake or should not be run at all"
                    .into(),
            );
        }
        let parsed = entries
            .iter()
            .map(|e| parse_allow_entry(e))
            .collect::<Result<Vec<_>, _>>()?;
        Ok(Self {
            entries: parsed,
            session: HashSet::new(),
        })
    }

    pub fn entries(&self) -> &[AllowEntry] {
        &self.entries
    }

    pub fn add(&mut self, raw: &str) -> Result<AllowEntry, String> {
        let entry = parse_allow_entry(raw)?;
        self.session.insert(entry.clone());
        Ok(entry)
    }

    pub fn allows(&self, host: &str, port: u16) -> bool {
        let host = host.to_ascii_lowercase();
        self.entries.iter().any(|e| e.allows(&host, port))
            || self.session.iter().any(|e| e.allows(&host, port))
    }
}
