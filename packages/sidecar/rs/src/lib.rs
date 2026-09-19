//! Rust sidecar library: JSON-RPC methods, sandbox profiles, stdio framing.

pub mod file_edit;
pub mod landlock;
pub mod methods;
mod ptc_js_child;
pub mod rpc;
mod run_code_child;
pub mod sandbox;

#[cfg(test)]
mod catalog_tests {
    #[test]
    fn methods_match_catalog() {
        let catalog: serde_json::Value = serde_json::from_str(include_str!(
            "../../../../docs/spec/coreloop-rust-test-catalog.json"
        ))
        .unwrap();
        let expected: Vec<String> = catalog["sidecarRpcMethods"]
            .as_array()
            .unwrap()
            .iter()
            .filter_map(|v| v.as_str().map(str::to_string))
            .collect();
        let got: Vec<String> = crate::methods::METHODS
            .iter()
            .map(|s| (*s).to_string())
            .collect();
        assert_eq!(got, expected);
    }
}
