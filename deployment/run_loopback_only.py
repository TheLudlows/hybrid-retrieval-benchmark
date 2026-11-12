"""Prepare loopback in an isolated Linux network namespace and run as the caller."""

import errno
import fcntl
import os
import socket
import struct
from pathlib import Path
import sys


def main():
    uid, gid, *command = sys.argv[1:]
    if os.getuid() != 0 or not command:
        raise SystemExit("Invoke with unshare --user --map-root-user --net")
    interfaces = Path("/proc/net/dev").read_text().splitlines()[2:]
    if {line.split(":", 1)[0].strip() for line in interfaces} != {"lo"}:
        raise SystemExit("Expected a network namespace containing only loopback")
    with socket.socket() as server:
        request = struct.pack("16sh22x", b"lo", 0)
        flags = struct.unpack_from("h", fcntl.ioctl(server, 0x8913, request), 16)[0]
        fcntl.ioctl(server, 0x8914, struct.pack("16sh22x", b"lo", flags | 1))
        server.bind(("127.0.0.1", 0))
        server.listen()
        with socket.socket() as client:
            client.connect(server.getsockname())
        with socket.socket() as probe:
            if probe.connect_ex(("192.0.2.1", 443)) != errno.ENETUNREACH:
                raise SystemExit("Expected non-loopback egress to be unreachable")
    print("Verified loopback-only network namespace", flush=True)
    os.execvp("unshare", ["unshare", "--user", "--map-user=" + uid, "--map-group=" + gid, *command])


if __name__ == "__main__":
    main()
