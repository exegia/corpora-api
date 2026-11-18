#!/usr/bin/env python3
"""Prepare an isolated Supabase workdir from the reviewed fresh baseline."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import tempfile
import tomllib
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
BASELINE_VERSION = "20261118100059"
BASELINE_NAME = BASELINE_VERSION + "_fresh_baseline.sql"
MIGRATION_NAME = re.compile(r"^(\d{14})_[a-z0-9_]+\.sql$")


def native_config(text: str) -> str:
    """Enable the CLI's native stack without rewriting unrelated settings."""
    parsed = tomllib.loads(text)
    experimental = parsed.get("experimental", {})
    if not isinstance(experimental, dict):
        raise ValueError("Invalid experimental Supabase configuration")
    if experimental.get("stack") is True:
        return text
    lines = text.splitlines(keepends=True)
    headers = [
        i
        for i, line in enumerate(lines)
        if re.match(r"^\s*\[experimental\]\s*(?:#.*)?$", line.rstrip())
    ]
    if not headers:
        if "experimental" in parsed:
            raise ValueError("Cannot safely modify the experimental configuration")
        result = text.rstrip() + "\n\n[experimental]\nstack = true\n"
    else:
        start = headers[0] + 1
        if not lines[start - 1].endswith("\n"):
            lines[start - 1] += "\n"
        end = next(
            (i for i in range(start, len(lines)) if re.match(r"^\s*\[", lines[i])), len(lines)
        )
        assignments = [i for i in range(start, end) if re.match(r"^\s*stack\s*=", lines[i])]
        if "stack" in experimental and not assignments:
            raise ValueError("Cannot safely modify the native stack setting")
        if assignments:
            lines[assignments[0]] = "stack = true\n"
        else:
            lines.insert(start, "stack = true\n")
        result = "".join(lines)
    if tomllib.loads(result).get("experimental", {}).get("stack") is not True:
        raise ValueError("Could not enable the native stack")
    return result


def prepare(
    workdir: Path, repository: Path = REPOSITORY, *, native: bool = False, sync: bool = False
) -> list[str]:
    """Copy a baseline and later migrations into an otherwise empty initialized workdir."""
    root = workdir.resolve(strict=True)
    supabase = root / "supabase"
    config = supabase / "config.toml"
    if not config.is_file():
        raise ValueError("Run supabase init in the new workdir first")
    if native and config.is_symlink():
        raise ValueError("Native setup requires a regular configuration file")
    updated_config = native_config(config.read_text()) if native else None
    destination = supabase / "migrations"
    if destination.is_symlink() or (
        not sync and destination.exists() and any(destination.iterdir())
    ):
        raise ValueError("The destination migration directory must be empty")
    source = repository / "supabase" / "migrations"
    baseline = repository / "supabase" / "bootstrap" / BASELINE_NAME
    if not baseline.is_file():
        raise ValueError("The fresh baseline is missing")
    newer: list[Path] = []
    versions = {BASELINE_VERSION}
    for item in sorted(source.glob("*.sql")):
        match = MIGRATION_NAME.fullmatch(item.name)
        if not match:
            raise ValueError(f"Invalid migration filename: {item.name}")
        version = match.group(1)
        if version > BASELINE_VERSION:
            if version in versions:
                raise ValueError(f"Duplicate migration version: {version}")
            versions.add(version)
            newer.append(item)
    sources = {item.name: item for item in (baseline, *newer)}
    if sync:
        if not destination.is_dir() or not (destination / BASELINE_NAME).is_file():
            raise ValueError("Sync requires the original prepared baseline")
        for existing in destination.iterdir():
            expected = sources.get(existing.name)
            if (
                existing.is_symlink()
                or not existing.is_file()
                or expected is None
                or existing.read_bytes() != expected.read_bytes()
            ):
                raise ValueError(f"Unexpected or changed migration: {existing.name}")
        present = sorted(item.name for item in destination.iterdir())
        if present != sorted(sources)[: len(present)]:
            raise ValueError("Existing migrations must be a consecutive prefix")
    destination.mkdir(exist_ok=True)
    copied: list[Path] = []
    try:
        for item in (baseline, *newer):
            target = destination / item.name
            if sync and target.exists():
                continue
            with item.open("rb") as src, target.open("xb") as out:
                copied.append(target)
                shutil.copyfileobj(src, out)
        if updated_config is not None and updated_config != config.read_text():
            with tempfile.NamedTemporaryFile(mode="w", dir=supabase, delete=False) as out:
                temporary = Path(out.name)
                try:
                    out.write(updated_config)
                    out.flush()
                    os.chmod(temporary, config.stat().st_mode & 0o777)
                    os.replace(temporary, config)
                finally:
                    temporary.unlink(missing_ok=True)
    except Exception:
        for target in copied:
            target.unlink(missing_ok=True)
        raise
    return [item.name for item in copied]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", type=Path, required=True, help="A new supabase init workdir")
    parser.add_argument("--native", action="store_true", help="Enable the CLI's native stack")
    parser.add_argument(
        "--sync", action="store_true", help="Copy new migrations into a verified workdir"
    )
    args = parser.parse_args()
    try:
        names = prepare(args.workdir, native=args.native, sync=args.sync)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Could not prepare fresh Supabase migrations: {error}\n")
    print(f"Prepared {len(names)} migrations in {args.workdir / 'supabase' / 'migrations'}")
    print("Use only on an empty Supabase project; the baseline refuses existing application data.")


if __name__ == "__main__":
    main()
