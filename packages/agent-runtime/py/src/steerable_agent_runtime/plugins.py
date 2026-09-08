"""Plugin runtime: discovery sources, lifecycle management, hot reload.

A plugin is a module — installed package or local ``.py`` file — that
exposes a register callable taking the host's ``ToolRouter``::

    # pyproject.toml of an installed extension package
    [project.entry-points."steerable.tools"]
    my_tools = "my_package.tools:register"

    # my_package/tools.py (or any local .py file in a plugin directory)
    def register(router):
        @tool(router=router, description="Greet by name")
        async def greet(name: str) -> str:
            return f"hello {name}"

`PluginRegistry` loads plugins from one or more `PluginSource`
implementations (`EntryPointSource` for installed packages,
`DirectorySource` for local development) and tracks which tool names each
plugin registered, so plugins can be disabled, re-enabled, unloaded, and
reloaded individually after boot.

Hot reload re-executes the plugin's module in place and re-runs its
register callable against the fresh module. The usual module-reload
boundaries apply: references other modules already imported *from* the
plugin keep the old objects, and module-level state outside the plugin
module is untouched. Reload resolves the fresh register
callable before touching the router, so a re-import failure leaves the
plugin's current tools in place; a failure during re-registration leaves
the plugin disabled (its old tools already removed) and raises.

Misconfiguration fails loud: a source path that does not exist, a plugin
that does not import, a register target that is missing or not callable,
and a tool-name conflict all raise ``PluginLoadError`` naming the offender
rather than being silently skipped — a tool the user installed and the
host dropped is a silent capability loss. Lifecycle operations on unknown
or wrong-state plugins raise ``PluginStateError``.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .errors import ToolDispatchError
from .tools import RegisteredTool, ToolRouter

logger = logging.getLogger(__name__)

#: The entry-point group third-party packages register tools under.
STEERABLE_TOOLS_ENTRY_POINT_GROUP = "steerable.tools"


class PluginLoadError(RuntimeError):
    """A plugin source or plugin failed to load/reload, naming the offender."""


class PluginStateError(RuntimeError):
    """A lifecycle operation named an unknown plugin or one in the wrong
    state (e.g. disabling a disabled plugin, reloading a non-reloadable
    one)."""


@dataclass(slots=True)
class PluginSpec:
    """One discovered plugin: how to resolve its register callable.

    ``load`` resolves the register callable on first load. ``reload``
    re-imports the plugin's module and resolves the callable again, or is
    ``None`` when the source cannot re-import — the plugin then loads once
    and `PluginRegistry.reload` refuses it. ``origin`` is the
    human-readable source (entry-point value or file path) used in error
    messages and logs.
    """

    name: str
    origin: str
    load: Callable[[], Any]
    reload: Callable[[], Any] | None = None


@runtime_checkable
class PluginSource(Protocol):
    """A place plugins are discovered from.

    Implementations return one `PluginSpec` per discovered plugin.
    Discovery fails loud (`PluginLoadError`) when the source itself is
    misconfigured (e.g. a directory that does not exist). Remote sources
    (a plugin market) plug in here; this runtime ships the entry-point and
    local-directory sources.
    """

    def discover(self) -> list[PluginSpec]:
        """Return every plugin this source offers, in discovery order."""
        ...


@dataclass(slots=True)
class PluginRecord:
    """Registry state for one loaded plugin.

    ``tools`` are the tool names the plugin currently has on the router
    (empty while disabled). ``reloadable`` mirrors whether the plugin's
    spec carries a reloader.
    """

    name: str
    origin: str
    tools: list[str]
    enabled: bool
    reloadable: bool


class _RecordingRouter:
    """``ToolRouter`` proxy that captures the names a plugin registers.

    A plugin's register callable receives this proxy in place of the real
    router: ``register``/``register_remote`` forward to the router and
    record the registered name; every other attribute (``get``,
    ``list_tools``, ``unregister``, ...) delegates unchanged. The registry
    uses the captured names to disable, unload, or roll back the plugin's
    tools later.
    """

    def __init__(self, router: ToolRouter) -> None:
        self._router = router
        self.names: list[str] = []

    def register(self, handler: Any, **kwargs: Any) -> RegisteredTool:
        tool = self._router.register(handler, **kwargs)
        self.names.append(tool.name)
        return tool

    def register_remote(self, name: str, invoker: Any, **kwargs: Any) -> RegisteredTool:
        tool = self._router.register_remote(name, invoker, **kwargs)
        self.names.append(tool.name)
        return tool

    def __getattr__(self, attr: str) -> Any:
        return getattr(self._router, attr)


class PluginRegistry:
    """Loads plugins onto a router and owns their lifecycle.

    ``load_source``/``load`` discover and enable; ``disable`` removes a
    plugin's tools from the router but keeps its record so ``enable``
    re-registers them (by re-running the register callable); ``unload``
    drops the record entirely; ``reload`` re-imports the plugin's module
    and swaps its tool registrations.

    Tool-name conflicts fail loud: the router rejects duplicate names, so
    the first registrant (host or plugin) wins and the later plugin's load
    raises `PluginLoadError` naming both the plugin and the tool.
    """

    def __init__(self, router: ToolRouter) -> None:
        self._router = router
        self._specs: dict[str, PluginSpec] = {}
        self._records: dict[str, PluginRecord] = {}

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def load_source(self, source: PluginSource) -> list[str]:
        """Discover every plugin ``source`` offers and load each one.

        Returns the loaded plugin names in discovery order. Raises
        `PluginLoadError` on the first offender; plugins loaded before it
        stay loaded.
        """
        loaded: list[str] = []
        for spec in source.discover():
            loaded.append(self.load(spec))
        return loaded

    def load(self, spec: PluginSpec) -> str:
        """Load one plugin: resolve its register callable and run it."""
        if spec.name in self._records:
            raise PluginLoadError(f"plugin {spec.name!r} is already loaded")
        register = self._resolve(spec)
        tools = self._register_tools(spec, register)
        self._specs[spec.name] = spec
        self._records[spec.name] = PluginRecord(
            name=spec.name,
            origin=spec.origin,
            tools=tools,
            enabled=True,
            reloadable=spec.reload is not None,
        )
        logger.info("loaded plugin %s (%s): %s", spec.name, spec.origin, tools)
        return spec.name

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def enable(self, name: str) -> None:
        """Re-register a disabled plugin's tools by re-running its register
        callable. The plugin's module is not re-imported (use ``reload``
        for that)."""
        record, spec = self._lookup(name)
        if record.enabled:
            raise PluginStateError(f"plugin {name!r} is already enabled")
        register = self._resolve(spec)
        record.tools = self._register_tools(spec, register)
        record.enabled = True
        logger.info("enabled plugin %s: %s", name, record.tools)

    def disable(self, name: str) -> None:
        """Remove a plugin's tools from the router, keeping its record."""
        record, _ = self._lookup(name)
        if not record.enabled:
            raise PluginStateError(f"plugin {name!r} is already disabled")
        self._unregister_tools(record)
        record.enabled = False
        logger.info("disabled plugin %s", name)

    def unload(self, name: str) -> None:
        """Remove a plugin's tools (if enabled) and drop its record."""
        record, _ = self._lookup(name)
        if record.enabled:
            self._unregister_tools(record)
        del self._records[name]
        del self._specs[name]
        logger.info("unloaded plugin %s", name)

    def reload(self, name: str) -> None:
        """Re-import a plugin's module and swap its tool registrations.

        The fresh register callable is resolved before any tool is
        removed, so a re-import failure leaves the current tools in place.
        A failure during re-registration removes the old tools (they were
        already unregistered), marks the plugin disabled, and raises.
        """
        record, spec = self._lookup(name)
        if spec.reload is None:
            raise PluginStateError(
                f"plugin {name!r} ({record.origin}) is not reloadable"
            )
        if not record.enabled:
            raise PluginStateError(
                f"plugin {name!r} is disabled; enable it before reloading"
            )
        try:
            register = spec.reload()
        except PluginLoadError:
            raise
        except Exception as exc:  # noqa: BLE001 — re-raised with the offender named
            raise PluginLoadError(
                f"plugin {name!r} ({record.origin}) failed to re-import: {exc}"
            ) from exc
        if not callable(register):
            raise PluginLoadError(
                f"plugin {name!r} ({record.origin}) reloaded to a non-callable "
                f"register target: got {type(register).__name__}"
            )
        self._unregister_tools(record)
        try:
            record.tools = self._register_tools(spec, register)
        except PluginLoadError:
            record.enabled = False
            raise
        logger.info("reloaded plugin %s: %s", name, record.tools)

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def plugins(self) -> list[PluginRecord]:
        """Every loaded plugin's record, in load order."""
        return list(self._records.values())

    def get(self, name: str) -> PluginRecord | None:
        return self._records.get(name)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _lookup(self, name: str) -> tuple[PluginRecord, PluginSpec]:
        try:
            return self._records[name], self._specs[name]
        except KeyError:
            raise PluginStateError(f"unknown plugin {name!r}") from None

    @staticmethod
    def _resolve(spec: PluginSpec) -> Any:
        try:
            register = spec.load()
        except PluginLoadError:
            raise
        except Exception as exc:  # noqa: BLE001 — re-raised with the offender named
            raise PluginLoadError(
                f"plugin {spec.name!r} ({spec.origin}) failed to import: {exc}"
            ) from exc
        if not callable(register):
            raise PluginLoadError(
                f"plugin {spec.name!r} ({spec.origin}) is not callable: "
                f"got {type(register).__name__}"
            )
        return register

    def _register_tools(self, spec: PluginSpec, register: Any) -> list[str]:
        """Run the register callable against a recording proxy.

        A registration failure rolls back the tools the plugin already
        registered in this run, so a half-loaded plugin never stays on the
        router.
        """
        recording = _RecordingRouter(self._router)
        try:
            register(recording)
        except Exception as exc:  # noqa: BLE001 — re-raised with the offender named
            for tool_name in recording.names:
                self._router.unregister(tool_name)
            if isinstance(exc, ToolDispatchError):
                raise PluginLoadError(
                    f"plugin {spec.name!r} ({spec.origin}) failed to register: {exc}"
                ) from exc
            raise PluginLoadError(
                f"plugin {spec.name!r} ({spec.origin}) raised during "
                f"registration: {exc}"
            ) from exc
        return recording.names

    def _unregister_tools(self, record: PluginRecord) -> None:
        for tool_name in record.tools:
            self._router.unregister(tool_name)
        record.tools = []


class EntryPointSource:
    """Discovers plugins declared under the ``steerable.tools`` entry-point
    group by installed packages (``importlib.metadata``)."""

    def __init__(self, *, group: str = STEERABLE_TOOLS_ENTRY_POINT_GROUP) -> None:
        self._group = group

    def discover(self) -> list[PluginSpec]:
        return [
            PluginSpec(
                name=ep.name,
                origin=f"entry-point:{self._group}:{ep.value}",
                load=ep.load,
                reload=_entry_point_reloader(ep),
            )
            for ep in metadata.entry_points(group=self._group)
        ]


def _entry_point_reloader(ep: metadata.EntryPoint) -> Callable[[], Any]:
    """Reload the module an entry point points into, then re-resolve the
    attribute path from ``ep.value`` (``"pkg.module:attr.sub"``)."""

    def _reload() -> Any:
        module_name, _, attr_path = ep.value.partition(":")
        module = sys.modules.get(module_name)
        if module is None:
            # Not imported under this name — nothing to reload; load fresh.
            return ep.load()
        _reexecute_module(module)
        target: Any = module
        for attr in attr_path.split("."):
            target = getattr(target, attr)
        return target

    return _reload


def _reexecute_module(module: Any) -> None:
    """Re-execute a module's code in place (reload semantics).

    Uses the module's own spec/loader rather than ``importlib.reload``,
    which re-resolves the spec through ``sys.meta_path`` and therefore
    cannot find modules imported from an explicit file path under a
    synthesized name. Falls back to ``importlib.reload`` when the module
    carries no usable spec.

    The source loader's ``exec_module`` validates ``__pycache__`` by
    (mtime, size): a same-size edit landing within the same mtime tick
    would silently re-run the STALE bytecode — hot reload must reflect the
    file on disk, so the source is read and compiled directly, bypassing
    the bytecode cache.
    """
    spec = getattr(module, "__spec__", None)
    loader = getattr(spec, "loader", None)
    path = getattr(spec, "origin", None)
    get_data = getattr(loader, "get_data", None)
    if loader is None or not callable(get_data) or not path:
        importlib.reload(module)
        return
    source = get_data(path)
    code = compile(source, str(path), "exec", dont_inherit=True)
    # Hot reload runs plugin source by design — the file IS the plugin.
    exec(code, module.__dict__)  # noqa: S102


class DirectorySource:
    """Discovers plugins as ``.py`` files in a local directory.

    Each file not prefixed with ``_`` is one plugin, named by its stem;
    the module must define a top-level ``register(router)`` callable. For
    development and site-local plugins that are not packaged as wheels.
    Files import fresh under a synthesized module name, and reload
    re-executes the file in place.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def discover(self) -> list[PluginSpec]:
        if not self._path.is_dir():
            raise PluginLoadError(f"plugin directory does not exist: {self._path}")
        return [
            self._spec_for(file)
            for file in sorted(self._path.glob("*.py"))
            if not file.stem.startswith("_")
        ]

    @staticmethod
    def _spec_for(file: Path) -> PluginSpec:
        module_name = f"steerable_plugin_{file.stem}"

        def _load() -> Any:
            return _register_attr(_import_file_module(module_name, file), file)

        def _reload() -> Any:
            module = sys.modules.get(module_name)
            if module is None:
                return _load()
            _reexecute_module(module)
            return _register_attr(module, file)

        return PluginSpec(
            name=file.stem,
            origin=f"directory:{file}",
            load=_load,
            reload=_reload,
        )


def _import_file_module(module_name: str, file: Path) -> Any:
    """Import ``file`` as a fresh module named ``module_name``."""
    spec = importlib.util.spec_from_file_location(module_name, file)
    if spec is None or spec.loader is None:
        raise PluginLoadError(f"cannot import plugin file: {file}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        # Don't leave a half-initialized module behind for the next import.
        sys.modules.pop(module_name, None)
        raise
    return module


def _register_attr(module: Any, file: Path) -> Any:
    register = getattr(module, "register", None)
    if register is None:
        raise PluginLoadError(
            f"plugin file {file} defines no top-level 'register' callable"
        )
    return register


def load_tool_entry_points(
    router: ToolRouter,
    *,
    group: str = STEERABLE_TOOLS_ENTRY_POINT_GROUP,
) -> list[str]:
    """Discover and load every entry point in ``group`` onto ``router``.

    Thin back-compat wrapper over `PluginRegistry` + `EntryPointSource` for
    boot paths that load once and never manage plugins afterwards. Returns
    the loaded plugin names in discovery order; raises `PluginLoadError` on
    the first offender.
    """
    return PluginRegistry(router).load_source(EntryPointSource(group=group))
