from pathlib import Path
from runpy import run_path

import pytest

script = run_path(str(Path(__file__).resolve().parents[2] / "scripts/prepare_fresh_supabase.py"))
BASELINE_NAME = script["BASELINE_NAME"]
prepare = script["prepare"]


def fixture(tmp_path: Path) -> tuple[Path, Path]:
    repository = tmp_path / "repo"
    source = repository / "supabase" / "migrations"
    source.mkdir(parents=True)
    (source / "20261101000000_old.sql").write_text("old")
    (source / "20261118100059_represented.sql").write_text("represented")
    (source / "20261118110000_later.sql").write_text("later")
    baseline = repository / "supabase" / "bootstrap" / BASELINE_NAME
    baseline.parent.mkdir(parents=True)
    baseline.write_text("baseline")
    workdir = tmp_path / "fresh"
    (workdir / "supabase").mkdir(parents=True)
    (workdir / "supabase" / "config.toml").write_text('project_id = "test"\n')
    return repository, workdir


def test_prepares_baseline_then_only_later_migrations(tmp_path):
    repository, workdir = fixture(tmp_path)
    names = prepare(workdir, repository)
    assert names == [BASELINE_NAME, "20261118110000_later.sql"]
    assert (workdir / "supabase" / "migrations" / BASELINE_NAME).read_text() == "baseline"
    with pytest.raises(ValueError, match="must be empty"):
        prepare(workdir, repository)


def test_refuses_uninitialized_workdir_and_unexpected_names(tmp_path):
    repository, workdir = fixture(tmp_path)
    (workdir / "supabase" / "config.toml").unlink()
    with pytest.raises(ValueError, match="supabase init"):
        prepare(workdir, repository)
    (workdir / "supabase" / "config.toml").write_text("")
    (repository / "supabase" / "migrations" / "invalid.sql").write_text("")
    with pytest.raises(ValueError, match="Invalid migration filename"):
        prepare(workdir, repository)
    assert not (workdir / "supabase" / "migrations").exists()


def test_native_option_enables_stack_without_losing_other_settings(tmp_path):
    repository, workdir = fixture(tmp_path)
    config = workdir / "supabase" / "config.toml"
    config.write_text(
        'project_id = "test"\n[experimental]\nstack = false\n'
        "[experimental.pgdelta]\nenabled = true\n"
    )
    prepare(workdir, repository, native=True)
    import tomllib

    parsed = tomllib.loads(config.read_text())
    assert parsed["experimental"]["stack"] is True
    assert parsed["experimental"]["pgdelta"]["enabled"] is True


def test_native_option_refuses_existing_migrations_before_changing_config(tmp_path):
    repository, workdir = fixture(tmp_path)
    config = workdir / "supabase" / "config.toml"
    original = config.read_text()
    directory = workdir / "supabase" / "migrations"
    directory.mkdir()
    (directory / "existing.sql").write_text("keep")
    with pytest.raises(ValueError, match="must be empty"):
        prepare(workdir, repository, native=True)
    assert config.read_text() == original
    assert (directory / "existing.sql").read_text() == "keep"


def test_sync_adds_only_newer_migrations_and_is_idempotent(tmp_path):
    repository, workdir = fixture(tmp_path)
    prepare(workdir, repository)
    new = repository / "supabase" / "migrations" / "20261118120000_new.sql"
    new.write_text("new")
    assert prepare(workdir, repository, sync=True) == [new.name]
    assert prepare(workdir, repository, sync=True) == []


def test_sync_refuses_inserting_an_older_missing_migration(tmp_path):
    repository, workdir = fixture(tmp_path)
    prepare(workdir, repository)
    (repository / "supabase" / "migrations" / "20261118103000_inserted.sql").write_text("new")
    with pytest.raises(ValueError, match="consecutive prefix"):
        prepare(workdir, repository, sync=True)


@pytest.mark.parametrize("problem", ["missing", "changed", "unexpected", "symlink"])
def test_sync_refuses_unverified_existing_migrations(tmp_path, problem):
    repository, workdir = fixture(tmp_path)
    prepare(workdir, repository)
    directory = workdir / "supabase" / "migrations"
    baseline = directory / BASELINE_NAME
    if problem == "missing":
        baseline.unlink()
    elif problem == "changed":
        baseline.write_text("modified")
    elif problem == "unexpected":
        (directory / "unknown.sql").write_text("unknown")
    else:
        baseline.unlink()
        baseline.symlink_to(repository / "supabase" / "bootstrap" / BASELINE_NAME)
    (repository / "supabase" / "migrations" / "20261118120000_new.sql").write_text("new")
    with pytest.raises(ValueError):
        prepare(workdir, repository, sync=True)
    assert not (directory / "20261118120000_new.sql").exists()
