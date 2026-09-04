#!/usr/bin/env python3
"""Select the first unused localhost TCP port without disturbing existing services."""

import socket
import sys


def select_port(preferred: int) -> int:
    if not 1024 <= preferred <= 65535:
        raise ValueError("port must be between 1024 and 65535")
    for port in range(preferred, 65536):
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError("no localhost port is available")


if __name__ == "__main__":
    print(select_port(int(sys.argv[1])))
