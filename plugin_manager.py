"""
plugin_manager.py
──────────────────────────────────────────────────────────────────────────
Lets an admin add a brand-new standalone feature (a new command, a new
callback flow, a new admin panel section) WITHOUT editing hosting_panel.py
and WITHOUT restarting the bot process.

How a plugin looks (plugins/my_feature.py):

    from aiogram import Router, types
    from aiogram.filters import Command

    PLUGIN_NAME = "My Feature"
    PLUGIN_DESCRIPTION = "Does a thing."

    router = Router()

    @router.message(Command("myfeature"))
    async def my_handler(message: types.Message):
        await message.answer("Hello from a hot-loaded plugin!")

`router` is the one required attribute — plugin_manager.load_all() finds
it and calls dp.include_router(router), which aiogram accepts at any
time, live process included. That's what makes this restart-free for
NEW features.

What this does NOT solve (be upfront about it, per the plugin's own
docstring warnings):
  - Editing/removing an ALREADY-loaded plugin's behavior still needs a
    restart — aiogram doesn't support cleanly un-including a router.
    Overwriting the .py file on disk does nothing to the routes already
    wired into the running dispatcher.
  - Changing hosting_panel.py's own core logic (upload flow, run flow,
    admin panel structure) is not something a plugin can hook into —
    plugins are additive, not a way to patch the core.
  - Plugins run in the SAME process as the main bot with full trust —
    there is no sandboxing. This is why installing one is admin/owner
    only, and why every upload goes through validate_plugin_source()
    first as a best-effort safety net, not a real sandbox boundary.
"""
import ast
import importlib.util
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# Best-effort static check before a plugin is ever imported into the live
# process. This is a safety net against an obviously dangerous plugin
# slipping in by mistake — it is NOT a sandbox. Only admins/owner can
# install plugins in the first place; this exists to catch accidents,
# not to defend against a malicious admin.
BLOCKED_IMPORT_MODULES = {
    'os', 'subprocess', 'socket', 'ctypes', 'pty', 'shutil',
    'multiprocessing', 'importlib',
}
BLOCKED_CALL_NAMES = {'eval', 'exec', 'compile', '__import__'}


def validate_plugin_source(source: str):
    """
    AST-level check — parses the plugin without executing it. Returns
    (ok, reason). Blocks the imports/calls most likely to let a plugin
    reach outside the bot process (shell exec, raw sockets, ctypes) or
    run arbitrary strings as code.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return False, f"Syntax error: {e}"

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split('.')[0]
                if top in BLOCKED_IMPORT_MODULES:
                    return False, f"Blocked import: {top}"
        elif isinstance(node, ast.ImportFrom):
            top = (node.module or '').split('.')[0]
            if top in BLOCKED_IMPORT_MODULES:
                return False, f"Blocked import: {top}"
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else (
                func.attr if isinstance(func, ast.Attribute) else None
            )
            if name in BLOCKED_CALL_NAMES:
                return False, f"Blocked call: {name}(...)"

    if 'router' not in source:
        return False, "No 'router' object found — every plugin must define a module-level aiogram Router named 'router'."

    return True, None


def safe_plugin_filename(display_name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_-]+", "_", display_name).strip("_")[:50] or "plugin"
    return f"{name}.py"


def load_one(path: Path, dp):
    """
    Imports a single plugin file and includes its router into the live
    dispatcher. Returns (ok, name_or_error).
    """
    source = path.read_text(encoding='utf-8', errors='ignore')
    ok, reason = validate_plugin_source(source)
    if not ok:
        return False, reason

    module_name = f"plugins.{path.stem}"
    try:
        spec = importlib.util.spec_from_file_location(module_name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as e:
        return False, f"Failed to load: {e}"

    router = getattr(module, 'router', None)
    if router is None:
        return False, "Plugin has no 'router' attribute after loading."

    try:
        dp.include_router(router)
    except Exception as e:
        return False, f"Failed to attach router: {e}"

    display_name = getattr(module, 'PLUGIN_NAME', path.stem)
    description = getattr(module, 'PLUGIN_DESCRIPTION', '')
    return True, {'name': display_name, 'description': description, 'file': path.name}


def load_all(plugins_dir: Path, dp):
    """Called once at startup — loads every .py file already sitting in
    plugins/. Returns a dict of file_name -> {name, description, file}
    for whatever loaded successfully; failures are logged and skipped."""
    loaded = {}
    if not plugins_dir.exists():
        return loaded
    for path in sorted(plugins_dir.glob("*.py")):
        ok, result = load_one(path, dp)
        if ok:
            loaded[path.name] = result
            logger.info(f"Loaded plugin: {result['name']} ({path.name})")
        else:
            logger.error(f"Skipped plugin {path.name}: {result}")
    return loaded
