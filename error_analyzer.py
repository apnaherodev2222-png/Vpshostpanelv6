"""
error_analyzer.py
──────────────────────────────────────────────────────────────────────────
Lightweight, regex-based post-mortem analyzer for the "Auto-Fix" feature.
Runs on a finished container's log output and tries to recognize a small
set of common, mechanical Python/JS errors:
    - missing pip / npm package  -> auto-fixable (adds to
      requirements.txt / package.json and offers a one-tap retry)
    - syntax / indentation / name / reference errors -> NOT auto-applied
      (we never rewrite the user's code); we just point at the likely
      cause so they can fix it themselves.

This is intentionally conservative: if nothing matches, analyze_error()
returns None and the caller just shows the raw logs as before.
"""
import re

PY_MODULE_RE = re.compile(r"ModuleNotFoundError: No module named '([\w\.\-]+)'")
PY_IMPORT_RE = re.compile(r"ImportError: cannot import name '(\w+)' from '([\w\.]+)'")
PY_SYNTAX_LINE_RE = re.compile(r'File "[^"]+", line (\d+)')
PY_SYNTAX_MSG_RE = re.compile(r"SyntaxError: (.+)")
PY_NAME_RE = re.compile(r"NameError: name '(\w+)' is not defined")
PY_INDENT_RE = re.compile(r"IndentationError: (.+)")
PY_ATTR_RE = re.compile(r"AttributeError: (.+)")

JS_MODULE_RE = re.compile(r"Cannot find module '([\w\.\-/@]+)'")
JS_REFERENCE_RE = re.compile(r"ReferenceError: (\w+) is not defined")
JS_SYNTAX_RE = re.compile(r"SyntaxError: (.+)")
JS_TYPE_RE = re.compile(r"TypeError: (.+)")


def analyze_error(log_text: str, lang: str):
    """
    Returns a dict describing the most likely root cause, or None if no
    known pattern matched.

    dict shape:
      type          short machine tag, e.g. 'missing_module'
      message       human-readable one-liner for what went wrong
      suggestion    plain-English suggestion for the user
      auto_fixable  bool — True only for missing-dependency cases
      fix_kind      'add_requirement' | 'add_npm_dep'   (only if auto_fixable)
      fix_value     the package name to add               (only if auto_fixable)
    """
    if not log_text or not log_text.strip():
        return None

    if lang == 'py':
        m = PY_MODULE_RE.search(log_text)
        if m:
            pkg = m.group(1).split('.')[0]
            return {
                'type': 'missing_module',
                'message': f"Missing Python package: {pkg}",
                'suggestion': f"Add '{pkg}' to requirements.txt and rerun.",
                'auto_fixable': True,
                'fix_kind': 'add_requirement',
                'fix_value': pkg,
            }
        m = PY_IMPORT_RE.search(log_text)
        if m:
            return {
                'type': 'import_error',
                'message': f"Cannot import '{m.group(1)}' from '{m.group(2)}'",
                'suggestion': f"Double-check the package/version for '{m.group(2)}' — the name may have moved or changed.",
                'auto_fixable': False,
            }
        line_m = PY_SYNTAX_LINE_RE.search(log_text)
        msg_m = PY_SYNTAX_MSG_RE.search(log_text)
        if msg_m:
            where = f" at line {line_m.group(1)}" if line_m else ""
            return {
                'type': 'syntax_error',
                'message': f"Syntax error{where}: {msg_m.group(1)}",
                'suggestion': "Check that line for a missing colon, bracket, quote, or indentation issue.",
                'auto_fixable': False,
            }
        m = PY_INDENT_RE.search(log_text)
        if m:
            return {
                'type': 'indentation_error',
                'message': f"Indentation error: {m.group(1)}",
                'suggestion': "Mixed tabs/spaces or an inconsistent indent level — check the block just above the error.",
                'auto_fixable': False,
            }
        m = PY_NAME_RE.search(log_text)
        if m:
            return {
                'type': 'name_error',
                'message': f"'{m.group(1)}' is used but never defined.",
                'suggestion': "Check for a typo in the variable/function name, or a missing import/assignment.",
                'auto_fixable': False,
            }
        m = PY_ATTR_RE.search(log_text)
        if m:
            return {
                'type': 'attribute_error',
                'message': f"AttributeError: {m.group(1)}",
                'suggestion': "The object doesn't have that attribute/method — check spelling or the object's type.",
                'auto_fixable': False,
            }

    else:  # js
        m = JS_MODULE_RE.search(log_text)
        if m and not m.group(1).startswith('.'):
            pkg = m.group(1)
            return {
                'type': 'missing_module',
                'message': f"Missing npm package: {pkg}",
                'suggestion': f"Add '{pkg}' to package.json and rerun.",
                'auto_fixable': True,
                'fix_kind': 'add_npm_dep',
                'fix_value': pkg,
            }
        m = JS_REFERENCE_RE.search(log_text)
        if m:
            return {
                'type': 'reference_error',
                'message': f"'{m.group(1)}' is not defined.",
                'suggestion': "Check for a typo, or a missing require()/import for it.",
                'auto_fixable': False,
            }
        m = JS_TYPE_RE.search(log_text)
        if m:
            return {
                'type': 'type_error',
                'message': f"TypeError: {m.group(1)}",
                'suggestion': "Something is undefined/null or the wrong type where a call or property access happened.",
                'auto_fixable': False,
            }
        m = JS_SYNTAX_RE.search(log_text)
        if m:
            return {
                'type': 'syntax_error',
                'message': f"Syntax error: {m.group(1)}",
                'suggestion': "Check brackets, quotes, and semicolons near the reported location.",
                'auto_fixable': False,
            }

    return None


def apply_auto_fix(user_folder, fix_kind: str, fix_value: str):
    """
    Applies an auto-fixable diagnosis by editing the user's dependency
    manifest only — never the script itself. Returns (ok: bool, message: str).
    """
    from pathlib import Path
    user_folder = Path(user_folder)

    if fix_kind == 'add_requirement':
        req_path = user_folder / 'requirements.txt'
        existing = set()
        if req_path.exists():
            existing = {
                line.strip().split('==')[0].split('>=')[0].split('<=')[0].lower()
                for line in req_path.read_text(encoding='utf-8', errors='ignore').splitlines()
                if line.strip()
            }
        if fix_value.lower() in existing:
            return True, f"'{fix_value}' is already in requirements.txt."
        with open(req_path, 'a', encoding='utf-8') as f:
            if req_path.exists() and req_path.stat().st_size > 0:
                f.write('\n')
            f.write(fix_value)
        return True, f"Added '{fix_value}' to requirements.txt."

    if fix_kind == 'add_npm_dep':
        import json
        pkg_path = user_folder / 'package.json'
        if pkg_path.exists():
            try:
                data = json.loads(pkg_path.read_text(encoding='utf-8'))
            except Exception:
                data = {}
        else:
            data = {}
        data.setdefault('name', 'hosted-script')
        data.setdefault('version', '1.0.0')
        deps = data.setdefault('dependencies', {})
        if fix_value in deps:
            return True, f"'{fix_value}' is already in package.json."
        deps[fix_value] = 'latest'
        pkg_path.write_text(json.dumps(data, indent=2), encoding='utf-8')
        return True, f"Added '{fix_value}' to package.json."

    return False, "Unknown fix type."
