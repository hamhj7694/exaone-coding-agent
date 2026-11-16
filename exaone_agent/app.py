"""Tk desktop UI; all Tk operations occur on the main thread."""
from __future__ import annotations

import copy
import hashlib
import json
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from .agent import Agent
from .api import Cancelled, ExaoneClient
from .security import Redactor, SecretStore, atomic_write, data_dir
from .tools import Permissions, ToolRunner, restore_backup

BG = "#f5f7fa"
INK = "#172b3a"
TEAL = "#14685e"


class ReviewDialog:
    def __init__(self, parent, title, detail, decision, cancel):
        self.decision, self.cancel, self.answered = decision, cancel, False
        self.window = tk.Toplevel(parent)
        self.window.title(title)
        self.window.geometry("880x650")
        self.window.transient(parent)
        self.window.configure(background=BG)
        ttk.Label(self.window, text=title, font=("맑은 고딕", 15, "bold")).pack(anchor="w", padx=20, pady=(16, 10))
        text = ScrolledText(self.window, wrap="none", font=("Consolas", 10), background="white")
        text.pack(fill="both", expand=True, padx=20, pady=8)
        text.insert("1.0", detail)
        text.configure(state="disabled")
        buttons = ttk.Frame(self.window)
        buttons.pack(fill="x", padx=20, pady=14)
        ttk.Button(buttons, text="이번 동작 거절", command=lambda: self.finish(False)).pack(side="right", padx=6)
        ttk.Button(buttons, text="승인하고 실행", command=lambda: self.finish(True), style="Accent.TButton").pack(side="right", padx=6)
        self.window.protocol("WM_DELETE_WINDOW", lambda: self.finish(False))
        self.window.grab_set()
        self.window.after(100, self.poll)

    def finish(self, answer):
        if self.answered:
            return
        self.answered = True
        self.decision["answer"] = answer
        self.decision["event"].set()
        self.window.grab_release()
        self.window.destroy()

    def poll(self):
        if self.cancel.is_set():
            self.finish(False)
        elif not self.answered:
            self.window.after(100, self.poll)


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("EXAONE Coding Agent")
        self.root.geometry("1250x850")
        self.root.minsize(1000, 680)
        self.root.configure(background=BG)
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self.worker = None
        self.history = []
        self.api_key = ""
        self.redact = Redactor()
        self.store = SecretStore()
        self.stream_start = None
        self.stream_value = ""
        self.config_path = data_dir() / "settings.json"
        self._style()
        self._build()
        self._load_settings()
        try:
            self.api_key = self.store.load()
            self.redact.secret = self.api_key
            if self.api_key:
                self.key_var.set("●" * 20)
                self.key_status.set("저장된 암호화 키 사용 중")
        except Exception:
            self.key_status.set("저장된 키를 복호화하지 못했습니다. 다시 입력하세요.")
        self._message("안내", "API 키를 저장하고 대화를 시작하세요. 파일을 직접 다루려면 작업 폴더를 선택하고 ‘에이전트’ 모드로 전환하세요.\n"
                      "선택한 코드·도구 실행 결과는 EXAONE API로 전송됩니다. 대화는 자동 저장하지 않습니다.")
        self.root.after(50, self._drain)
        self.root.protocol("WM_DELETE_WINDOW", self._close)

    def _style(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure(".", font=("맑은 고딕", 10), background=BG, foreground=INK)
        style.configure("TButton", padding=(10, 7))
        style.configure("TCheckbutton", padding=(0, 4))
        style.configure("TLabelframe", padding=12)
        style.configure("Accent.TButton", foreground="white", background=TEAL)
        style.map("Accent.TButton", background=[("active", "#0c5149"), ("disabled", "#9db9b4")])

    def _build(self):
        header = ttk.Frame(self.root, padding=(22, 16))
        header.pack(fill="x")
        ttk.Label(header, text="EXAONE", font=("Segoe UI", 23, "bold"), foreground=TEAL).pack(side="left")
        ttk.Label(header, text="Coding Agent   /   로컬 코딩 워크스페이스", font=("맑은 고딕", 12)).pack(side="left", padx=18)
        body = ttk.Frame(self.root, padding=(16, 0, 16, 12))
        body.pack(fill="both", expand=True)
        side_shell = ttk.Frame(body, width=300)
        side_shell.pack(side="left", fill="y", padx=(0, 16))
        side_shell.pack_propagate(False)
        canvas = tk.Canvas(side_shell, background=BG, highlightthickness=0, width=280)
        scrollbar = ttk.Scrollbar(side_shell, orient="vertical", command=canvas.yview)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        canvas.configure(yscrollcommand=scrollbar.set)
        side = ttk.Frame(canvas)
        side_window = canvas.create_window((0, 0), window=side, anchor="nw")
        side.bind("<Configure>", lambda _: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(side_window, width=event.width))
        key_box = ttk.LabelFrame(side, text="API 연결")
        key_box.pack(fill="x", pady=(0, 12))
        self.key_var = tk.StringVar()
        self.key_entry = ttk.Entry(key_box, textvariable=self.key_var, show="●")
        self.key_entry.pack(fill="x", pady=(0, 8))
        key_buttons = ttk.Frame(key_box)
        key_buttons.pack(fill="x")
        self.save_key_button = ttk.Button(key_buttons, text="저장", command=self._save_key, style="Accent.TButton")
        self.save_key_button.pack(side="left")
        self.delete_key_button = ttk.Button(key_buttons, text="키 삭제", command=self._delete_key)
        self.delete_key_button.pack(side="left", padx=6)
        self.test_button = ttk.Button(key_box, text="연결 테스트", command=self._test)
        self.test_button.pack(fill="x", pady=(8, 0))
        self.key_status = tk.StringVar(value="Windows 계정 암호화 저장")
        ttk.Label(key_box, textvariable=self.key_status, wraplength=245, foreground="#536777").pack(anchor="w", pady=(6, 0))
        project_box = ttk.LabelFrame(side, text="작업 폴더")
        project_box.pack(fill="x", pady=(0, 12))
        self.workspace_var = tk.StringVar(value=str(Path.cwd()))
        self.workspace_label = ttk.Label(project_box, textvariable=self.workspace_var, wraplength=245)
        self.workspace_label.pack(fill="x", pady=(0, 8))
        self.folder_button = ttk.Button(project_box, text="폴더 선택", command=self._choose_folder)
        self.folder_button.pack(fill="x")
        controls = ttk.LabelFrame(side, text="모드 · 접근 권한")
        controls.pack(fill="x", pady=(0, 12))
        self.mode_var = tk.StringVar(value="대화")
        self.mode_box = ttk.Combobox(controls, textvariable=self.mode_var, values=["대화", "에이전트"], state="readonly")
        self.mode_box.pack(fill="x", pady=(0, 8))
        self.permission_vars = {name: tk.BooleanVar(value=name == "read") for name in ("read", "write", "shell", "desktop", "auto_write")}
        labels = {"read": "폴더 파일 읽기", "write": "파일 생성 · 편집", "shell": "터미널 명령 실행", "desktop": "컴퓨터 조작 (Windows UIA)", "auto_write": "파일 수정 승인 생략 (이번 세션)"}
        self.permission_widgets = []
        for name, variable in self.permission_vars.items():
            widget = ttk.Checkbutton(controls, text=labels[name], variable=variable)
            widget.pack(anchor="w")
            self.permission_widgets.append(widget)
        ttk.Label(controls, text="명령 실행·컴퓨터 접근은 매번 확인합니다.\n터미널·컴퓨터 조작은 폴더 밖에도 영향을 줍니다.", wraplength=245,
                  foreground="#667786", font=("맑은 고딕", 9)).pack(anchor="w", pady=(6, 0))
        settings = ttk.LabelFrame(side, text="생성 설정")
        settings.pack(fill="x", pady=(0, 12))
        ttk.Label(settings, text="추론 강도").grid(row=0, column=0, sticky="w")
        self.effort_var = tk.StringVar(value="low")
        self.effort_box = ttk.Combobox(settings, textvariable=self.effort_var, values=["low", "high", "max"], state="readonly", width=10)
        self.effort_box.grid(row=0, column=1, sticky="e", padx=8)
        ttk.Label(settings, text="최대 출력 토큰").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.tokens_var = tk.StringVar(value="8192")
        self.tokens_box = ttk.Combobox(settings, textvariable=self.tokens_var, values=["4096", "8192", "16384", "32768"], state="readonly", width=10)
        self.tokens_box.grid(row=1, column=1, sticky="e", padx=8, pady=(8, 0))
        ttk.Label(settings, text="추론 토큰도 출력 한도에 포함됩니다.", font=("맑은 고딕", 9), foreground="#667786").grid(row=2, column=0, columnspan=2, pady=(8, 0))
        self.backup_button = ttk.Button(side, text="최근 파일 변경 되돌리기", command=self._restore)
        self.backup_button.pack(fill="x", pady=(0, 8))
        self.new_button = ttk.Button(side, text="새 대화", command=self._new_chat)
        self.new_button.pack(fill="x", pady=(0, 8))
        self.export_button = ttk.Button(side, text="대화 내보내기 (.md)", command=self._export)
        self.export_button.pack(fill="x")
        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True)
        notebook = ttk.Notebook(right)
        notebook.pack(fill="both", expand=True)
        chat_frame = ttk.Frame(notebook)
        log_frame = ttk.Frame(notebook)
        notebook.add(chat_frame, text="  대화 · 코드  ")
        notebook.add(log_frame, text="  도구 실행 기록  ")
        self.chat = ScrolledText(chat_frame, wrap="word", font=("맑은 고딕", 11), padx=18, pady=16,
                                 background="white", foreground=INK, relief="flat", state="disabled")
        self.chat.pack(fill="both", expand=True)
        self.chat.tag_configure("heading", font=("맑은 고딕", 10, "bold"), foreground=TEAL, spacing1=16, spacing3=7)
        self.chat.tag_configure("body", spacing3=8)
        self.chat.tag_configure("code", font=("Consolas", 10), foreground="#283c52", background="#f1f5f8")
        self.logs = ScrolledText(log_frame, wrap="word", font=("Consolas", 10), padx=12, pady=12, state="disabled")
        self.logs.pack(fill="both", expand=True)
        self.status_var = tk.StringVar(value="준비됨")
        ttk.Label(right, textvariable=self.status_var, foreground="#5c7181").pack(anchor="w", pady=(10, 4))
        self.input = ScrolledText(right, height=5, wrap="word", font=("맑은 고딕", 11), padx=12, pady=10, relief="solid", borderwidth=1)
        self.input.pack(fill="x")
        self.input.bind("<Control-Return>", self._send_shortcut)
        actions = ttk.Frame(right)
        actions.pack(fill="x", pady=(8, 0))
        ttk.Label(actions, text="Ctrl+Enter 전송 · Enter 줄바꿈", foreground="#667786").pack(side="left")
        self.send_button = ttk.Button(actions, text="보내기", command=self._send, style="Accent.TButton")
        self.send_button.pack(side="right", padx=(8, 0))
        self.stop_button = ttk.Button(actions, text="중지", command=self._stop, state="disabled")
        self.stop_button.pack(side="right")
        self.lock_widgets = [self.key_entry, self.save_key_button, self.delete_key_button, self.test_button,
                             self.folder_button, self.mode_box, self.effort_box, self.tokens_box,
                             self.backup_button, self.new_button, self.export_button] + self.permission_widgets

    def _load_settings(self):
        try:
            settings = json.loads(self.config_path.read_text(encoding="utf-8"))
            folder = settings.get("workspace", "")
            if folder and Path(folder).is_dir():
                self.workspace_var.set(folder)
            if settings.get("effort") in {"low", "high", "max"}:
                self.effort_var.set(settings["effort"])
        except (OSError, ValueError):
            pass

    def _save_settings(self):
        atomic_write(self.config_path, json.dumps({"workspace": self.workspace_var.get(),
                     "effort": self.effort_var.get()}, ensure_ascii=False).encode("utf-8"))

    def _save_key(self):
        key = self.key_var.get().strip()
        if key == "●" * 20 and self.api_key:
            self.key_status.set("이미 암호화 저장된 키입니다.")
            return
        if not key or any(c.isspace() for c in key):
            messagebox.showerror("API 키", "공백 없는 API 키를 입력하세요.")
            return
        try:
            self.store.save(key)
        except Exception as exc:
            # No silent plaintext fallback; user may explicitly use volatile memory.
            if not messagebox.askyesno("키 저장 실패", f"암호화 저장에 실패했습니다 ({type(exc).__name__}).\n종료 시 사라지는 메모리에서만 사용할까요?"):
                return
            status = "메모리에서만 사용 중 (종료 시 삭제)"
        else:
            status = "Windows 계정 암호화 저장 완료"
        self.api_key = key
        self.redact.secret = key
        self.key_var.set("●" * 20)
        self.key_status.set(status)

    def _delete_key(self):
        try:
            self.store.delete()
        except OSError:
            messagebox.showerror("키 삭제 실패", "암호화 키 파일을 삭제하지 못했습니다.")
            return
        self.api_key = ""
        self.redact.secret = ""
        self.key_var.set("")
        self.key_status.set("키 삭제 완료")

    def _choose_folder(self):
        folder = filedialog.askdirectory(title="에이전트가 작업할 폴더 선택", initialdir=self.workspace_var.get())
        if not folder or folder == self.workspace_var.get():
            return
        if self.history and not messagebox.askyesno("작업 폴더 전환", "새 폴더로 전환하면 현재 대화를 초기화합니다. 필요한 대화는 먼저 내보내세요.\n전환할까요?"):
            return
        self.workspace_var.set(folder)
        self._new_chat(force=True)
        self._save_settings()

    def _message(self, who, body):
        self.chat.configure(state="normal")
        self.chat.insert("end", who + "\n", "heading")
        self.chat.insert("end", self.redact(body) + "\n", "body")
        self.chat.configure(state="disabled")
        self.chat.see("end")

    def _log(self, text):
        self.logs.configure(state="normal")
        self.logs.insert("end", self.redact(text) + "\n")
        self.logs.configure(state="disabled")
        self.logs.see("end")

    def _set_busy(self, busy):
        self.busy = busy
        for widget in self.lock_widgets:
            if not busy and widget in (self.mode_box, self.effort_box, self.tokens_box):
                widget.configure(state="readonly")
            else:
                widget.configure(state="disabled" if busy else "normal")
        self.send_button.configure(state="disabled" if busy else "normal")
        self.stop_button.configure(state="normal" if busy else "disabled")

    def _check_key(self):
        if not self.api_key:
            messagebox.showinfo("API 키", "먼저 API 키를 입력하고 저장하세요.")
            return False
        if self.key_var.get() != "●" * 20:
            messagebox.showinfo("API 키", "입력한 API 키의 저장 버튼을 먼저 눌러주세요.")
            return False
        return True

    def _test(self):
        if self.busy or not self._check_key():
            return
        self.cancel.clear()
        self._set_busy(True)
        self.status_var.set("연결 확인 중…")
        key = self.api_key

        def work():
            try:
                result = ExaoneClient(key).complete([{"role": "user", "content": "연결 확인입니다. OK라고만 답하세요."}],
                         [], "low", 1024, self.cancel)
                self.events.put(("info", "API 연결 성공: " + result["message"]["content"].strip()))
            except Cancelled:
                self.events.put(("info", "연결 테스트를 중지했습니다."))
            except Exception as exc:
                self.events.put(("error", self.redact(exc)))
            finally:
                self.events.put(("done", None))
        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _send_shortcut(self, _):
        self._send()
        return "break"

    def _send(self):
        if self.busy or not self._check_key():
            return
        prompt = self.input.get("1.0", "end-1c").strip()
        if not prompt:
            return
        if len(prompt) > 80_000:
            messagebox.showinfo("입력 길이", "입력이 너무 큽니다. 8만자 이내로 나누세요.")
            return
        mode = self.mode_var.get() == "에이전트"
        if mode and self.permission_vars["write"].get() and not self.permission_vars["read"].get():
            messagebox.showinfo("권한", "기존 파일을 확인하고 수정하려면 파일 읽기 권한도 켜세요.")
            return
        self.cancel.clear()
        try:
            runner = ToolRunner(Path(self.workspace_var.get()),
                                Permissions(**{k: v.get() for k, v in self.permission_vars.items()}),
                                self.cancel, self._approve_from_worker,
                                log=lambda value: self.events.put(("log", value)), redact=self.redact)
        except Exception as exc:
            messagebox.showerror("작업 폴더", self.redact(exc))
            return
        prompt = self.redact(prompt)
        self._message("나", prompt)
        self.input.delete("1.0", "end")
        self._set_busy(True)
        self._save_settings()
        agent = Agent(ExaoneClient(self.api_key), runner, self.cancel,
                      effort=self.effort_var.get(), max_tokens=int(self.tokens_var.get()),
                      on_event=lambda kind, value: self.events.put((kind, value)))
        history = copy.deepcopy(self.history)

        def work():
            try:
                agent.run(history, prompt, mode)
            except Cancelled:
                self.events.put(("info", "작업을 중지했습니다. 이미 완료된 파일 변경은 유지됩니다."))
            except Exception as exc:
                self.events.put(("error", self.redact(exc)))
            finally:
                self.events.put(("done", None))
        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _approve_from_worker(self, title, detail):
        decision = {"event": threading.Event(), "answer": False}
        self.events.put(("review", (title, detail, decision)))
        while not decision["event"].wait(0.1):
            if self.cancel.is_set():
                return False
        return decision["answer"] and not self.cancel.is_set()

    def _drain(self):
        try:
            for _ in range(150):
                kind, value = self.events.get_nowait()
                if kind == "status":
                    if not self.cancel.is_set():
                        self.status_var.set(value)
                elif kind == "stream_start":
                    self.stream_start = None
                    self.stream_value = ""
                elif kind == "text":
                    if self.stream_start is None:
                        self._message("EXAONE", "")
                        self.chat.configure(state="normal")
                        self.chat.mark_set("stream", "end-2c")
                        self.chat.mark_gravity("stream", "left")
                        self.chat.configure(state="disabled")
                        self.stream_start = True
                    self.stream_value += value
                    self.chat.configure(state="normal")
                    self.chat.delete("stream", "end-1c")
                    self.chat.insert("stream", self.redact(self.stream_value) + "\n", "body")
                    self.chat.configure(state="disabled")
                    self.chat.see("end")
                elif kind == "stream_end":
                    self._highlight_code()
                    self.stream_start = None
                elif kind == "history":
                    self.history = value
                elif kind == "tool_result":
                    result = json.dumps(value["result"], ensure_ascii=False, indent=2)
                    self._log(f"[{value['name']}]\n" + result[:36_000])
                elif kind == "log":
                    self._log(value)
                elif kind == "usage":
                    self._log("토큰 사용량: " + json.dumps(value, ensure_ascii=False))
                elif kind == "review":
                    title, detail, decision = value
                    if self.cancel.is_set():
                        decision["event"].set()
                    else:
                        self.status_var.set("동작 승인 대기 중…")
                        ReviewDialog(self.root, title, detail, decision, self.cancel)
                elif kind in ("error", "info"):
                    self.stream_start = None
                    self._message("오류" if kind == "error" else "안내", value)
                    self._log(value)
                elif kind == "done":
                    self.stream_start = None
                    self._highlight_code()
                    self._set_busy(False)
                    self.status_var.set("준비됨 · 실행 결과는 도구 기록에서 확인하세요")
        except queue.Empty:
            pass
        self.root.after(50, self._drain)

    def _highlight_code(self):
        # Preserve plain, copyable code. Fenced sections use a monospace font.
        text = self.chat.get("1.0", "end-1c")
        inside = False
        start = None
        for number, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("```"):
                if inside:
                    self.chat.tag_add("code", start, f"{number}.end")
                else:
                    start = f"{number}.0"
                inside = not inside

    def _stop(self):
        self.cancel.set()
        self.status_var.set("중지 요청됨 · 네트워크 응답 대기는 최대 90초 걸릴 수 있습니다")

    def _new_chat(self, force=False):
        if self.busy:
            return
        if not force and self.history and not messagebox.askyesno("새 대화", "현재 대화는 자동 저장되지 않습니다. 초기화할까요?"):
            return
        self.history = []
        for widget in (self.chat, self.logs):
            widget.configure(state="normal")
            widget.delete("1.0", "end")
            widget.configure(state="disabled")
        self._message("안내", "새 대화를 시작합니다. 작업 폴더: " + self.workspace_var.get())

    def _export(self):
        path = filedialog.asksaveasfilename(title="대화 저장", defaultextension=".md", filetypes=[("Markdown", "*.md")])
        if path:
            try:
                Path(path).write_text(self.redact(self.chat.get("1.0", "end-1c")), encoding="utf-8")
                self.status_var.set("대화 내보내기 완료")
            except OSError as exc:
                messagebox.showerror("저장 실패", self.redact(exc))

    def _restore(self):
        root = Path(self.workspace_var.get()).resolve()
        folder = data_dir() / "backups" / hashlib.sha256(str(root).encode()).hexdigest()[:16]
        records = sorted(folder.glob("*.json"), reverse=True)
        if not records:
            messagebox.showinfo("되돌리기", "이 작업 폴더의 변경 백업이 없습니다.")
            return
        record_path = records[0]
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
            if not messagebox.askyesno("최근 변경 되돌리기", f"{record['path']}\n변경 ID: {record['id']}\n\n이 변경을 되돌릴까요? 새로 만든 파일이면 삭제됩니다."):
                return
            restored = restore_backup(root, record_path)
            self._message("안내", f"파일 변경을 되돌렸습니다: {restored}\n다음 작업 시 파일을 다시 읽도록 요청하세요.")
        except Exception as exc:
            messagebox.showerror("복구 실패", self.redact(exc))

    def _close(self):
        if self.busy:
            self._stop()
            messagebox.showinfo("중지 요청", "진행 중인 작업을 중지하고 있습니다. 준비됨 상태가 된 뒤 닫으세요.")
            return
        if self.history and not messagebox.askyesno("종료", "대화는 자동 저장되지 않습니다. 종료할까요?"):
            return
        self.api_key = ""
        self.redact.secret = ""
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()
