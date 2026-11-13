"""PROTOTYPE helper: type a line into a running winpe_worker VM through QMP, then screendump.

Usage: python3 qmp_type.py WORK_DIR 'text' OUT.ppm [WAIT_SECONDS]
(WORK_DIR holds qmp.sock; useful to poke at a WinPE prompt while debugging a job.)
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import sys
import time
from typing import Any

KEYS = {
    " ": "spc", "\\": "backslash", ":": "shift-semicolon", ".": "dot", "&": "shift-7",
    ">": "shift-dot", "-": "minus", "/": "slash", "_": "shift-minus", "\n": "ret",
    "%": "shift-5", '"': "shift-apostrophe", "*": "shift-8",
}  # fmt: skip


def sendkeys(text: str) -> list[str]:
    """QEMU sendkey names for each character (US layout)."""
    out = []
    for ch in text:
        if ch in KEYS:
            out.append(KEYS[ch])
        elif ch.isascii() and ch.isalpha():
            out.append(f"shift-{ch.lower()}" if ch.isupper() else ch)
        elif ch.isascii() and ch.isdigit():
            out.append(ch)
        else:
            raise ValueError(f"no key for {ch!r}")
    return out


def main(argv: list[str]) -> int:
    if len(argv) not in (3, 4):
        print(__doc__, file=sys.stderr)
        return 2
    keys = sendkeys(argv[1])
    os.chdir(argv[0])
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(5)
        s.connect("qmp.sock")
        s.recv(65536)

        def cmd(c: dict[str, Any]) -> None:
            s.sendall(json.dumps(c).encode() + b"\n")
            time.sleep(0.05)
            with contextlib.suppress(TimeoutError):
                s.recv(65536)

        cmd({"execute": "qmp_capabilities"})
        for k in keys:
            cmd({"execute": "human-monitor-command", "arguments": {"command-line": f"sendkey {k}"}})
        time.sleep(float(argv[3]) if len(argv) == 4 else 3)
        cmd({"execute": "screendump", "arguments": {"filename": argv[2]}})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
