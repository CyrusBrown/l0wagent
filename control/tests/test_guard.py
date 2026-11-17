"""The request guard's rules as data: caller class -> reachable paths, default deny."""

import ipaddress

import pytest

from l0w_control.guard import CHAT, OTHER, SANDBOX, allowed, caller_class

SANDBOX_NET = ipaddress.ip_network("172.31.240.0/24")
CHAT_NET = ipaddress.ip_network("172.31.230.0/24")


@pytest.mark.parametrize("host,cls", [
    ("172.31.240.5", SANDBOX), ("172.31.230.9", CHAT), ("192.168.1.20", OTHER), ("127.0.0.1", OTHER), ("testclient", OTHER), (None, OTHER),
])
def test_caller_class(host, cls):
    assert caller_class(host, SANDBOX_NET, CHAT_NET) == cls


# (class, path, agent_any_address, chat_net_set) -> allowed
TABLE = [
    (SANDBOX, "/api/agent/ping", False, True, True),
    (SANDBOX, "/api/agent", False, True, True),
    (SANDBOX, "/api/meta", False, True, False),
    (SANDBOX, "/api/chat-runtime/mcp", False, True, False),
    (SANDBOX, "/mcp", False, True, False),
    (CHAT, "/api/chat-runtime/mcp", False, True, True),
    (CHAT, "/api/agent/ping", False, True, False),
    (CHAT, "/api/runs", False, True, False),
    (OTHER, "/api/runs", False, True, True),
    (OTHER, "/", False, True, True),
    (OTHER, "/mcp", False, True, True),
    (OTHER, "/api/agent/secrets/X", False, True, False),  # the token is useless off the sandbox subnet
    (OTHER, "/api/agent/secrets/X", True, True, True),  # dev_stack.py
    (OTHER, "/api/agentic", False, True, True),  # a different path, not the agent API
    (OTHER, "/api/chat-runtime/mcp", False, True, False),
    (OTHER, "/api/chat-runtime/mcp", False, False, True),  # no sidecar subnet configured (dev)
    (SANDBOX, "/api/agent/ping", True, False, True),  # the escape hatch never widens the sandbox
    (SANDBOX, "/api/runs", True, False, False),
]


@pytest.mark.parametrize("cls,path,any_addr,chat_set,ok", TABLE)
def test_rules_table(cls, path, any_addr, chat_set, ok):
    assert allowed(cls, path, agent_any_address=any_addr, chat_net_set=chat_set) is ok
