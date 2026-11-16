"""Workspace file tools, reviewed shell execution, and opt-in desktop automation."""
from __future__ import annotations

import codecs
import difflib
import hashlib
import json
import os
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .api import Cancelled
from .desktop import DesktopTools
from .security import Redactor, atomic_write, data_dir

IGNORED = {".git", ".venv", "venv", "node_modules", "__pycache__", ".idea", "dist", "build", ".next"}
MAX_FILE_BYTES = 400_000


@dataclass(frozen=True)
class Permissions:
    read: bool = True
    write: bool = False
    shell: bool = False
    desktop: bool = False
    auto_write: bool = False


def spec(name, description, properties=None, required=None):
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties or {},
                           "required": required or [], "additionalProperties": False}}}


S = {"type": "string"}
I = {"type": "integer"}
SPECS = {
    "list_files": spec("list_files", "List workspace files. Relative path; recursive listing, ignores dependencies/secrets.", {"path": S}, []),
    "read_file": spec("read_file", "Read UTF-8 workspace text with line numbers. Max 400 lines per call.",
                      {"path": S, "start_line": I, "end_line": I}, ["path"]),
    "search_files": spec("search_files", "Literal text search in UTF-8 workspace files, max 80 matching lines.",
                         {"query": S, "path": S}, ["query"]),
    "write_file": spec("write_file", "Create/replace a UTF-8 file. Read existing file first. Changes have diff review and backup.",
                       {"path": S, "content": S}, ["path", "content"]),
    "edit_file": spec("edit_file", "Replace exactly ONE occurrence of old_text in a UTF-8 file. Read it first.",
                      {"path": S, "old_text": S, "new_text": S}, ["path", "old_text", "new_text"]),
    "run_command": spec("run_command", "Run PowerShell on Windows, sh on other OS, cwd inside workspace. NOT an OS sandbox. Explicit review always required; timeout <=120 seconds; no interactive/server commands.",
                        {"command": S, "cwd": S, "timeout": I}, ["command"]),
    "desktop_windows": spec("desktop_windows", "List visible Windows windows (title, handle). Text UI Automation, not vision."),
    "desktop_observe": spec("desktop_observe", "Read Windows UI Automation control tree; returns temporary control IDs. Never sees screenshots.",
                            {"handle": I}, ["handle"]),
    "desktop_action": spec("desktop_action", "Act on one recently observed Windows control; then observe again. Every action reviewed. keys uses pywinauto syntax: ^s=Ctrl+S, {ENTER}, %{F4}=Alt+F4. set_text replaces an Edit field with Unicode text.",
                           {"action": {"type": "string", "enum": ["click", "set_text", "keys"]},
                            "control_id": S, "text": S}, ["action", "control_id"]),
}


class ToolRunner:
    def __init__(self, root: Path, permissions: Permissions, cancel: threading.Event,
                 approve, log=lambda _: None, redact=None, storage=None):
        self.root = root.resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("작업 폴더가 아닙니다.")
        self.permissions, self.cancel = permissions, cancel
        self.approve, self.log = approve, log
        self.redact = redact or Redactor()
        self.storage = storage or data_dir()
        self.desktop = DesktopTools()
        self.read_versions = {}

    def definitions(self):
        names = []
        if self.permissions.read:
            names += ["list_files", "read_file", "search_files"]
        if self.permissions.write:
            names += ["write_file", "edit_file"]
        if self.permissions.shell:
            names += ["run_command"]
        if self.permissions.desktop:
            names += ["desktop_windows", "desktop_observe", "desktop_action"]
        return [SPECS[n] for n in names]

    @staticmethod
    def sensitive(relative: Path):
        for part in relative.parts:
            lower = part.lower()
            if (lower in {".git", ".ssh", ".aws", ".azure", "credentials", "secrets", "api-key.dpapi"}
                or lower == ".env" or lower.startswith(".env.")
                or lower.endswith((".pem", ".key", ".pfx", ".p12", ".dpapi"))):
                return True
        return False

    def path(self, value: str, *, directory=False):
        if not isinstance(value, str) or "\x00" in value:
            raise ValueError("잘못된 경로입니다.")
        raw = Path(value or ".")
        if raw.is_absolute() or raw.drive or ":" in value:
            raise ValueError("작업 폴더 기준 상대 경로만 사용하세요.")
        resolved = (self.root / raw).resolve()
        try:
            rel = resolved.relative_to(self.root)
        except ValueError:
            raise ValueError("작업 폴더 밖 경로·심볼릭 링크·junction은 허용하지 않습니다.") from None
        if self.sensitive(raw) or self.sensitive(rel):
            raise ValueError("키·환경변수·인증·Git 내부 파일은 직접 접근하지 않습니다.")
        if directory and not resolved.is_dir():
            raise ValueError("해당 폴더가 없습니다.")
        return resolved

    def _files(self, directory: Path):
        count = 0
        for parent, folders, files in os.walk(directory, followlinks=False):
            folders[:] = sorted(d for d in folders if d not in IGNORED and
                               not self.sensitive(Path(d)) and not (Path(parent) / d).is_symlink())
            for name in sorted(files):
                count += 1
                if count > 6000:
                    return
                candidate = Path(parent) / name
                try:
                    checked = self.path(candidate.relative_to(self.root).as_posix())
                    if checked.is_file():
                        yield checked
                except (ValueError, OSError):
                    continue

    @staticmethod
    def _read(path: Path):
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError("파일이 너무 큽니다 (400 KB 제한).")
        data = path.read_bytes()
        if b"\x00" in data:
            raise ValueError("바이너리 파일은 읽거나 편집할 수 없습니다.")
        return data, data.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")

    def _review(self, title, detail):
        if self.cancel.is_set():
            raise Cancelled()
        if not self.approve(title, self.redact(detail)):
            raise PermissionError("사용자가 이번 동작을 거절했습니다. 같은 동작을 우회 재시도하지 마세요.")
        if self.cancel.is_set():
            raise Cancelled()

    def execute(self, name, args):
        allowed = {s["function"]["name"] for s in self.definitions()}
        if name not in allowed:
            return {"error": "권한이 없거나 지원하지 않는 도구입니다."}
        try:
            if self.cancel.is_set():
                raise Cancelled()
            if not isinstance(args, dict):
                raise ValueError("도구 인자는 JSON 객체여야 합니다.")
            parameters = SPECS[name]["function"]["parameters"]
            if set(args) - set(parameters["properties"]) or set(parameters["required"]) - set(args):
                raise ValueError("누락되거나 지원하지 않는 도구 인자입니다.")
            for key, value in args.items():
                kind = parameters["properties"][key]["type"]
                if kind == "string" and not isinstance(value, str):
                    raise ValueError(f"{key}는 문자열이어야 합니다.")
                if kind == "integer" and type(value) is not int:
                    raise ValueError(f"{key}는 정수여야 합니다.")
            self.log(f"→ {name} " + self.redact(json.dumps({k: v for k, v in args.items()
                                                           if k not in {"content", "new_text", "old_text"}}, ensure_ascii=False))[:1200])
            result = getattr(self, name)(**args)
            safe = json.loads(self.redact(json.dumps(result, ensure_ascii=False)))
            self.log(f"← {name} 완료")
            return safe
        except Cancelled:
            raise
        except Exception as exc:
            error = self.redact(f"{type(exc).__name__}: {exc}")[:1800]
            self.log(f"← {name}: {error}")
            return {"error": error}

    def list_files(self, path="."):
        base = self.path(path, directory=True)
        result = []
        for file in self._files(base):
            result.append(file.relative_to(self.root).as_posix())
            if len(result) >= 500:
                return {"files": result, "truncated": True}
        return {"files": result, "note": "종속성·비밀 파일 제외, 최대 6000개 파일 탐색"}

    def read_file(self, path, start_line=1, end_line=250):
        file = self.path(path)
        raw, text = self._read(file)
        self.read_versions[file] = hashlib.sha256(raw).hexdigest()
        lines = text.splitlines()
        if not 1 <= start_line <= end_line or end_line - start_line >= 400:
            raise ValueError("행 범위를 확인하세요. 한 번에 최대 400행입니다.")
        return {"path": path, "total_lines": len(lines),
                "content": "\n".join(f"{i + 1}: {lines[i]}" for i in range(start_line - 1, min(end_line, len(lines))))[:35_000]}

    def search_files(self, query, path="."):
        if not query or len(query) > 300:
            raise ValueError("검색어는 1~300자로 입력하세요.")
        results = []
        for file in self._files(self.path(path, directory=True)):
            if self.cancel.is_set():
                raise Cancelled()
            try:
                _, text = self._read(file)
            except (OSError, ValueError, UnicodeError):
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if query.casefold() in line.casefold():
                    results.append({"path": file.relative_to(self.root).as_posix(),
                                    "line": number, "text": line[:350]})
                    if len(results) >= 80:
                        return {"matches": results, "truncated": True}
        return {"matches": results}

    def write_file(self, path, content):
        file = self.path(path)
        before = None
        old = ""
        if file.exists():
            before, old = self._read(file)
            if self.read_versions.get(file) != hashlib.sha256(before).hexdigest():
                raise ValueError("기존 파일을 먼저 read_file로 읽으세요. 외부 수정이 있으면 다시 읽어야 합니다.")
        if self.redact.secret and self.redact.secret in content:
            raise ValueError("API 키가 포함된 파일은 작성할 수 없습니다.")
        if "\x00" in content or len(content.encode("utf-8")) > MAX_FILE_BYTES:
            raise ValueError("UTF-8 텍스트 400 KB 이내만 작성할 수 있습니다.")
        if before is not None and old == content:
            return {"ok": True, "changed": False}
        # Preserve existing UTF-8 BOM and line-ending style.
        newline = "\r\n" if before and b"\r\n" in before else "\n"
        normalized = content.replace("\r\n", "\n").replace("\r", "\n").replace("\n", newline)
        after = (codecs.BOM_UTF8 if before and before.startswith(codecs.BOM_UTF8) else b"") + normalized.encode("utf-8")
        diff = "".join(difflib.unified_diff(old.splitlines(True), content.splitlines(True),
                                            fromfile=f"a/{path}", tofile=f"b/{path}"))
        if not diff:
            diff = f"새 파일: {path}\n" + content
        if not self.permissions.auto_write:
            self._review("파일 변경 승인", diff)
        # Recheck after user review, including resolution and concurrent edits.
        if self.path(path) != file:
            raise ValueError("승인 중 경로가 변경되었습니다.")
        if (file.read_bytes() if file.exists() else None) != before:
            raise ValueError("승인 중 파일이 변경되었습니다. 다시 읽고 수정하세요.")
        if self.cancel.is_set():
            raise Cancelled()
        backup_id = self._backup(file, before, after)
        atomic_write(file, after)
        self.read_versions[file] = hashlib.sha256(after).hexdigest()
        return {"ok": True, "path": path, "bytes": len(after), "backup_id": backup_id}

    def edit_file(self, path, old_text, new_text):
        file = self.path(path)
        _, text = self._read(file)
        if not old_text or text.count(old_text) != 1:
            raise ValueError("old_text가 정확히 한 번 일치해야 합니다. 파일을 다시 읽고 범위를 조정하세요.")
        return self.write_file(path, text.replace(old_text, new_text, 1))

    def _backup(self, file, before, after):
        token = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
        folder = self.storage / "backups" / hashlib.sha256(str(self.root).encode()).hexdigest()[:16]
        folder.mkdir(parents=True, exist_ok=True)
        if before is not None:
            atomic_write(folder / (token + ".bin"), before)
        record = {"id": token, "workspace": str(self.root), "path": file.relative_to(self.root).as_posix(),
                  "created": before is None, "after_sha256": hashlib.sha256(after).hexdigest()}
        atomic_write(folder / (token + ".json"), json.dumps(record, ensure_ascii=False).encode())
        return token

    def run_command(self, command, cwd=".", timeout=60):
        folder = self.path(cwd, directory=True)
        if not command.strip() or len(command) > 12_000 or not 1 <= timeout <= 120:
            raise ValueError("명령과 제한 시간(1~120초)을 확인하세요.")
        self._review("터미널 명령 실행 승인",
                     f"작업 폴더: {folder}\n제한 시간: {timeout}초\n\n{command}\n\n"
                     "터미널은 이 폴더 밖 파일과 네트워크에도 접근할 수 있습니다. OS 격리 기능은 아닙니다.")
        env = {k: v for k, v in os.environ.items() if not any(word in k.upper() for word in
               ("API_KEY", "TOKEN", "SECRET", "PASSWORD"))}
        env["PYTHONIOENCODING"] = "utf-8"
        if os.name == "nt":
            prefix = "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); $OutputEncoding = [Console]::OutputEncoding; "
            executable = ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", prefix + command]
            flags = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
        else:
            executable = ["/bin/sh", "-c", command]
            flags = {"start_new_session": True}
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen(executable, cwd=folder, env=env, stdin=subprocess.DEVNULL,
                                       stdout=output, stderr=subprocess.STDOUT, **flags)
            start = time.monotonic()
            timed_out = False
            try:
                while process.poll() is None:
                    if self.cancel.wait(0.1):
                        raise Cancelled()
                    if time.monotonic() - start > timeout or os.fstat(output.fileno()).st_size > 8_000_000:
                        timed_out = True
                        break
            finally:
                # Always terminate descendants too; background servers are not supported.
                if os.name == "nt":
                    if process.poll() is None:
                        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                       capture_output=True, timeout=10,
                                       creationflags=subprocess.CREATE_NO_WINDOW)
                else:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            size = os.fstat(output.fileno()).st_size
            output.seek(0)
            raw = output.read(32_000)
            text = raw.decode("utf-8", errors="replace")
        return {"exit_code": process.returncode, "output": text, "truncated": size > 32_000,
                "timed_out_or_output_limit": timed_out}

    def desktop_windows(self):
        self._review("컴퓨터 창 목록 읽기", "열려 있는 다른 프로그램의 창 제목을 읽어 EXAONE API에 전송합니다.")
        return self.desktop.windows()

    def desktop_observe(self, handle):
        self._review("컴퓨터 화면 정보 읽기", f"창 핸들 {handle}의 버튼·텍스트 정보를 읽어 API에 전송합니다. 비밀번호 필드는 제외합니다.")
        return self.desktop.observe(handle)

    def desktop_action(self, action, control_id, text=""):
        self._review("컴퓨터 조작 승인", f"동작: {action}\n컨트롤 ID: {control_id}\n입력: {text}\n\n승인 후 관찰한 대상 창으로 전환됩니다.")
        return self.desktop.action(action, control_id, text)


def restore_backup(root: Path, record_path: Path):
    """User-invoked restore; do not clobber changes made since the agent's edit."""
    root = root.resolve(strict=True)
    record = json.loads(record_path.read_text(encoding="utf-8"))
    if record["workspace"] != str(root):
        raise ValueError("다른 작업 폴더의 백업입니다.")
    file = (root / record["path"]).resolve()
    file.relative_to(root)
    if not file.exists() or hashlib.sha256(file.read_bytes()).hexdigest() != record["after_sha256"]:
        raise ValueError("이후 변경이 있어 자동 복구할 수 없습니다. 백업 폴더에서 원본을 확인하세요.")
    if record["created"]:
        file.unlink()
    else:
        atomic_write(file, record_path.with_suffix(".bin").read_bytes())
    record_path.rename(record_path.with_suffix(".restored"))
    return record["path"]
