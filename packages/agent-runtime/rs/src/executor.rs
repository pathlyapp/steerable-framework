use std::collections::HashMap;
use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;

use async_trait::async_trait;
use serde_json::Value;

use crate::types::{LoopContext, ToolCall, ToolResult};

type Handler = Arc<
    dyn Fn(Value) -> Pin<Box<dyn Future<Output = Result<ToolResult, String>> + Send>> + Send + Sync,
>;

#[derive(Clone, Default)]
pub struct ToolRouter {
    handlers: HashMap<String, Handler>,
}

impl ToolRouter {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn register<F, Fut>(&mut self, name: impl Into<String>, handler: F)
    where
        F: Fn(Value) -> Fut + Send + Sync + 'static,
        Fut: Future<Output = Result<ToolResult, String>> + Send + 'static,
    {
        let handler = Arc::new(handler);
        self.handlers.insert(
            name.into(),
            Arc::new(move |args| {
                let handler = Arc::clone(&handler);
                Box::pin(async move { handler(args).await })
            }),
        );
    }

    pub async fn dispatch(&self, call: &ToolCall) -> ToolResult {
        let Some(handler) = self.handlers.get(&call.name) else {
            return ToolResult::fail(format!("unknown tool: {}", call.name));
        };
        match handler(call.arguments.clone()).await {
            Ok(result) => result,
            Err(error) => ToolResult::fail(error),
        }
    }
}

#[async_trait]
pub trait ToolExecutor: Send + Sync {
    /// `Err` becomes a `tool_error` event. Router handlers that fail should
    /// return `Ok(ToolResult { success: false, .. })` instead, matching
    /// Python `ToolRouter.dispatch`.
    async fn execute(&self, call: &ToolCall, ctx: &LoopContext) -> Result<ToolResult, String>;

    fn concurrency_safe(&self, _call: &ToolCall) -> bool {
        false
    }

    fn dedup_exempt(&self, _call: &ToolCall) -> bool {
        false
    }
}

pub struct RouterToolExecutor {
    router: ToolRouter,
}

impl RouterToolExecutor {
    pub fn new(router: ToolRouter) -> Self {
        Self { router }
    }
}

#[async_trait]
impl ToolExecutor for RouterToolExecutor {
    async fn execute(&self, call: &ToolCall, _ctx: &LoopContext) -> Result<ToolResult, String> {
        Ok(self.router.dispatch(call).await)
    }
}
