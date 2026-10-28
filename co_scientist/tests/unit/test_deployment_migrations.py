from co_scientist.storage import db


def test_migration_discovery_ignores_macos_metadata(tmp_path, monkeypatch):
    migration = tmp_path / "0001_initial.sql"
    migration.write_text("SELECT 1;")
    (tmp_path / "._0001_initial.sql").write_bytes(b"AppleDouble metadata")
    monkeypatch.setattr(db, "MIGRATIONS_DIR", tmp_path)

    assert db._discover_migrations() == [(1, "initial", migration)]
