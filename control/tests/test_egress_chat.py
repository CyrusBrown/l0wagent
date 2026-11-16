"""Egress: the chat-runtime sidecar's subnet reaches only the model host."""

import asyncio
import importlib.util
import os
from pathlib import Path


def _egress(monkeypatch, tmp_path):
    monkeypatch.setenv("CHAT_SUBNET", "172.31.241.0/24")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    spec = importlib.util.spec_from_file_location("egress_under_test", Path(__file__).parents[2] / "egress" / "egress.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def test_chat_subnet_is_limited_to_the_model_host(monkeypatch, tmp_path):
    eg = _egress(monkeypatch, tmp_path)
    assert eg.from_chat("172.31.241.7") and not eg.from_chat("172.31.240.7") and not eg.from_chat("?")
    for host, port in (("example.com", 443), ("api.anthropic.com", 80), ("control", 8000), ("169.254.169.254", 443)):
        allowed, reason, _, _ = asyncio.run(eg.decide_chat(host, port))
        assert not allowed and reason


def test_no_chat_subnet_means_no_chat_clients(monkeypatch, tmp_path):
    monkeypatch.delenv("CHAT_SUBNET", raising=False)
    os.environ.pop("CHAT_SUBNET", None)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    spec = importlib.util.spec_from_file_location("egress_plain", Path(__file__).parents[2] / "egress" / "egress.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    assert not mod.from_chat("172.31.241.7")
