import json
import os
import stat
import tomllib

import pytest
from conftest import HUB, MANIFEST, OTHER, TOKEN, git, install_body, post, service_doc

from l0w_ops import gitx
from l0w_ops.common import OpsError
from l0w_ops.extensions import SOURCE_FILE

SUBDIR = {"extension.toml": MANIFEST, "main.py": "x = 1\n"}


def installed_dir(settings):
    return settings.extensions_dir / "plantlog"


def test_owner_install_extracts_records_and_writes_the_toml(client, settings, repo):
    r = post(client, "/v0/extensions/install", install_body(repo))
    assert r.status_code == 200, r.text
    assert r.json()["replaced"] is False
    d = installed_dir(settings)
    assert (d / "main.py").read_text() == "print('hi')\n" and (d / "web" / "ui.js").is_file()
    assert not (d / ".git").exists()
    src = json.loads((d / SOURCE_FILE).read_text())
    assert src["git"] == repo.url and src["commit"] == repo.sha and src["source"] == "owner" and src["path"] is None and src["installed_at"]
    entry = tomllib.loads(settings.ops_toml.read_text())["extension"]["plantlog"]
    assert entry == {"git": repo.url, "commit": repo.sha}
    assert stat.S_IMODE(d.stat().st_mode) & 0o005 == 0o005  # control, another user, can read it
    st = client.get("/v0/status", headers={"Authorization": f"Bearer {TOKEN}"}).json()
    assert [e["name"] for e in st["extensions"]] == ["plantlog"] and st["extensions"][0]["source"] == "owner"
    assert not [p for p in settings.extensions_dir.iterdir() if p.name.startswith((".tmp-", ".old-"))]


def test_install_keeps_other_toml_entries_and_comments(client, settings, repo):
    settings.ops_toml.write_text('# mine\n[extension.other]\ngit = "https://x/y.git"\ncommit = "' + "a" * 40 + '"\n\n# trailing\n')
    assert post(client, "/v0/extensions/install", install_body(repo)).status_code == 200
    text = settings.ops_toml.read_text()
    assert "# mine" in text and "# trailing" in text and "[extension.other]" in text
    entries = tomllib.loads(text)["extension"]
    assert set(entries) == {"other", "plantlog"}
    # a second install replaces the entry rather than adding another
    sha2 = repo.commit({"extension.toml": MANIFEST, "main.py": "v2\n"})
    assert post(client, "/v0/extensions/install", install_body(repo, commit=sha2)).json()["replaced"] is True
    entries = tomllib.loads(settings.ops_toml.read_text())["extension"]
    assert entries["plantlog"]["commit"] == sha2 and entries["other"]["commit"] == "a" * 40 and settings.ops_toml.read_text().count("[extension.plantlog]") == 1
    assert (installed_dir(settings) / "main.py").read_text() == "v2\n"


def test_replace_is_atomic_when_the_new_tree_is_refused(client, settings, repo):
    post(client, "/v0/extensions/install", install_body(repo))
    bad = repo.commit({"extension.toml": MANIFEST, "main.py": "x"})
    # a symlink in the new commit: refused, and the installed extension and the toml entry stay as they were
    (repo.path / "evil").symlink_to("/etc/passwd")
    git(repo.path, "add", "-A")
    git(repo.path, "commit", "-q", "-m", "link")
    bad = git(repo.path, "rev-parse", "HEAD")
    r = post(client, "/v0/extensions/install", install_body(repo, commit=bad))
    assert r.status_code == 422 and r.json()["error"] == "unsafe_archive"
    assert (installed_dir(settings) / "main.py").read_text() == "print('hi')\n"
    assert json.loads((installed_dir(settings) / SOURCE_FILE).read_text())["commit"] == repo.sha
    assert tomllib.loads(settings.ops_toml.read_text())["extension"]["plantlog"]["commit"] == repo.sha
    assert not [p for p in settings.extensions_dir.iterdir() if p.name.startswith((".tmp-", ".old-"))]


def test_symlink_in_a_fresh_install_is_refused_and_leaves_nothing(client, settings, repo):
    (repo.path / "evil").symlink_to("../../etc")
    git(repo.path, "add", "-A")
    git(repo.path, "commit", "-q", "-m", "link")
    sha = git(repo.path, "rev-parse", "HEAD")
    r = post(client, "/v0/extensions/install", install_body(repo, commit=sha))
    assert r.status_code == 422 and "symlink" in r.json()["detail"]
    assert not installed_dir(settings).exists() and not settings.ops_toml.exists()


def test_commit_must_be_the_full_id_and_exist(client, repo):
    assert post(client, "/v0/extensions/install", install_body(repo, commit=repo.sha[:12])).json()["error"] == "bad_commit"
    assert post(client, "/v0/extensions/install", install_body(repo, commit=repo.sha.upper())).json()["error"] == "bad_commit"
    r = post(client, "/v0/extensions/install", install_body(repo, commit="0" * 40))
    assert r.status_code in (422, 502)


def test_path_installs_a_subfolder(client, settings, repo):
    sha = repo.commit({"README": "top\n", "ext/plantlog/extension.toml": MANIFEST, "ext/plantlog/main.py": "sub\n"})
    r = post(client, "/v0/extensions/install", install_body(repo, commit=sha, path="ext/plantlog"))
    assert r.status_code == 200, r.text
    assert (installed_dir(settings) / "main.py").read_text() == "sub\n" and not (installed_dir(settings) / "README").exists()
    assert tomllib.loads(settings.ops_toml.read_text())["extension"]["plantlog"]["path"] == "ext/plantlog"
    r = post(client, "/v0/extensions/install", install_body(repo, commit=sha, path="nope"))
    assert r.status_code == 422 and r.json()["error"] == "path_not_found"


@pytest.mark.parametrize("path", ["../x", "/abs", "a/../b", "a//b", "-x", "a\\b", "./a"])
def test_bad_paths(client, repo, path):
    assert post(client, "/v0/extensions/install", install_body(repo, path=path)).json()["error"] == "bad_path"


@pytest.mark.parametrize("name", ["Bad", "a", "plantlog\n", "../x", "x" * 40, "has_underscore", "1abc"])
def test_bad_names(client, repo, name):
    assert post(client, "/v0/extensions/install", install_body(repo, name=name)).json()["error"] == "bad_name"


def test_manifest_must_exist_and_match_the_name(client, settings, repo):
    sha = repo.commit({"main.py": "x"})
    assert post(client, "/v0/extensions/install", install_body(repo, commit=sha)).json()["error"] == "no_manifest"
    sha = repo.commit({"extension.toml": 'name = "other"\n'})
    assert post(client, "/v0/extensions/install", install_body(repo, commit=sha)).json()["error"] == "name_mismatch"
    assert not installed_dir(settings).exists() and not settings.ops_toml.exists()


def test_wont_replace_what_it_didnt_install(client, settings, repo):
    installed_dir(settings).mkdir()
    (installed_dir(settings) / "mine.txt").write_text("keep")
    r = post(client, "/v0/extensions/install", install_body(repo))
    assert r.status_code == 409 and r.json()["error"] == "not_ours"
    assert (installed_dir(settings) / "mine.txt").read_text() == "keep"
    # nor through a symlink
    other = settings.extensions_dir.parent / "elsewhere"
    other.mkdir()
    (other / SOURCE_FILE).write_text("{}")
    installed_dir(settings).rename(settings.extensions_dir / "gone")
    installed_dir(settings).symlink_to(other)
    assert post(client, "/v0/extensions/install", install_body(repo)).json()["error"] == "not_ours"
    assert post(client, "/v0/extensions/remove", {"name": "plantlog"}).json()["error"] == "not_ours"
    assert other.exists()


# ── source: toml ──────────────────────────────────────────────────────────────


def test_toml_source_must_match_the_owners_file(client, settings, repo):
    body = install_body(repo, source="toml")
    r = post(client, "/v0/extensions/install", body)
    assert r.status_code == 403 and r.json()["error"] == "not_in_toml"
    settings.toml_file.write_text(f'[extension.plantlog]\ngit = "{repo.url}"\ncommit = "{repo.sha}"\n')
    assert post(client, "/v0/extensions/install", body).status_code == 200
    assert not settings.ops_toml.exists()  # the owner's file is the record; ops writes nothing
    other = repo.commit({"extension.toml": MANIFEST, "main.py": "other\n"})
    r = post(client, "/v0/extensions/install", install_body(repo, source="toml", commit=other))
    assert r.status_code == 403 and r.json()["error"] == "toml_mismatch"
    r = post(client, "/v0/extensions/install", install_body(repo, source="toml", path="sub"))
    assert r.json()["error"] == "toml_mismatch"
    assert (installed_dir(settings) / "main.py").read_text() == "print('hi')\n"


# ── source: hub ───────────────────────────────────────────────────────────────

HTTPS = "https://git.example/plants.git"


@pytest.fixture
def hub_repo(repo, settings):
    """An https URL that git maps onto the local repository (git's own url.insteadOf, test only)."""
    settings.git_env = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": f"url.file://{repo.path}.insteadOf", "GIT_CONFIG_VALUE_0": HTTPS}
    return repo


def hub_body(repo, ref=None, signer=HUB, **kw):
    ref = ref or {"git": HTTPS, "commit": repo.sha, "path": "."}
    return {"name": "plantlog", "git": HTTPS, "commit": repo.sha, "source": "hub", "service_doc": service_doc(ref, signer), "hub_key": HUB.key.to_dict(), **kw}


def test_hub_install_checks_the_signed_document(client, settings, hub_repo):
    r = post(client, "/v0/extensions/install", hub_body(hub_repo))
    assert r.status_code == 200, r.text
    assert json.loads((installed_dir(settings) / SOURCE_FILE).read_text())["source"] == "hub"
    assert not settings.ops_toml.exists()


def test_hub_forged_document_is_refused(client, settings, hub_repo):
    r = post(client, "/v0/extensions/install", hub_body(hub_repo, signer=OTHER))
    assert r.status_code == 403 and r.json()["error"] == "bad_service_doc"
    doc = service_doc({"git": HTTPS, "commit": hub_repo.sha, "path": "."})
    doc["payload"] = service_doc({"git": HTTPS, "commit": "f" * 40, "path": "."})["payload"]  # a different payload under the real signature
    r = post(client, "/v0/extensions/install", hub_body(hub_repo, service_doc=doc))
    assert r.status_code == 403 and r.json()["error"] == "bad_service_doc"
    assert not installed_dir(settings).exists()


def test_hub_document_must_match_git_commit_and_path(client, settings, hub_repo):
    for ref, over in [({"git": HTTPS, "commit": "f" * 40, "path": "."}, {}), ({"git": "https://git.example/other.git", "commit": hub_repo.sha, "path": "."}, {}),
                      ({"git": HTTPS, "commit": hub_repo.sha, "path": "ext"}, {}), ({"git": HTTPS, "commit": hub_repo.sha, "path": "."}, {"path": "ext"})]:
        r = post(client, "/v0/extensions/install", hub_body(hub_repo, ref, **over))
        assert r.status_code == 403 and r.json()["error"] == "doc_mismatch", (ref, over)
    r = post(client, "/v0/extensions/install", hub_body(hub_repo) | {"service_doc": service_doc(None)})
    assert r.json()["error"] == "no_extension"
    assert post(client, "/v0/extensions/install", {**hub_body(hub_repo), "hub_key": None}).json()["error"] == "bad_request"
    assert not installed_dir(settings).exists()


def test_hub_key_must_be_a_signing_key(client, hub_repo):
    from l0wproto import keys
    seal = keys.from_seed(b"s" * 32, "seal").key.to_dict()
    assert post(client, "/v0/extensions/install", {**hub_body(hub_repo), "hub_key": seal}).json()["error"] == "bad_service_doc"
    assert post(client, "/v0/extensions/install", {**hub_body(hub_repo), "hub_key": {"kid": "x"}}).json()["error"] == "bad_service_doc"


# ── remove and resolve ────────────────────────────────────────────────────────


def test_remove_only_what_ops_installed(client, settings, repo):
    mine = settings.extensions_dir / "mine"
    mine.mkdir()
    (mine / "x").write_text("1")
    assert post(client, "/v0/extensions/remove", {"name": "mine"}).json()["error"] == "not_ours"
    assert mine.exists()
    assert post(client, "/v0/extensions/remove", {"name": "nothing"}).status_code == 409

    post(client, "/v0/extensions/install", install_body(repo))
    r = post(client, "/v0/extensions/remove", {"name": "plantlog"})
    assert r.status_code == 200 and r.json()["toml_entry_removed"] is True
    assert not installed_dir(settings).exists()
    assert "plantlog" not in (tomllib.loads(settings.ops_toml.read_text()).get("extension") or {})


def test_remove_of_a_toml_install_leaves_the_owners_file(client, settings, repo):
    settings.toml_file.write_text(f'[extension.plantlog]\ngit = "{repo.url}"\ncommit = "{repo.sha}"\n')
    post(client, "/v0/extensions/install", install_body(repo, source="toml"))
    r = post(client, "/v0/extensions/remove", {"name": "plantlog"}).json()
    assert r["removed"] is True and r["still_in_extensions_toml"] is True
    assert "plantlog" in settings.toml_file.read_text()


def test_resolve_returns_the_commit_a_ref_points_at(client, repo):
    sha2 = repo.commit({"extension.toml": MANIFEST, "n": "2"})
    git(repo.path, "tag", "-a", "v1", "-m", "v1")
    assert post(client, "/v0/extensions/resolve", {"git": repo.url}).json() == {"commit": sha2}
    assert post(client, "/v0/extensions/resolve", {"git": repo.url, "ref": "main"}).json() == {"commit": sha2}
    assert post(client, "/v0/extensions/resolve", {"git": repo.url, "ref": "v1"}).json() == {"commit": sha2}  # the tag object resolves to its commit
    assert post(client, "/v0/extensions/resolve", {"git": repo.url, "ref": "nope"}).status_code == 404
    for ref in ("--upload-pack=x", "-x", "a..b", "a b"):
        assert post(client, "/v0/extensions/resolve", {"git": repo.url, "ref": ref}).json()["error"] == "bad_ref"


# ── URLs ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("url", ["http://git.example/x.git", "git://git.example/x.git", "ssh://git@git.example/x", "https://u:p@git.example/x.git", "-x", "--upload-pack=touch /x",
                                 "https://git.example/x .git", "file:///etc", "", None, 5])
def test_production_url_rules(url):
    from l0w_ops.config import Settings
    s = Settings(token="t", allow_file_git=False, allow_private_hosts=True)
    with pytest.raises(OpsError) as e:
        gitx.check_url(url, s)
    assert e.value.code == "bad_git_url"


def test_private_hosts_are_refused_unless_allowed(monkeypatch):
    from l0w_ops.config import Settings
    monkeypatch.setattr(gitx, "resolve", lambda host, port: {"git.example": ["93.184.216.34"], "lan.example": ["192.168.1.5"], "both.example": ["93.184.216.34", "10.0.0.2"],
                                                              "lo.example": ["::1"], "meta.example": ["169.254.169.254"], "v6map.example": ["::ffff:10.0.0.1"], "gone.example": []}[host])
    s = Settings(token="t")
    assert gitx.check_url("https://git.example/x.git", s)
    for h in ("lan.example", "both.example", "lo.example", "meta.example", "v6map.example", "gone.example"):
        with pytest.raises(OpsError):
            gitx.check_url(f"https://{h}/x.git", s)
    assert gitx.check_url("https://lan.example/x.git", Settings(token="t", allow_private_hosts=True))


def test_file_urls_need_the_test_flag(client, repo, settings):
    settings.allow_file_git = False
    assert post(client, "/v0/extensions/resolve", {"git": repo.url}).json()["error"] == "bad_git_url"


def test_installs_are_serialized(client, settings, repo):
    # two installs of different extensions through the same lock both finish and leave consistent state
    sha2 = repo.commit({"extension.toml": 'name = "second"\nversion = "1"\nsdk = "1.0"\n'})
    assert post(client, "/v0/extensions/install", install_body(repo)).status_code == 200
    assert post(client, "/v0/extensions/install", install_body(repo, name="second", commit=sha2)).status_code == 200
    assert set(tomllib.loads(settings.ops_toml.read_text())["extension"]) == {"plantlog", "second"}
    assert os.listdir(settings.extensions_dir / "second")
