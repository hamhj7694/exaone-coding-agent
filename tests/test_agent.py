import codecs
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace

from exaone_agent.agent import Agent
from exaone_agent.api import APIError, Cancelled, ExaoneClient
from exaone_agent.security import Redactor, SecretStore
from exaone_agent.tools import Permissions, ToolRunner, restore_backup
from exaone_agent.desktop import DesktopTools


def events_stream(*events, done=True):
    data = b"".join(b"data: " + json.dumps(event).encode() + b"\r\n\r\n" for event in events)
    if done:
        data += b"data: [DONE]\r\n\r\n"
    return io.BytesIO(data)


def delta(value=None, finish=None):
    return {"choices": [{"delta": value or {}, "finish_reason": finish}]}


class TransportTests(unittest.TestCase):
    def complete(self, stream, tools=None):
        captured = {}

        def opener(request, timeout):
            captured.update(payload=json.loads(request.data), auth=request.get_header("Authorization"))
            return stream
        client = ExaoneClient("fake-private-key", opener=opener)
        result = client.complete([{"role": "user", "content": "안녕"}], tools or [], "low", 8192, threading.Event())
        return result, captured

    def test_korean_stream_and_usage(self):
        result, sent = self.complete(events_stream(delta({"reasoning_content": "reason"}),
                            delta({"content": "안녕"}), delta({"content": "하세요"}, "stop"),
                            {"choices": [], "usage": {"total_tokens": 50}}))
        self.assertEqual(result["message"]["content"], "안녕하세요")
        self.assertEqual(result["message"]["reasoning_content"], "reason")
        self.assertEqual(result["usage"]["total_tokens"], 50)
        self.assertEqual(sent["auth"], "Bearer fake-private-key")
        self.assertEqual(sent["payload"]["model"], "k-exaone_v2.5")
        self.assertTrue(sent["payload"]["stream"])
        self.assertNotIn("tools", sent["payload"])

    def test_fragmented_tool_arguments(self):
        stream = events_stream(
            delta({"tool_calls": [{"index": 0, "id": "call-1", "function": {"name": "read_file", "arguments": '{"pa'}}]}),
            delta({"tool_calls": [{"index": 0, "function": {"arguments": 'th":"hello.py"}'}}]}, "tool_calls"))
        result, sent = self.complete(stream, [{"type": "function", "function": {"name": "read_file"}}])
        call = result["message"]["tool_calls"][0]
        self.assertEqual(json.loads(call["function"]["arguments"]), {"path": "hello.py"})
        self.assertEqual(sent["payload"]["tool_choice"], "auto")

    def test_truncated_stream_never_becomes_tool_call(self):
        stream = events_stream(delta({"tool_calls": [{"index": 0, "id": "x", "function": {"name": "write_file", "arguments": "{"}}]}), done=False)
        with self.assertRaises(APIError):
            self.complete(stream)

    def test_output_limit_not_executed(self):
        with self.assertRaises(APIError):
            self.complete(events_stream(delta({"content": "partial"}, "length")))

    def test_missing_or_duplicate_tool_ids_rejected(self):
        calls = [{"index": i, "id": "same", "function": {"name": "read_file", "arguments": "{}"}} for i in range(2)]
        with self.assertRaises(APIError):
            self.complete(events_stream(delta({"tool_calls": calls}, "tool_calls")))

    def test_http_error_hides_raw_body(self):
        def opener(*_, **__):
            raise urllib.error.HTTPError("https://api.exaone.ai", 401, "Unauthorized", {}, io.BytesIO(b"fake-private-key"))
        with self.assertRaises(APIError) as error:
            ExaoneClient("fake-private-key", opener).complete([], [], "low", 100, threading.Event())
        self.assertIn("401", str(error.exception))
        self.assertNotIn("fake-private-key", str(error.exception))

    def test_cancellation_before_request(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(Cancelled):
            ExaoneClient("fake").complete([], [], "low", 100, cancel)


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()
        self.storage = Path(self.temp.name) / "appdata"
        self.reviews = []
        self.cancel = threading.Event()
        self.runner = ToolRunner(self.root, Permissions(read=True, write=True, shell=True), self.cancel,
                                 lambda title, detail: self.reviews.append((title, detail)) or True,
                                 redact=Redactor("fake-secret-key"), storage=self.storage)

    def write(self, file="a.py", content="print('hello')\n"):
        return self.runner.execute("write_file", {"path": file, "content": content})

    def test_create_review_edit_backup_restore(self):
        result = self.write()
        self.assertTrue(result["ok"])
        self.runner.execute("read_file", {"path": "a.py"})
        edit = self.runner.execute("edit_file", {"path": "a.py", "old_text": "hello", "new_text": "world"})
        self.assertTrue(edit["ok"])
        self.assertEqual(len(self.reviews), 2)
        self.assertIn("-print('hello')", self.reviews[1][1])
        record = next(self.storage.rglob(edit["backup_id"] + ".json"))
        restore_backup(self.root, record)
        self.assertEqual((self.root / "a.py").read_text(), "print('hello')\n")
        created_record = next(self.storage.rglob(result["backup_id"] + ".json"))
        restore_backup(self.root, created_record)
        self.assertFalse((self.root / "a.py").exists())

    def test_existing_file_requires_read(self):
        (self.root / "a.py").write_text("old")
        self.assertIn("error", self.write(content="new"))
        self.assertEqual((self.root / "a.py").read_text(), "old")

    def test_concurrent_edit_during_review_not_overwritten(self):
        self.write()
        self.runner.execute("read_file", {"path": "a.py"})

        def review(*_):
            (self.root / "a.py").write_text("external edit")
            return True
        self.runner.approve = review
        self.assertIn("error", self.write(content="new"))
        self.assertEqual((self.root / "a.py").read_text(), "external edit")

    def test_ambiguous_edit_refused(self):
        self.write(content="a a")
        result = self.runner.execute("edit_file", {"path": "a.py", "old_text": "a", "new_text": "b"})
        self.assertIn("error", result)

    def test_traversal_absolute_ads_and_credentials_blocked(self):
        for path in ("../outside.py", str(self.root / "a.py"), ".env", ".git/config", "a.txt:stream", "id.key"):
            with self.subTest(path=path):
                self.assertIn("error", self.write(file=path))

    @unittest.skipIf(os.name == "nt", "Windows symlink privileges vary")
    def test_symlink_escape_blocked(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (self.root / "link").symlink_to(outside, target_is_directory=True)
        self.assertIn("error", self.write(file="link/a.py"))
        self.assertFalse((outside / "a.py").exists())

    def test_read_only_permissions_enforced(self):
        self.runner.permissions = Permissions()
        self.assertIn("error", self.write())
        self.assertNotIn("write_file", {x["function"]["name"] for x in self.runner.definitions()})

    def test_declined_write_has_no_side_effect(self):
        self.runner.approve = lambda *_: False
        self.assertIn("error", self.write())
        self.assertFalse((self.root / "a.py").exists())

    def test_no_plaintext_secret_written(self):
        self.assertIn("error", self.write(content="fake-secret-key"))
        self.assertFalse((self.root / "a.py").exists())

    def test_bom_and_crlf_preserved_with_multiline_edit(self):
        raw = codecs.BOM_UTF8 + b"first\r\nsecond\r\n"
        (self.root / "a.py").write_bytes(raw)
        self.runner.execute("read_file", {"path": "a.py"})
        result = self.runner.execute("edit_file", {"path": "a.py", "old_text": "first\nsecond", "new_text": "A\nB"})
        self.assertTrue(result["ok"])
        self.assertEqual((self.root / "a.py").read_bytes(), codecs.BOM_UTF8 + b"A\r\nB\r\n")

    def test_restore_refuses_to_clobber_later_change(self):
        result = self.write()
        record = next(self.storage.rglob(result["backup_id"] + ".json"))
        (self.root / "a.py").write_text("new user edit")
        with self.assertRaises(ValueError):
            restore_backup(self.root, record)

    def test_literal_search_and_hidden_secret(self):
        self.write(content="hello world\n")
        (self.root / ".env").write_text("hello secret")
        result = self.runner.execute("search_files", {"query": "hello"})
        self.assertEqual(len(result["matches"]), 1)
        self.assertEqual(result["matches"][0]["path"], "a.py")

    def test_shell_real_execution_and_redaction(self):
        command = "Write-Output 'fake-secret-key'" if os.name == "nt" else "printf 'fake-secret-key'"
        result = self.runner.execute("run_command", {"command": command})
        self.assertEqual(result["exit_code"], 0)
        self.assertNotIn("fake-secret-key", result["output"])
        self.assertIn("REDACTED", result["output"])

    def test_shell_timeout(self):
        command = "Start-Sleep -Seconds 10" if os.name == "nt" else "sleep 10"
        result = self.runner.execute("run_command", {"command": command, "timeout": 1})
        self.assertTrue(result["timed_out_or_output_limit"])

    def test_cancel_running_shell(self):
        command = "Start-Sleep -Seconds 10" if os.name == "nt" else "sleep 10"
        timer = threading.Timer(0.25, self.cancel.set)
        timer.start()
        try:
            with self.assertRaises(Cancelled):
                self.runner.execute("run_command", {"command": command})
        finally:
            timer.cancel()

    def test_arguments_validated(self):
        for args in ({"path": 5}, {"path": "a", "extra": "x"}, {}):
            self.assertIn("error", self.runner.execute("read_file", args))


class AgentLoopTests(unittest.TestCase):
    def test_tool_result_goes_back_to_api(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "a.py").write_text("hello")
            requests = []

            def opener(request, **_):
                body = json.loads(request.data)
                requests.append(body)
                if len(requests) == 1:
                    return events_stream(delta({"tool_calls": [{"index": 0, "id": "x", "function": {
                        "name": "read_file", "arguments": '{"path":"a.py"}'}}]}, "tool_calls"))
                self.assertEqual(body["messages"][-1]["role"], "tool")
                self.assertIn("hello", body["messages"][-1]["content"])
                return events_stream(delta({"content": "파일 확인 완료"}, "stop"))
            stop = threading.Event()
            runner = ToolRunner(root, Permissions(), stop, lambda *_: True, storage=root / "store")
            messages = Agent(ExaoneClient("fake", opener), runner, stop).run([], "읽어줘", True)
            self.assertEqual(len(requests), 2)
            self.assertEqual(messages[-1]["content"], "파일 확인 완료")

    def test_chat_mode_never_executes_tools(self):
        with tempfile.TemporaryDirectory() as folder:
            stop = threading.Event()
            runner = ToolRunner(Path(folder), Permissions(write=True), stop, lambda *_: True, storage=Path(folder))

            class MaliciousClient:
                def complete(self, *_, **__):
                    return {"message": {"role": "assistant", "tool_calls": [
                        {"id": "x", "function": {"name": "write_file", "arguments": '{"path":"a.py","content":"x"}'}}]}}
            with self.assertRaises(APIError):
                Agent(MaliciousClient(), runner, stop).run([], "hello", False)
            self.assertFalse((Path(folder) / "a.py").exists())

    def test_cancellation_keeps_protocol_valid(self):
        with tempfile.TemporaryDirectory() as folder:
            stop = threading.Event()
            observed = []
            runner = ToolRunner(Path(folder), Permissions(), stop, lambda *_: True, storage=Path(folder))

            class Client:
                def complete(self, *_, **__):
                    stop.set()
                    return {"message": {"role": "assistant", "tool_calls": [
                        {"id": "x", "function": {"name": "list_files", "arguments": "{}"}},
                        {"id": "y", "function": {"name": "list_files", "arguments": "{}"}}]}}
            with self.assertRaises(Cancelled):
                Agent(Client(), runner, stop, on_event=lambda k, v: observed.append((k, v))).run([], "test", True)
            history = next(value for kind, value in observed if kind == "history")
            self.assertEqual([m.get("tool_call_id") for m in history[-2:]], ["x", "y"])


class SecretTests(unittest.TestCase):
    def test_redaction(self):
        redact = Redactor("my-key-123")
        self.assertNotIn("my-key-123", redact("my-key-123 and Bearer abcdef"))
        self.assertNotIn("abcdef", redact("Bearer abcdef"))

    @unittest.skipUnless(os.name == "nt", "DPAPI requires Windows")
    def test_windows_encrypted_roundtrip(self):
        with tempfile.TemporaryDirectory() as folder:
            store = SecretStore(Path(folder))
            store.save("test-secret-never-plaintext")
            self.assertNotIn(b"test-secret", store.path.read_bytes())
            self.assertEqual(store.load(), "test-secret-never-plaintext")
            store.delete()
            self.assertEqual(store.load(), "")

    @unittest.skipIf(os.name == "nt", "Non-Windows fail-closed check")
    def test_non_windows_never_stores_plaintext(self):
        with tempfile.TemporaryDirectory() as folder:
            store = SecretStore(Path(folder))
            with self.assertRaises(RuntimeError):
                store.save("secret")
            self.assertFalse(store.path.exists())


class DesktopTests(unittest.TestCase):
    class Control:
        def __init__(self, name, password=False, children=None):
            self.element_info = SimpleNamespace(name=name, control_type="Edit",
                                               element=SimpleNamespace(CurrentIsPassword=password))
            self._children = children or []
            self.clicked = False
            self.text = None

        def children(self):
            return self._children

        def is_enabled(self):
            return True

        def is_visible(self):
            return True

        def process_id(self):
            return os.getpid() + 999

        def set_focus(self):
            pass

        def click_input(self):
            self.clicked = True

        def set_edit_text(self, text):
            self.text = text

    def make(self):
        password = self.Control("DO NOT READ SECRET", password=True)
        edit = self.Control("Input")
        window = self.Control("Window", children=[password, edit])
        desktop = DesktopTools()
        desktop._get = lambda: SimpleNamespace(window=lambda **_: SimpleNamespace(wrapper_object=lambda: window))
        return desktop, window, password, edit

    def test_password_controls_never_exposed(self):
        desktop, window, password, edit = self.make()
        observed = desktop.observe(123)
        self.assertNotIn("DO NOT READ SECRET", json.dumps(observed))
        self.assertEqual(len(observed["controls"]), 2)

    def test_missing_password_metadata_fails_closed(self):
        self.assertTrue(DesktopTools._is_password(SimpleNamespace()))

    def test_action_requires_fresh_observation_and_invalidates_ids(self):
        desktop, window, password, edit = self.make()
        desktop.observe(123)
        desktop.action("set_text", "1", "안녕하세요")
        self.assertEqual(edit.text, "안녕하세요")
        with self.assertRaises(ValueError):
            desktop.action("click", "1")
        desktop.observe(123)
        desktop._observed_at -= 121
        with self.assertRaises(ValueError):
            desktop.action("click", "1")


if __name__ == "__main__":
    unittest.main()
