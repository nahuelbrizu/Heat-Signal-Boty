"""Drive falso para los tests del almacenamiento por niveles: un directorio local que hace de `gdrive:` y un runner
que imita los comandos de rclone que usa history/remote.py (copyto [--immutable], lsjson --hash). Sin red."""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Sequence
from pathlib import Path

from heatsignal.history.remote import RcloneRemote


class FakeRclone:
    def __init__(self, root: Path, *, remote: str = "gdrive:") -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.remote = remote
        self.fail: set[str] = set()     # "upload" | "download" | "stat": esa operacion falla (red caida)
        self.corrupt_upload = False     # sube con un byte cambiado (la verificacion tiene que detectarlo)
        self.no_md5 = False             # backend que no informa md5 (verificacion bajando el archivo)
        self.calls: list[list[str]] = []

    def _local(self, p: str) -> Path | None:
        return self.root / p[len(self.remote):] if p.startswith(self.remote) else None

    def __call__(self, argv: Sequence[str], timeout: float) -> tuple[int, str, str]:
        argv = list(argv)
        self.calls.append(argv)
        cmd, args = argv[1], [a for a in argv[2:] if not a.startswith("--")]
        if cmd == "copyto":
            src, dst = args
            if self._local(src) is not None:  # bajar
                if "download" in self.fail:
                    return 1, "", "Failed to copy: couldn't connect to Google Drive"
                s = self._local(src)
                if not s.is_file():
                    return 3, "", "directory not found"
                shutil.copyfile(s, dst)
                return 0, "", ""
            if "upload" in self.fail:
                return 1, "", "Failed to copy: googleapi: Error 503"
            d = self._local(dst)
            data = Path(src).read_bytes()
            if self.corrupt_upload:
                data = data[:-1] + bytes([data[-1] ^ 0xFF])
            if "--immutable" in argv and d.is_file() and d.read_bytes() != data:
                return 1, "", "Source and destination exist but do not match: immutable file modified"
            d.parent.mkdir(parents=True, exist_ok=True)
            d.write_bytes(data)
            return 0, "", ""
        if cmd == "lsjson":
            if "stat" in self.fail:
                return 1, "", "couldn't list: network unreachable"
            p = self._local(args[0])
            if p is None or not p.is_file():
                return 3, "", "error listing: directory not found"
            data = p.read_bytes()
            hashes = {} if self.no_md5 else {"md5": hashlib.md5(data).hexdigest()}  # noqa: S324
            return 0, json.dumps([{"Path": p.name, "Name": p.name, "Size": len(data), "IsDir": False,
                                   "Hashes": hashes}]), ""
        return 1, "", f"comando no simulado: {cmd}"

    def files(self) -> list[str]:
        return sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_file())


def fake_remote(root: Path) -> tuple[RcloneRemote, FakeRclone]:
    fake = FakeRclone(root)
    return RcloneRemote(runner=fake), fake
