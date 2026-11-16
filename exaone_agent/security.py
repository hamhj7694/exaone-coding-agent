"""Secret redaction and Windows user-scoped DPAPI storage (no plaintext fallback)."""
from __future__ import annotations

import ctypes
import os
import re
import tempfile
from pathlib import Path


def data_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share"))
    path = base / "EXAONECodingAgent"
    path.mkdir(parents=True, exist_ok=True)
    return path


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".exaone-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class Redactor:
    def __init__(self, secret: str = ""):
        self.secret = secret

    def __call__(self, value: object) -> str:
        text = str(value)
        if self.secret:
            text = text.replace(self.secret, "[API KEY REDACTED]")
        text = re.sub(r"(?i)(Bearer\s+)[^\s\"']+", r"\1[REDACTED]", text)
        return text


class SecretStore:
    def __init__(self, directory: Path | None = None):
        self.path = (directory or data_dir()) / "api-key.dpapi"

    @staticmethod
    def _transform(data: bytes, decrypt: bool = False) -> bytes:
        if os.name != "nt":
            raise RuntimeError("암호화 키 저장은 Windows에서 지원됩니다. 현재 실행에서는 메모리만 사용하세요.")
        from ctypes import wintypes

        class Blob(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

        buffer = ctypes.create_string_buffer(data)
        source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
        output = Blob()
        crypt = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        if decrypt:
            function = crypt.CryptUnprotectData
            function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                                 ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
            args = (ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output))
        else:
            function = crypt.CryptProtectData
            function.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.c_void_p,
                                 ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
            args = (ctypes.byref(source), "EXAONE Coding Agent API key", None, None, None, 1, ctypes.byref(output))
        function.restype = wintypes.BOOL
        if not function(*args):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            ctypes.memset(output.pbData, 0, output.cbData)
            kernel.LocalFree(ctypes.cast(output.pbData, ctypes.c_void_p))

    def save(self, key: str) -> None:
        if not key.strip():
            raise ValueError("API 키를 입력하세요.")
        atomic_write(self.path, self._transform(key.strip().encode("utf-8")))

    def load(self) -> str:
        return self._transform(self.path.read_bytes(), decrypt=True).decode("utf-8") if self.path.exists() else ""

    def delete(self) -> None:
        self.path.unlink(missing_ok=True)
