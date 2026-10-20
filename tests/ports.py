#!/usr/bin/env python3
# Copyright (c) 2026 The Sequentia developers
# Distributed under the MIT software license.
"""Ports for the test rigs: free on 127.0.0.1 and below the kernel's
ephemeral range.

A port the kernel assigns (a bind to port 0) comes from the ephemeral range,
32768-60999 by default on Linux, which is also where every outgoing
connection on the machine takes its local port. A rig that picks such a port,
releases it and binds it again a moment later can find it taken by any
process's connection in between, and several sessions running suites at once
on one machine make that likely. So the rigs pick from 21000-29999, at
random so that two rigs started together do not try the same ports in the
same order, and check that the port is free.

    python3 tests/ports.py [N]      print N distinct free ports (default 1)
"""

import random
import socket
import sys

LOW, HIGH = 21000, 29999
_TAKEN = set()


def free_port():
    """A port nobody on 127.0.0.1 is bound to, between LOW and HIGH, not one
    this process handed out before."""
    for _ in range(4000):
        p = random.randint(LOW, HIGH)
        if p in _TAKEN:
            continue
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p))
            except OSError:
                continue
        _TAKEN.add(p)
        return p
    raise RuntimeError(f"no free port between {LOW} and {HIGH}")


if __name__ == "__main__":
    print(" ".join(str(free_port()) for _ in range(int(sys.argv[1]) if len(sys.argv) > 1 else 1)))
