"""Text-based Windows UI Automation. No image or screenshot is sent to EXAONE."""
from __future__ import annotations

import os
import time


class DesktopTools:
    def __init__(self):
        self._desktop = None
        self._com = None
        self._controls = {}
        self._window = None
        self._observed_at = 0.0

    @staticmethod
    def _is_password(info):
        # pywinauto 0.6.9 has no element_info.is_password property.
        # Query the underlying IUIAutomationElement; unknown => do not expose it.
        try:
            return bool(info.element.CurrentIsPassword)
        except Exception:
            return True

    def _get(self):
        if os.name != "nt":
            raise RuntimeError("컴퓨터 조작은 Windows에서만 지원합니다.")
        if self._desktop is None:
            try:
                import pythoncom
                pythoncom.CoInitialize()
                self._com = pythoncom
                from pywinauto import Desktop
                self._desktop = Desktop(backend="uia")
            except ImportError:
                raise RuntimeError("컴퓨터 조작 모듈이 없습니다. setup_windows.bat를 실행하세요.") from None
        return self._desktop

    def close(self):
        self._controls.clear()
        self._window = None
        self._desktop = None
        if self._com:
            self._com.CoUninitialize()
            self._com = None

    def windows(self):
        items = []
        for window in self._get().windows():
            try:
                if window.process_id() == os.getpid() or not window.is_visible():
                    continue
                items.append({"handle": window.handle, "title": window.window_text()[:200],
                              "process_id": window.process_id()})
            except Exception:
                continue
        return {"windows": items[:60], "note": "조작할 창을 desktop_observe로 먼저 관찰하세요."}

    def observe(self, handle: int):
        window = self._get().window(handle=int(handle)).wrapper_object()
        if window.process_id() == os.getpid():
            raise ValueError("에이전트 자신의 창은 조작할 수 없습니다.")
        self._controls.clear()
        self._window = window
        # Bounded traversal avoids enumerating huge UI trees in a browser/IDE.
        pending = [(window, 0)]
        items = []
        while pending and len(items) < 200:
            control, depth = pending.pop(0)
            try:
                info = control.element_info
                if self._is_password(info):
                    continue
                node_id = str(len(items))
                self._controls[node_id] = control
                items.append({"id": node_id, "depth": depth, "type": info.control_type,
                              "name": (info.name or "")[:240], "enabled": control.is_enabled()})
                if depth < 6:
                    pending.extend((child, depth + 1) for child in control.children()[:60])
            except Exception:
                continue
        self._observed_at = time.monotonic()
        return {"handle": int(handle), "controls": items,
                "note": "ID는 이 관찰에만 유효. 화면 변경 후 다시 관찰. 비밀번호 필드 제외. 화면 이미지는 보지 못함."}

    def action(self, action: str, control_id: str, text: str = ""):
        if time.monotonic() - self._observed_at > 120:
            raise ValueError("관찰이 만료됐습니다. desktop_observe를 다시 호출하세요.")
        control = self._controls.get(str(control_id))
        if control is None:
            raise ValueError("유효한 control_id가 아닙니다. 창을 먼저 관찰하세요.")
        if self._is_password(control.element_info):
            raise ValueError("비밀번호 필드는 조작하지 않습니다.")
        if self._window.process_id() == os.getpid():
            raise ValueError("자기 창 조작은 허용되지 않습니다.")
        if not control.is_enabled() or not control.is_visible():
            raise ValueError("현재 조작할 수 없는 컨트롤입니다. 다시 관찰하세요.")
        self._window.set_focus()
        if action == "click":
            control.click_input()
        elif action == "set_text":
            # UIA value pattern supports Unicode without clipboard exposure.
            control.set_edit_text(text)
        elif action == "keys":
            if len(text) > 200:
                raise ValueError("키 조합은 200자 이하여야 합니다.")
            control.type_keys(text, with_spaces=True, set_foreground=True)
        else:
            raise ValueError("지원하지 않는 데스크톱 동작입니다.")
        self._controls.clear()  # Never act on stale IDs twice.
        return {"ok": True, "note": "동작을 요청했습니다. desktop_observe로 실제 결과를 확인하세요."}
