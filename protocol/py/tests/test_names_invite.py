import pytest

from l0wproto import names
from l0wproto.errors import InviteError, NameRuleError
from l0wproto.invite import format_invite, parse_invite


@pytest.mark.parametrize("n", ["mara", "a1b", "my-name", "abc", "a" * 32, "a-b-c", "123"])
def test_valid_names(n):
    assert names.validate_name(n) == n


@pytest.mark.parametrize("n", ["ab", "a" * 33, "-abc", "abc-", "a--b", "Mara", "ma_ra", "mara!", "ma ra", "mará", "", "ab\n", None])
def test_invalid_names(n):
    with pytest.raises(NameRuleError) as e:
        names.validate_name(n)
    assert e.value.code == "bad_registration"


def test_reserved():
    for n in ("admin", "api", "www", "well-known", "tunnel", "postmaster", "directory"):
        with pytest.raises(NameRuleError) as e:
            names.check_name(n)
        assert e.value.code == "name_reserved"
    with pytest.raises(NameRuleError) as e:
        names.check_name("myhub", reserved_extra=["myhub"])
    assert e.value.code == "name_reserved"
    assert len(names.RESERVED) == 22


def test_taken_and_lookalikes():
    with pytest.raises(NameRuleError) as e:
        names.check_name("mara", taken=["mara"])
    assert e.value.code == "name_taken"
    for cand, code in (("ma-ra", "name_taken"), ("m4ra", None), ("mar-a", "name_taken"), ("adm-in", "name_reserved"), ("adm1n", "name_reserved"), ("mari", None),
                       ("5tatus", "name_reserved"), ("wel-lknown", "name_reserved")):
        if code is None:
            assert names.check_name(cand, taken=["mara"]) == cand
        else:
            with pytest.raises(NameRuleError) as e:
                names.check_name(cand, taken=["mara"])
            assert e.value.code == code
    assert names.lookalike_key("a0-1b5i") == "aolbsl"
    assert names.lookalike_key("mara") == "mara"


def test_invite():
    i = parse_invite("l0w-invite:https://Hub.Example#Zk3-abc_9")
    assert (i.base_url, i.code, i.domain) == ("https://hub.example", "Zk3-abc_9", "hub.example")
    assert parse_invite("l0w-invite:https://hub.example/#x").base_url == "https://hub.example"
    assert parse_invite("l0w-invite:https://hub.example:8443#x").domain == "hub.example:8443"
    assert parse_invite(format_invite("https://hub.example/", "c")).code == "c"
    assert parse_invite("l0w-invite:http://localhost:8100#x", allow_insecure=True).domain == "localhost:8100"


@pytest.mark.parametrize("s", [
    "https://hub.example#x", "l0w-invite:https://hub.example", "l0w-invite:https://hub.example#", "l0w-invite:http://hub.example#x",
    "l0w-invite:https://hub.example/path#x", "l0w-invite:https://hub.example#a#b", "l0w-invite:#x", "l0w-invite:https://#x",
    "l0w-invite:https://hub.example#a b", "l0w-invite:https://u@hub.example#x", "l0w-invite:https://hub.example:99999#x", 5,
])
def test_bad_invites(s):
    with pytest.raises(InviteError):
        parse_invite(s)


def test_bound_names():
    # the hub's own / extra reserved names (and lookalikes) pass when bound; the fixed list and taken names never do
    assert names.check_name("myhub", reserved_extra=["myhub"], bound=True) == "myhub"
    assert names.check_name("my-hub", reserved_extra=["myhub"], bound=True) == "my-hub"
    for n, code in (("admin", "name_reserved"), ("adm1n", "name_reserved"), ("w-ww", "name_reserved"), ("myhub", "name_taken"), ("my-hub", "name_taken")):
        with pytest.raises(NameRuleError) as e:
            names.check_name(n, taken=["myhub"], reserved_extra=["myhub", "admin"], bound=True)
        assert e.value.code == code
    with pytest.raises(NameRuleError):
        names.check_name("myhub", reserved_extra=["myhub"])
    assert names.is_spec_reserved("admin") and names.is_spec_reserved("adm1n") and not names.is_spec_reserved("myhub")
