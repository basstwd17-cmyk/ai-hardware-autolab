"""Per-invocation workspace imports; never replace the daemon's modules.

Normal import/from statements can use workspace helpers. This is not a Python
security sandbox: the host still owns device dispatch, transport and records.
"""
from __future__ import annotations

import builtins
import functools
import importlib
import importlib.util
import sys
import threading
import types
import uuid
from contextlib import contextmanager
from pathlib import Path


HOST_PREFIXES = (
    'QKD_PROJ.orchestrator', 'QKD_PROJ.mcp_server',
    *(f'QKD_PROJ.hwtools.{name}' for name in (
        'safety', 'record', 'result', 'scpi', 'ssh', 'instruments', 'scope',
        'workspace_recipe', 'recipe_imports', 'bbb', 'jtag', 'sram', 'gpio',
        'cpld', 'board_adapter',
    )),
)
HOST_PACKAGES = {'QKD_PROJ', 'QKD_PROJ.hwtools', 'QKD_PROJ.hwtools.recipes'}
HOST_FACADES = {'QKD_PROJ', 'QKD_PROJ.hwtools'}


class RecipeImports:
    def __init__(self, root: Path, entry: Path, source_sink=None):
        self.root = root.resolve()
        self.entry = entry
        self.source_sink = source_sink
        self.modules = {}
        self.local_modules = {}
        self.prefix = '_qkd_recipe_' + uuid.uuid4().hex
        self.lock = threading.RLock()
        self.importlib_proxy = types.ModuleType('importlib')
        self.importlib_proxy.__dict__.update(vars(importlib))
        self.importlib_proxy.import_module = self.dynamic_import
        parts = entry.relative_to(self.root).with_suffix('').parts
        if len(parts) > 1 and parts[-1] == '__init__':
            parts = parts[:-1]
        self.entry_name = '.'.join(parts) if (len(parts) > 1 or entry.name == '__init__.py') and all(p.isidentifier() for p in parts) else 'QKD_PROJ.hwtools.recipes._job_recipe'
        # The selected source hash must identify the code actually executed.
        # Never silently substitute a host function for an edited entrypoint.
        if self._host(self.entry_name) or self.entry_name in HOST_FACADES:
            raise ValueError('recipe entrypoint names a host-owned platform module; use a workspace recipe/helper module')
        # Standalone capture.py historically used ..scope and ..instruments.
        # Preserve that compatibility and allow .helper from the same folder.
        self.standalone = self.entry_name.endswith('._job_recipe')

    @contextmanager
    def active(self):
        with self.lock:
            for module in self.local_modules.values():
                sys.modules[module.__name__] = module
            try:
                yield
            finally:
                for module in self.local_modules.values():
                    sys.modules.pop(module.__name__, None)

    def _path(self, name):
        if not all(part.isidentifier() for part in name.split('.')):
            return None
        candidates = []
        if self.standalone and name.startswith('QKD_PROJ.hwtools.recipes.'):
            tail = name.removeprefix('QKD_PROJ.hwtools.recipes.')
            candidates.append(self.entry.parent.joinpath(*tail.split('.')))
        candidates.append(self.root.joinpath(*name.split('.')))
        for candidate in candidates:
            for path in (candidate.with_suffix('.py'), candidate / '__init__.py'):
                if path.is_file():
                    resolved = path.resolve()
                    if not resolved.is_relative_to(self.root):
                        raise ValueError(f'workspace import escapes its root: {name}')
                    return resolved
        return None

    @staticmethod
    def _host(name):
        return any(name == prefix or name.startswith(prefix + '.') for prefix in HOST_PREFIXES)

    def load(self, name, *, path=None, content=None):
        if name in self.modules:
            return self.modules[name]
        if self._host(name):
            return importlib.import_module(name)
        path = path or self._path(name)
        if path is None and name not in HOST_PACKAGES:
            # Namespace packages (no __init__.py) are useful for small helpers.
            directory = self.root.joinpath(*name.split('.')).resolve()
            if not directory.is_relative_to(self.root):
                raise ValueError('workspace package escapes its root')
            if not directory.is_dir():
                return importlib.import_module(name)
        package = name in HOST_PACKAGES or path is None or path.name == '__init__.py'
        module = types.ModuleType(self.prefix + '.' + name)
        module.__package__ = name if package else name.rpartition('.')[0]
        module.__file__ = str(path) if path else None
        if package:
            module.__path__ = []  # importlib cannot accidentally load host-name aliases
        module.__dict__['__builtins__'] = {**vars(builtins), '__import__': self.import_module}
        self.modules[name] = self.local_modules[name] = module
        sys.modules[module.__name__] = module  # dataclasses resolves __module__ here
        if name in HOST_FACADES or (name in HOST_PACKAGES and path is None):
            # Do not execute workspace package initialisers that re-export gates.
            def host_attribute(attribute):
                return getattr(importlib.import_module(name), attribute)
            module.__getattr__ = host_attribute
        elif path is not None:
            content = content if content is not None else path.read_bytes()
            if self.source_sink:
                self.source_sink(path.relative_to(self.root).as_posix(), content)
            try:
                exec(compile(content, str(path), 'exec'), module.__dict__)
            except BaseException:
                self.modules.pop(name, None)
                self.local_modules.pop(name, None)
                sys.modules.pop(module.__name__, None)
                raise
        return module

    def _local(self, name):
        return (name in self.modules or name in HOST_PACKAGES or self._path(name) is not None
                or self.root.joinpath(*name.split('.')).is_dir())

    def import_module(self, name, globals=None, locals=None, fromlist=(), level=0):
        absolute = importlib.util.resolve_name('.' * level + name, (globals or {}).get('__package__', '')) if level else name
        if absolute == 'importlib':
            return self.importlib_proxy
        if self._host(absolute) or not self._local(absolute):
            return builtins.__import__(name, globals, locals, fromlist, level)
        module = self.load(absolute)
        if fromlist:
            for item in fromlist:
                if item == '*':
                    continue
                child = absolute + '.' + item
                # A host package can already have a host helper attribute. A
                # workspace helper must win there, without changing that package.
                if ((absolute in HOST_FACADES or item not in module.__dict__)
                        and (self._host(child) or self._local(child))):
                    setattr(module, item, self.load(child))
            return module
        # import a.b.c returns a; bind the private chain just as Python does.
        child = module
        current = absolute
        while '.' in current:
            parent, _, leaf = current.rpartition('.')
            container = self.load(parent)
            setattr(container, leaf, child)
            child, current = container, parent
        return child

    def dynamic_import(self, name, package=None):
        absolute = importlib.util.resolve_name(name, package) if name.startswith('.') else name
        return self.load(absolute) if not self._host(absolute) and self._local(absolute) else importlib.import_module(absolute)

    def recipe(self, symbol, content):
        with self.active():
            module = self.load(self.entry_name, path=self.entry, content=content)
            function = getattr(module, symbol, None)
            if not callable(function):
                raise ValueError(f'recipe has no callable {symbol!r}')

        @functools.wraps(function)
        def invoke(*args, **kwargs):
            with self.active():
                return function(*args, **kwargs)
        return invoke
