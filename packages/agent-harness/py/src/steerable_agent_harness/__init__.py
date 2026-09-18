from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version

try:
    __version__ = _dist_version("steerable-agent-harness")
except PackageNotFoundError:  # source tree without installed metadata
    __version__ = "0.0.0"

from .budget import (
    DEFAULT_CACHED_TOKEN_WEIGHT,
    BudgetLimit,
    BudgetState,
    consume_budget,
)
from .completion import is_terminal_result
from .policy import PolicyDecision, ToolMode, decide_tool_mode
from .retry import RetryPolicy, next_retry_delay_ms
from .safety import (
    BUILTIN_PATTERNS,
    SAFETY_CATEGORIES,
    CommandSafetyConfig,
    SafetyPatternDef,
    ShellCommandClassification,
    classify_shell_command,
    get_patterns_by_category,
)
from .tracing import TraceSpan

__all__ = [
    "BUILTIN_PATTERNS",
    "DEFAULT_CACHED_TOKEN_WEIGHT",
    "SAFETY_CATEGORIES",
    "BudgetLimit",
    "BudgetState",
    "CommandSafetyConfig",
    "PolicyDecision",
    "RetryPolicy",
    "SafetyPatternDef",
    "ShellCommandClassification",
    "ToolMode",
    "TraceSpan",
    "__version__",
    "classify_shell_command",
    "consume_budget",
    "decide_tool_mode",
    "get_patterns_by_category",
    "is_terminal_result",
    "next_retry_delay_ms",
]
