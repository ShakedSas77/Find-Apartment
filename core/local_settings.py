"""
Writing the user's own settings: NAME = value lines in config_local.py and NAME=value lines in .env
(both gitignored), touching only the names written and keeping every other line. Used by
setup_wizard.py and the web app's Setup/Settings pages. A value also applies to the running process
(config attributes, os.environ); a scan, started as a new process, reads the files.
"""
import ast
import os
from pathlib import Path

import config

_PROJECT_DIR = Path(__file__).resolve().parent.parent
_CONFIG_LOCAL = _PROJECT_DIR / "config_local.py"
_ENV_FILE = _PROJECT_DIR / ".env"


def save_settings(values: dict, path: Path = _CONFIG_LOCAL):
    """Sets each NAME = value in config_local.py: an existing assignment (even one spanning several lines)
    is replaced in place, a new one appended; everything else in the file is kept."""
    text = path.read_text(encoding="utf-8") if path.exists() else (
        '"""Your settings (written by the setup wizard and the web app; gitignored). Anything here overrides config.py."""\n')
    lines = text.splitlines()
    spans = {target.id: (node.lineno, node.end_lineno)
             for node in ast.parse(text).body if isinstance(node, ast.Assign)
             for target in node.targets if isinstance(target, ast.Name)}
    for name, value in sorted(values.items(), key=lambda item: -spans.get(item[0], (0, 0))[0]):
        line = f"{name} = {value!r}"
        if name in spans:
            start, end = spans[name]
            lines[start - 1:end] = [line]
        else:
            lines.append(line)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    for name, value in values.items():
        setattr(config, name, value)


def save_secrets(values: dict, path: Path = _ENV_FILE):
    """Sets each NAME=value line in .env, replacing an existing one and keeping every other line."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    for name, value in values.items():
        new_line = f"{name}={value}"
        index = next((i for i, line in enumerate(lines) if line.split("=", 1)[0].strip() == name), None)
        if index is None:
            lines.append(new_line)
        else:
            lines[index] = new_line
        os.environ[name] = value
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def is_saved(name: str, path: Path = _CONFIG_LOCAL) -> bool:
    """Whether config_local.py sets `name`, as opposed to config.py's example value."""
    if not path.exists():
        return False
    return any(isinstance(target, ast.Name) and target.id == name
               for node in ast.parse(path.read_text(encoding="utf-8")).body if isinstance(node, ast.Assign)
               for target in node.targets)
