from importlib.resources import files
from pathlib import Path


def test_packaged_schema_matches_documented_migration() -> None:
    packaged = files("replayscope").joinpath("schema.sql").read_text()
    documented = (Path(__file__).parents[1] / "sql" / "001_initial.sql").read_text()
    assert packaged == documented
