import tomllib

import pytest

from l0w_ops import tomlfile
from l0w_ops.common import OpsError

SHA = "a" * 40


def test_new_file_gets_a_header_and_the_entry(tmp_path):
    p = tmp_path / ".ops.toml"
    assert tomlfile.set_entry(p, "alpha", "https://x/a.git", SHA, None) is None
    text = p.read_text()
    assert text.startswith("# Written by the ops container") and tomllib.loads(text)["extension"]["alpha"] == {"git": "https://x/a.git", "commit": SHA}


def test_replace_keeps_neighbours_and_comments(tmp_path):
    p = tmp_path / "f.toml"
    p.write_text(
        f'# top\n\n[extension.one]\ngit = "https://x/1.git"\ncommit = "{SHA}"\n# one stays\n\n'
        f'[extension.alpha]  # mine\ngit = "https://old"\ncommit = "{"b" * 40}"\nextra = 1\n\n'
        f'[extension.two]\ngit = "https://x/2.git"\ncommit = "{SHA}"\n# end\n'
    )
    old = tomlfile.set_entry(p, "alpha", "https://x/a.git", SHA, "sub/dir")
    assert "extra" in old
    text = p.read_text()
    assert "# top" in text and "# one stays" in text and "# end" in text
    got = tomllib.loads(text)["extension"]
    assert got["alpha"] == {"git": "https://x/a.git", "commit": SHA, "path": "sub/dir"} and set(got) == {"one", "alpha", "two"}


def test_quoted_name_header_is_replaced_too(tmp_path):
    p = tmp_path / "f.toml"
    p.write_text(f'[extension."alpha"]\ngit = "https://old"\ncommit = "{SHA}"\n')
    tomlfile.set_entry(p, "alpha", "https://new", SHA, None)
    assert tomllib.loads(p.read_text())["extension"]["alpha"]["git"] == "https://new" and p.read_text().count("alpha") == 1


def test_remove_entry(tmp_path):
    p = tmp_path / "f.toml"
    tomlfile.set_entry(p, "alpha", "https://x/a.git", SHA, None)
    tomlfile.set_entry(p, "beta", "https://x/b.git", SHA, None)
    assert tomlfile.remove_entry(p, "alpha") is True and tomlfile.remove_entry(p, "alpha") is False
    assert set(tomllib.loads(p.read_text())["extension"]) == {"beta"} and p.read_text().startswith("# Written")


def test_unwritable_layouts_are_refused_not_corrupted(tmp_path):
    p = tmp_path / "f.toml"
    original = f'[extension]\nalpha = {{ git = "https://x", commit = "{SHA}" }}\n'
    p.write_text(original)
    with pytest.raises(OpsError):
        tomlfile.set_entry(p, "alpha", "https://y", SHA, None)  # an inline table under [extension]: can't edit it textually
    assert p.read_text() == original
    p.write_text("this is [not toml\n")
    with pytest.raises(OpsError):
        tomlfile.set_entry(p, "alpha", "https://y", SHA, None)
    assert p.read_text() == "this is [not toml\n"


def test_lookup_prefers_the_first_file_and_ignores_bad_ones(tmp_path):
    a, b, c = tmp_path / "a.toml", tmp_path / "b.toml", tmp_path / "missing.toml"
    a.write_text(f'[extension.x]\ngit = "https://a"\ncommit = "{SHA}"\n')
    b.write_text(f'[extension.x]\ngit = "https://b"\ncommit = "{SHA}"\n[extension.y]\ngit = "https://b"\ncommit = "{SHA}"\n')
    assert tomlfile.lookup("x", a, b)["git"] == "https://a" and tomlfile.lookup("y", a, b)["git"] == "https://b" and tomlfile.lookup("x", c) is None
    a.write_text("not toml [")
    assert tomlfile.lookup("x", a, b)["git"] == "https://b"
