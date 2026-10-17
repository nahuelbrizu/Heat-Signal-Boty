"""
Acceso al almacenamiento frio (Drive) via rclone, para el archivado (`history/archive.py`) y la lectura fria
(`history/cold.py`). Corre en la VM: sin pandas, solo subprocess.

Rutas completas tipo rclone: "gdrive:archive/history/2026/08/17/bars_1m.csv.gz". `join_remote("gdrive:", ...)`
arma la ruta respetando el ":" del remoto.

Comandos usados (rclone >= 1.60, el de Debian 13):
    rclone copyto --immutable LOCAL REMOTO   sube un archivo; --immutable falla si ya existe con otro contenido
                                             (nunca pisa un archivo frio verificado)
    rclone lsjson --hash REMOTO              tamano + hashes del archivo (Drive informa md5)
    rclone copyto REMOTO LOCAL               baja un archivo

El runner (argv, timeout) -> (rc, stdout, stderr) es inyectable: los tests simulan Drive con un directorio local
(tests/archive_fakes.py) sin red ni rclone real.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

Runner = Callable[[Sequence[str], float], tuple[int, str, str]]

UPLOAD_TIMEOUT_S = 1800.0
STAT_TIMEOUT_S = 120.0
DOWNLOAD_TIMEOUT_S = 600.0


class RemoteError(Exception):
    """Fallo de rclone o de Drive (red, credenciales, archivo inexistente, verificacion)."""


@dataclass(frozen=True)
class RemoteFile:
    size: int
    md5: str | None  # None si el backend no informa md5 (se verifica bajando el archivo)


def join_remote(base: str, rel: str) -> str:
    rel = rel.lstrip("/")
    if base.endswith((":", "/")):
        return base + rel
    return f"{base}/{rel}"


def md5_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.md5()  # noqa: S324 - checksum de integridad (es el que informa Drive), no seguridad
    with open(path, "rb") as fh:
        while b := fh.read(chunk):
            h.update(b)
    return h.hexdigest()


def _subprocess_runner(argv: Sequence[str], timeout: float) -> tuple[int, str, str]:
    try:
        res = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "", f"rclone no respondio en {timeout:.0f} s"
    except OSError as exc:
        return 127, "", f"no se pudo ejecutar rclone: {exc}"
    return res.returncode, res.stdout, res.stderr


class RcloneRemote:
    def __init__(self, *, rclone: str = "rclone", runner: Runner | None = None) -> None:
        self.rclone = rclone
        self._runner = runner
        if runner is None and shutil.which(rclone) is None:
            raise RemoteError(f"rclone no esta instalado ({rclone!r} no esta en el PATH)")

    def _run(self, args: Sequence[str], timeout: float) -> tuple[int, str, str]:
        return (self._runner or _subprocess_runner)([self.rclone, *args], timeout)

    def upload(self, local: Path, remote_path: str) -> None:
        rc, _, err = self._run(["copyto", "--immutable", str(local), remote_path], UPLOAD_TIMEOUT_S)
        if rc != 0:
            raise RemoteError(f"rclone copyto {remote_path} fallo (rc={rc}): {err.strip()[-400:]}")

    def stat(self, remote_path: str) -> RemoteFile | None:
        """Tamano y md5 del archivo remoto, o None si no existe."""
        rc, out, err = self._run(["lsjson", "--hash", remote_path], STAT_TIMEOUT_S)
        if rc != 0:
            low = err.lower()
            if rc == 3 or "not found" in low or "doesn't exist" in low:
                return None
            raise RemoteError(f"rclone lsjson {remote_path} fallo (rc={rc}): {err.strip()[-400:]}")
        try:
            items = json.loads(out or "[]")
        except ValueError as exc:
            raise RemoteError(f"rclone lsjson devolvio algo ilegible: {out[:200]!r}") from exc
        name = remote_path.rsplit("/", 1)[-1].rsplit(":", 1)[-1]
        files = [i for i in items if not i.get("IsDir") and i.get("Name") == name]
        if not files:
            return None
        f = files[0]
        hashes = {k.lower(): v for k, v in (f.get("Hashes") or {}).items()}
        return RemoteFile(size=int(f.get("Size", -1)), md5=(hashes.get("md5") or None))

    def download(self, remote_path: str, local: Path) -> None:
        rc, _, err = self._run(["copyto", remote_path, str(local)], DOWNLOAD_TIMEOUT_S)
        if rc != 0:
            raise RemoteError(f"rclone copyto {remote_path} fallo (rc={rc}): {err.strip()[-400:]}")
        if not local.is_file():
            raise RemoteError(f"rclone no dejo el archivo {local} al bajar {remote_path}")


def verify_remote(remote: RcloneRemote, local: Path, remote_path: str, *, workdir: Path) -> RemoteFile:
    """Verifica que `remote_path` sea byte a byte `local`: tamano + md5 informado por el backend; si el backend no
    informa md5, se baja el archivo y se compara el md5 local. Lanza RemoteError si no coincide."""
    want_size, want_md5 = local.stat().st_size, md5_file(local)
    got = remote.stat(remote_path)
    if got is None:
        raise RemoteError(f"verificacion: {remote_path} no aparece en el remoto despues de subirlo")
    if got.size != want_size:
        raise RemoteError(f"verificacion: {remote_path} mide {got.size} B en el remoto y {want_size} B local")
    if got.md5 is None:
        probe = workdir / (local.name + ".verify")
        try:
            remote.download(remote_path, probe)
            md5 = md5_file(probe)
        finally:
            probe.unlink(missing_ok=True)
    else:
        md5 = got.md5.lower()
    if md5 != want_md5:
        raise RemoteError(f"verificacion: md5 distinto en {remote_path} (remoto {md5}, local {want_md5})")
    return RemoteFile(size=want_size, md5=want_md5)
