import pytest

from l0wproto import keys
from l0wproto.times import parse_time

IAT = "2026-11-17T21:30:00Z"
NOW = parse_time(IAT)
HUB = "hub.example"
ADDR = f"mara@{HUB}"


@pytest.fixture(scope="session")
def kp():
    mk = lambda label, use: keys.from_seed(__import__("hashlib").sha256(label.encode()).digest(), use, IAT)  # noqa: E731
    return {"sign": mk("a", "sign"), "seal": mk("b", "seal"), "recover": mk("c", "recover"), "sign2": mk("d", "sign"),
            "hub": mk("e", "sign"), "other": mk("f", "sign")}
