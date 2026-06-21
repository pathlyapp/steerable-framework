# Contributing to Steerable

Thank you for your interest in contributing to Steerable! We welcome contributions of all kinds, from bug reports and documentation improvements to new LLM providers, tool routers, or UI components.

As an open-source project, we hold our codebase to high standards of quality, security, and architectural purity. Please read this guide before submitting a pull request.

---

## Code of Conduct

By participating in this project, you agree to abide by our Code of Conduct. We expect all contributors to maintain a respectful, welcoming, and collaborative environment.

---

## Architectural Purity: The "Zero Business Code" Rule (ADR-002)

Steerable is designed as a **100% business-neutral, generic agent product framework**. 

To maintain this boundary:
- **No Branded Content**: Framework packages must never contain references to specific brands, product names (like "DeepPath"), marketing copy, or proprietary domains.
- **No Paid/Billing Logic**: Features like subscription tiers, payment gateways (WeChat Pay, Alipay, Stripe), or billing routes must live in the host application, not the framework.
- **No Vertical Domain Logic**: Industry-specific tools or expert agent definitions (such as specialized geology or logging tools) must be injected via the SPI (Service Provider Interface) rather than hardcoded in the framework.

### Automated Boundary Checks
We enforce this boundary in CI using a scanning script. Before committing, run:
```bash
python3 scripts/check_framework_boundary.py
```
If your changes accidentally introduce forbidden keywords or imports, this script will fail.

---

## Development Setup

Steerable is structured as a monorepo containing both TypeScript (Frontend/UI) and Python (Harness/Runtime/Sidecar) packages.

### Prerequisites
- **Node.js** (>= 22)
- **pnpm** (>= 10)
- **Python** (>= 3.10)
- **uv** (latest) — Fast Python dependency manager

### Installation
1. Clone the repository:
   ```bash
   git clone https://github.com/pathlyapp/steerable-framework.git
   cd steerable-framework
   ```
2. Install TypeScript dependencies:
   ```bash
   pnpm install
   ```
3. Install Python dependencies and set up the virtual environment:
   ```bash
   uv sync --all-packages
   ```

---

## Running Tests

We require all pull requests to pass 100% of our test suites.

### TypeScript Tests (vitest)
Run all frontend and conformance tests:
```bash
pnpm test
```

### Python Tests (pytest)
Run all backend, harness, and runtime tests:
```bash
uv run pytest
```

---

## Code Style & Guidelines

### TypeScript / React
- Use **TypeScript** for all frontend code with strict type safety.
- Follow functional component and React Hooks programming patterns.
- Use **Tailwind CSS** for styling, utilizing the semantic tokens defined in our theme preset.
- Do not hardcode colors; always use semantic theme variables (e.g., `bg-agent-canvas`, `text-agent-foreground`) to ensure dark-mode compatibility.

### Python
- Follow **PEP 8** guidelines.
- Use **Type Hints** for all function signatures and variable definitions.
- Write clear docstrings explaining the non-obvious intent or constraints of functions and classes.
- Use `async`/`await` for all I/O-bound operations.

---

## Protocol Codegen

The single source of truth for our cross-language contracts is the JSON Schemas located in `spec/`. 

If you modify any schema in `spec/`:
1. Regenerate the TypeScript types:
   ```bash
   pnpm gen
   ```
2. Regenerate the Python Pydantic models:
   ```bash
   uv run python scripts/generate_py.py
   ```
3. Verify that the codegen drift check passes:
   ```bash
   pnpm check:drift
   ```

---

## Pull Request Process

1. **Create a Branch**: Create a descriptive branch name (e.g., `feat/anthropic-adapter` or `fix/sse-parser-leak`).
2. **Write Tests**: Include unit tests for any new features or bug fixes.
3. **Run Checks**: Ensure both `pnpm test`, `uv run pytest`, and `python3 scripts/check_framework_boundary.py` pass locally.
4. **Submit PR**: Open a pull request against the `main` branch.
5. **DCO Sign-off**: All commits must be Developer Certificate of Origin (DCO) signed. Use `git commit -s` when committing.
