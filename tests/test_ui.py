"""Display-dependent smoke tests; fake API only, no account key required."""
import os
import tempfile
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

from exaone_agent.app import App, ReviewDialog


@unittest.skipUnless(os.name == "nt" or os.environ.get("DISPLAY"), "GUI display required")
class UITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.environment.start()
        self.root = tk.Tk()
        self.app = App(self.root)
        self.app.workspace_var.set(self.temp.name)
        self.app.api_key = "test-only-fake-key"
        self.app.redact.secret = self.app.api_key
        self.app.key_var.set("●" * 20)
        self.root.update()

    def tearDown(self):
        self.app.cancel.set()
        if self.app.worker:
            self.app.worker.join(timeout=2)
        for event in self.root.tk.call("after", "info"):
            self.root.after_cancel(event)
        self.root.destroy()
        self.environment.stop()
        self.temp.cleanup()

    def pump(self):
        deadline = time.monotonic() + 5
        while self.app.busy and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.root.update()
        self.assertFalse(self.app.busy)

    def test_chat_streaming_and_history(self):
        class FakeClient:
            def __init__(self, _):
                pass

            def complete(self, messages, tools, effort, max_tokens, cancel, on_text, on_status):
                self_tools = tools
                assert not self_tools
                on_text("안녕하세요.\n")
                on_text("```python\nprint('hello')\n```\n")
                return {"message": {"role": "assistant", "content": "안녕하세요.\n```python\nprint('hello')\n```\n"}, "usage": {"total_tokens": 20}}
        with patch("exaone_agent.app.ExaoneClient", FakeClient):
            self.app.input.insert("1.0", "파이썬 코드 예시")
            self.app._send()
            self.pump()
        visible = self.app.chat.get("1.0", "end-1c")
        self.assertEqual(visible.count("안녕하세요."), 1)
        self.assertIn("print('hello')", visible)
        self.assertEqual(self.app.history[-1]["role"], "assistant")
        self.assertTrue(self.app.chat.tag_ranges("code"))
        self.assertEqual(str(self.app.send_button["state"]), "normal")

    def test_agent_edits_real_temp_file_through_review(self):
        class FakeClient:
            def __init__(self, _):
                self.calls = 0

            def complete(self, messages, tools, *args, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    return {"message": {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function",
                            "function": {"name": "write_file", "arguments": '{"path":"hello.py","content":"print(42)"}'}}]}}
                return {"message": {"role": "assistant", "content": "파일 생성 완료"}}

        def review(parent, title, detail, decision, cancel):
            self.assertIn("hello.py", detail)
            decision.update(answer=True)
            decision["event"].set()
        with patch("exaone_agent.app.ExaoneClient", FakeClient), patch("exaone_agent.app.ReviewDialog", review):
            self.app.mode_var.set("에이전트")
            self.app.permission_vars["write"].set(True)
            self.app.input.insert("1.0", "hello.py 만들어줘")
            self.app._send()
            self.pump()
        self.assertEqual((Path(self.temp.name) / "hello.py").read_text(), "print(42)")
        self.assertIn("파일 생성 완료", self.app.history[-1]["content"])

    def test_password_mask_and_key_storage_callback(self):
        self.app.key_var.set("new-fake-key")
        with patch.object(self.app.store, "save") as save:
            self.app._save_key()
        save.assert_called_once_with("new-fake-key")
        self.assertEqual(self.app.key_entry.cget("show"), "●")
        self.assertEqual(self.app.key_var.get(), "●" * 20)
        self.assertEqual(self.app.api_key, "new-fake-key")

    def test_review_modal_approve_and_cancel(self):
        decision = {"event": threading.Event(), "answer": False}
        dialog = ReviewDialog(self.root, "테스트 승인", "+print(42)", decision, threading.Event())
        self.root.update()
        dialog.finish(True)
        self.assertTrue(decision["event"].is_set())
        self.assertTrue(decision["answer"])
        cancel = threading.Event()
        decision = {"event": threading.Event(), "answer": False}
        dialog = ReviewDialog(self.root, "중지 테스트", "+print(42)", decision, cancel)
        cancel.set()
        dialog.poll()
        self.assertTrue(decision["event"].is_set())
        self.assertFalse(decision["answer"])


if __name__ == "__main__":
    unittest.main()
