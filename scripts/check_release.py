"""Fail closed on common credentials, runtime data and unexpected release files.

Not a substitute for manual review of Git history / GitHub's secret scanning.
"""
from __future__ import annotations

import argparse
import re
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWED_SUFFIXES = {".py", ".txt", ".rst", ".example", ".yml", ".yaml", ".toml", ".gitignore"}
ALLOWED_PATHS = {".gitignore"}
IGNORED_RUNTIME = {"__pycache__", ".pytest_cache", ".venv", "venv", ".git", ".mypy_cache", ".ruff_cache"}
FORBIDDEN_COMPONENTS = {"data", "storage", "uploads", "media", "backups", "backup", "logs", ".idea", ".vscode"}
FORBIDDEN_SUFFIXES = {".db", ".db3", ".sqlite", ".sqlite3", ".pem", ".key", ".pfx", ".p12", ".session", ".log", ".sql", ".dump", ".zip"}
SECRET_PATTERNS = {
    "telegram_bot_token": re.compile(r"\b\d{7,12}:[A-Za-z0-9_-]{25,}\b"),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "github_token": re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b"),
    "telegram_api_hash": re.compile(r"\b(?:TELEGRAM_API_HASH|api_hash)\b\s*[:=]\s*['\"]?[a-f0-9]{32}\b", re.I),
    "github_fine_grained_token": re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}\b"),
    "slack_token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    "email": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
}


def scan(root: Path, *, strict: bool = False) -> list[str]:
    errors: list[str] = []
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            errors.append(f"symlink: {relative}")
            continue
        if any(part in IGNORED_RUNTIME for part in path.relative_to(root).parts):
            if strict:
                errors.append(f"excluded runtime/git content: {relative}")
            continue
        if path.is_dir():
            if path.name in FORBIDDEN_COMPONENTS:
                errors.append(f"runtime directory: {relative}")
            continue
        if path.name == ".env" or (path.name.startswith(".env.") and path.name != ".env.example"):
            errors.append(f"environment secret file: {relative}")
            continue
        if path.suffix.lower() in FORBIDDEN_SUFFIXES or re.search(r"\.(db|sqlite|sqlite3)-(?:wal|shm|journal)$",path.name,re.I):
            errors.append(f"runtime or secret file: {relative}")
            continue
        if path.suffix.lower() not in ALLOWED_SUFFIXES and path.name not in ALLOWED_PATHS:
            errors.append(f"unexpected file type: {relative}")
            continue
        if path.stat().st_size > 500_000:
            errors.append(f"oversized tracked file: {relative}")
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeError:
            errors.append(f"non-text file: {relative}")
            continue
        if "\x00" in content:
            errors.append(f"binary data in text file: {relative}")
            continue
        for name, pattern in SECRET_PATTERNS.items():
            if pattern.search(content):
                errors.append(f"possible {name}: {relative}")
        if path.name == ".env.example":
            for line in content.splitlines():
                if not line or line.startswith("#"):
                    continue
                key, delim, value = line.partition("=")
                if not delim or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
                    errors.append("invalid .env.example entry")
                if key == "API_TOKEN" and value.strip() != "CHANGE_ME":
                    errors.append("API_TOKEN in .env.example is not a placeholder")
                if key == "ADMIN_ID" and value.strip() != "0":
                    errors.append("ADMIN_ID in .env.example is not empty/default")
                if key in {"CHANNEL_USERNAMES", "TELEGRAM_API_ID", "TELEGRAM_API_HASH"} and value.strip():
                    errors.append(key + " in .env.example must remain empty")
    return sorted(set(errors))


def scan_archive(archive: Path) -> list[str]:
    """Inspect the actual shipped ZIP, including content normally ignored by Git."""
    with zipfile.ZipFile(archive) as z:
        members = z.infolist()
        if len(members) > 10000 or sum(i.file_size for i in members) > 20_000_000:
            return ["archive exceeds source-release size limit"]
        for item in members:
            name = item.filename
            parts = Path(name).parts
            if (not name or name.startswith(("/", "\\")) or "\\" in name
                    or ":" in name or ".." in parts
                    or ((item.external_attr >> 16) & 0o170000) == 0o120000):
                return ["unsafe archive member"]
            if item.file_size > 500_000:
                return ["oversized archive member"]
        if len({i.filename for i in members}) != len(members):
            return ["duplicate archive member"]
        with tempfile.TemporaryDirectory() as temp:
            z.extractall(temp)
            return scan(Path(temp), strict=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Check public sources for data and common secrets")
    parser.add_argument("--archive", type=Path, help="Check the exact ZIP before publishing")
    parser.add_argument("--strict", action="store_true", help="Reject caches, environments and Git history too")
    args = parser.parse_args()
    try:
        failures = scan_archive(args.archive) if args.archive else scan(ROOT, strict=args.strict)
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        print("FAIL: cannot inspect release (" + type(exc).__name__ + ")")
        return 1
    for failure in failures:
        print("FAIL:", failure)
    if failures:
        return 1
    print("PASS: no common runtime data or embedded secrets in the inspected release")
    return 0


if __name__ == "__main__":
    sys.exit(main())
