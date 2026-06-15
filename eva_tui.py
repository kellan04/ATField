"""
EVA TUI — 终端图形化界面（薄前端）
通过子进程启动 eva.py --tui 作为后端，通过 JSON 消息通信。
"""

from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
import sys
from io import StringIO
from pathlib import Path
from threading import Lock, Thread
from typing import Callable

from rich.console import Console
from rich.markdown import Markdown as RichMarkdown
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import ScrollableContainer
from textual.widgets import Input, Static  # noqa: F401

# ============================================================================
# 工具函数
# ============================================================================

MAX_RESULT_LEN = 500

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def strip_ansi(text: str) -> str:
    """去除 ANSI 转义码"""
    return ANSI_RE.sub("", text)


# ============================================================================
# EvaBackend — 子进程管理
# ============================================================================

EVA_DIR = Path(__file__).resolve().parent
EVA_SCRIPT = EVA_DIR / "eva.py"


class EvaBackend:
    """管理 eva.py 子进程通信"""

    def __init__(self, debug: bool = False, allow_all_cli: bool = False):
        self._debug = debug
        (EVA_DIR / ".eva").mkdir(exist_ok=True)
        self._debug_file = EVA_DIR / ".eva" / "debug.log"

        args = [sys.executable, str(EVA_SCRIPT), "--tui"]
        if debug:
            args.append("--debug")
        if allow_all_cli:
            args.append("-a")

        self.proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=str(EVA_DIR),
        )

    def _log(self, direction: str, msg: dict) -> None:
        if not self._debug:
            return
        ts = dt.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        line = f"[{ts}] FRONTEND {direction} {json.dumps(msg, ensure_ascii=False)[:200]}\n"
        with self._debug_file.open("a", encoding="utf-8") as f:
            f.write(line)

    def send(self, msg: dict) -> None:
        self._log("OUT", msg)
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def _reader_loop(self, on_message: Callable[[dict], None]) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            try:
                msg = json.loads(line.strip())
            except json.JSONDecodeError:
                self._log("IN", {"error": "json decode failed", "raw": line[:100]})
                continue
            try:
                self._log("IN", msg)
                on_message(msg)
            except Exception as e:
                self._log("IN", {"error": str(e)})

    def start_reader(self, on_message: Callable[[dict], None]) -> None:
        t = Thread(target=self._reader_loop, args=(on_message,), daemon=True)
        t.start()

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=1.0)


# ============================================================================
# Textual TUI 应用
# ============================================================================
# Design Tokens
# ============================================================================

ROLE_COLORS: dict[str, str] = {
    "user": "#7ee8fa",
    "assistant": "#e8d5b7",
    "tool": "#b8a9c9",
    "system": "#90EE90",
    "thinking": "#555555",
}

ROLE_ICONS: dict[str, str] = {
    "user": "👤",
    "assistant": "🤖",
    "tool": "🔧",
    "system": "⚙️",
    "thinking": "💭",
}

ROLE_LABELS: dict[str, str] = {
    "user": "You",
    "assistant": "EVA",
}

PLACEHOLDER_TEXT: str = "输入你的问题，按 Enter 发送..."
INIT_MESSAGE: str = "你好，介绍一下你自己"


class EVATUI(App):
    """EVA 终端图形化界面（薄前端）"""

    CSS = """
    Screen {
        background: #0f0f1a;
    }

    #conv_scroll {
        dock: top;
        width: 100%;
        height: 100%;
        background: #0f0f1a;
        scrollbar-size: 1 1;
        scrollbar-color: #7ee8fa #1a1a2e;
        padding: 1;
    }

    #input_area {
        dock: bottom;
        height: auto;
        min-height: 3;
        background: #1a1a2e;
        border-top: tall solid #7ee8fa;
    }

    Input {
        dock: bottom;
        height: 3;
        background: #1a1a2e;
        color: #e8d5b7;
        border-top: tall solid #7ee8fa;
    }

    Static {
        color: #e8d5b7;
    }

    .header {
        dock: top;
        height: 2;
        background: #1a1a2e;
        content-align: center middle;
        color: #7ee8fa;
        text-style: bold;
    }

    .msg-assistant {
        color: #e8d5b7;
        margin: 1 0;
    }

    .msg-user {
        color: #7ee8fa;
        margin: 1 0;
    }

    .msg-tool {
        color: #b8a9c9;
        margin: 1 0;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit", show=False),
        Binding("ctrl+l", "clear_conv", "Clear", show=False),
        Binding("ctrl+d", "toggle_debug", "Debug", show=False),
    ]

    def __init__(
        self,
        debug: bool = False,
        allow_all_cli: bool = False,
        auto_greet: bool = True,
    ):
        super().__init__()
        self._debug = debug
        self._allow_all_cli = allow_all_cli
        self._auto_greet = auto_greet
        self.backend = EvaBackend(debug=debug, allow_all_cli=allow_all_cli)
        self._thinking_buf: list[str] = []
        self._content_buf: list[str] = []
        self._buf_lock = Lock()
        self._thinking_widget: Static | None = None
        self._tool_widget: Static | None = None
        self._md_io: StringIO = StringIO()
        self._console: Console | None = None
        self._session_id: str = ""
        self._resumed: bool = False
        self._seen_msg_ids: set[str] = set()

    def compose(self) -> ComposeResult:
        header = "EVA TUI  |  Backend: eva.py --tui"
        if self._debug:
            header += "  [DEBUG]"
        yield Static(header, classes="header", id="header_bar")
        yield ScrollableContainer(id="conv_scroll")
        yield Input(placeholder=PLACEHOLDER_TEXT, id="user_input")

    def on_mount(self) -> None:
        self.backend.start_reader(self._on_backend_message)
        self.backend.send({"type": "init", "allow_all_cli": self._allow_all_cli})

    def _on_backend_message(self, msg: dict) -> None:
        """处理后端发来的所有 JSON 消息"""
        msg_type = msg.get("type")

        if msg_type == "ready":
            self._session_id = msg.get("session_id", "")
            self._resumed = bool(msg.get("resumed", False))
            self._refresh_header()
            if self._auto_greet:
                self.backend.send({"type": "user_message", "content": INIT_MESSAGE})

        elif msg_type == "event":
            if not self._mark_seen(msg.get("message_id", "")):
                return
            event = msg.get("event")
            if event == "thinking":
                raw = msg.get("data", "")
                with self._buf_lock:
                    self._thinking_buf.append(strip_ansi(raw))
                    full = "".join(self._thinking_buf)
                self.call_from_thread(self._show_thinking, full[:80])
            elif event == "content":
                self._finalize_thinking()
                with self._buf_lock:
                    self._content_buf.append(msg.get("data", ""))
                self._schedule_content_flush()
            elif event == "tool_start":
                with self._buf_lock:
                    self._thinking_buf.clear()
                self._finalize_thinking()
                name = msg.get("name", "?")
                args = msg.get("args", {})
                self.call_from_thread(self._append_tool_start, name, args)
            elif event == "tool_result":
                with self._buf_lock:
                    self._thinking_buf.clear()
                tool_id = msg.get("id", "")
                result = msg.get("result", "")
                self.call_from_thread(self._append_tool_result, tool_id, result)
            elif event == "compact_panic":
                self.call_from_thread(self._append_system, "⚠️ 记忆压缩触发")

        elif msg_type == "tool_call":
            # tool_start 已通知，等待 tool_result 即可
            pass

        elif msg_type == "response":
            self.call_from_thread(self._finalize_response, msg)

        elif msg_type == "session_saved":
            pass  # save 成功，无需 UI 反馈

    def _truncate_at_word_boundary(self, text: str, limit: int = MAX_RESULT_LEN) -> str:
        """在单词边界截断文本，避免硬截断打断单词"""
        if len(text) <= limit:
            return text
        truncated = text[:limit]
        last_space = truncated.rfind(" ")
        if last_space > limit * 0.7:
            return truncated[:last_space]
        return truncated

    def _render_md(self, text: str) -> Text:
        """将 Markdown 文本渲染为 Rich Text（width 跟随终端）"""
        clean = strip_ansi(text)
        size = getattr(self, "size", None)
        w = size.width if size else 80
        console = getattr(self, "_console", None)
        if console is None or console.width != w:
            self._md_io = StringIO()
            console = Console(
                file=self._md_io, width=max(40, w - 4), force_terminal=False
            )
            self._console = console
        else:
            self._md_io.truncate(0)
            self._md_io.seek(0)
        console.print(RichMarkdown(clean))
        return Text.from_ansi(self._md_io.getvalue())

    def on_resize(self) -> None:
        self._console = None

    def _append_conv(self, role: str, body: str) -> None:
        """向对话区追加一条消息（label/icon/color 跟随 role）"""
        scroll = self.query_one("#conv_scroll", ScrollableContainer)
        color = ROLE_COLORS.get(role, ROLE_COLORS["assistant"])
        icon = ROLE_ICONS.get(role, ROLE_ICONS["assistant"])
        label_text = ROLE_LABELS.get(role, role.title())
        label = Text(f"{icon} {label_text}\n", style=f"bold {color}")
        content = self._render_md(body)
        bubble = Static(
            label + content,
            classes=f"msg-{role}",
        )
        scroll.mount(bubble)
        scroll.scroll_end(animate=False)

    def _mark_seen(self, msg_id: str) -> bool:
        """return True if message should be rendered (id unseen or empty)"""
        if not msg_id:
            return True
        if msg_id in self._seen_msg_ids:
            return False
        self._seen_msg_ids.add(msg_id)
        return True

    def _refresh_header(self) -> None:
        """刷新 header 文本（DEBUG + resumed 指示）"""
        parts = ["EVA TUI  |  Backend: eva.py --tui"]
        if getattr(self, "_debug", False):
            parts.append("DEBUG")
        session_id = getattr(self, "_session_id", "") or ""
        if getattr(self, "_resumed", False) and session_id:
            parts.append(f"resumed:{session_id[:8]}")
        header = "  |  ".join(parts)
        try:
            self.query_one("#header_bar", Static).update(header)
        except Exception:
            pass

    def _append_user(self, text: str) -> None:
        scroll = self.query_one("#conv_scroll", ScrollableContainer)
        item = Static(
            Text(f"👤 You\n{text}", style=f"bold {ROLE_COLORS.get('user', '#7ee8fa')}"),
            classes="msg-user",
        )
        scroll.mount(item)
        scroll.scroll_end(animate=False)

    def _append_tool_start(self, name: str, args: dict) -> None:
        """显示工具开始执行"""
        # 清理 thinking 进度条
        if self._thinking_widget:
            self._thinking_widget.remove()
            self._thinking_widget = None
        scroll = self.query_one("#conv_scroll", ScrollableContainer)
        args_str = "\n".join(f"{k}: {v}" for k, v in args.items())
        # 显示命令及运行中指示
        self._tool_widget = Static(
            Text(f"🔧 {name}\n{args_str}\n⏳ 执行中...", style=f"bold {ROLE_COLORS.get('tool', '#b8a9c9')}"),
            classes="msg-tool",
        )
        scroll.mount(self._tool_widget)
        scroll.scroll_end(animate=False)

    def _append_tool_result(self, tool_id: str, result: str) -> None:
        """显示工具执行结果（替换运行中指示）"""
        scroll = self.query_one("#conv_scroll", ScrollableContainer)
        # 移除运行中 widget（不删 thinking_widget，等 finalize 再删）
        if self._tool_widget:
            self._tool_widget.remove()
            self._tool_widget = None
        # 渲染工具结果（先清理 ANSI，再截断）
        clean = strip_ansi(result)
        display = self._truncate_at_word_boundary(clean) + ("... 省略" if len(clean) > MAX_RESULT_LEN else "")
        content = self._render_md(display)
        label = Text(f"🔧 工具结果 [{tool_id[:12]}...]\n", style=f"bold {ROLE_COLORS.get('tool', '#b8a9c9')}")
        scroll.mount(Static(
            label + content,
            classes="msg-tool",
        ))
        scroll.scroll_end(animate=False)

    def _append_system(self, text: str) -> None:
        scroll = self.query_one("#conv_scroll", ScrollableContainer)
        scroll.mount(Static(
            Text(text, style=f"bold {ROLE_COLORS.get('system', '#90EE90')}"),
        ))
        scroll.scroll_end(animate=False)

    def _show_thinking(self, text: str) -> None:
        """显示 thinking 进度条"""
        scroll = self.query_one("#conv_scroll", ScrollableContainer)
        if self._thinking_widget:
            self._thinking_widget.remove()
        color = ROLE_COLORS.get("thinking", "#555555")
        self._thinking_widget = Static(
            Text(f"💭 thinking: {text[:80]}", style=f"dim {color}"),
            classes="msg-thinking",
        )
        scroll.mount(self._thinking_widget)
        scroll.scroll_end(animate=False)

    def _finalize_thinking(self) -> None:
        """thinking 结束，移除进度条"""
        if self._thinking_widget:
            self._thinking_widget.remove()
            self._thinking_widget = None

    def _schedule_content_flush(self) -> None:
        """节流：50ms 内的 content 批量渲染一次。Textual set_timer 按 name dedupe。"""
        try:
            self.set_timer(0.05, self._flush_content, name="content_flush")
        except RuntimeError:
            # 无 event loop（test 上下文）— 同步 flush
            self._flush_content()

    def _flush_content(self) -> None:
        """批量渲染累积的 content buffer"""
        with self._buf_lock:
            if not self._content_buf:
                return
            full = "".join(self._content_buf)
            self._content_buf.clear()
        self._append_conv("assistant", full)

    def _finalize_response(self, msg: dict) -> None:
        """处理后端最终响应"""
        self._finalize_thinking()

        with self._buf_lock:
            thinking_text = "".join(self._thinking_buf)
            content_text = "".join(self._content_buf)
            self._thinking_buf = []
            self._content_buf = []

        if thinking_text and content_text:
            full = f"💭 *thinking:*\n_{thinking_text}_\n\n---\n\n{content_text}"
        elif thinking_text:
            full = f"💭 *{thinking_text}*\n\n{content_text}"
        else:
            full = content_text

        if full.strip():
            self._append_conv("assistant", full)

        self.backend.send({"type": "save_session"})
        # 一轮结束：清空 dedupe 集合，下一轮重新计数
        self._seen_msg_ids.clear()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        user_text = event.value.strip()
        if not user_text:
            return
        # 清空后立即发送（非事务性，但保证UI先清理）
        with self._buf_lock:
            self._thinking_buf.clear()
            self._content_buf.clear()
        self._seen_msg_ids.clear()  # 新一轮：dedupe 重置
        self._thinking_widget = None
        self._tool_widget = None
        self._clear_conv_widgets()
        event.input.clear()
        self._append_user(user_text)
        self.backend.send({"type": "user_message", "content": user_text})

    def action_clear_conv(self) -> None:
        self._clear_conv_widgets()
        self._thinking_widget = None
        self._tool_widget = None

    def _clear_conv_widgets(self) -> None:
        try:
            scroll = self.query_one("#conv_scroll", ScrollableContainer)
            for widget in scroll.children:
                widget.remove()
        except Exception:
            pass

    def action_toggle_debug(self) -> None:
        """切换 debug 模式（仅前端日志，不影响后端）"""
        self._debug = not self._debug
        self._refresh_header()

    def on_unmount(self) -> None:
        """textual 卸载兜底：保证后端进程不残留（含 ctrl-c 路径）"""
        self.backend.stop()


# ============================================================================
# 入口
# ============================================================================

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="EVA TUI")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument(
        "--no-greet",
        action="store_true",
        help="Skip the auto greeting message on backend ready",
    )
    parser.add_argument(
        "-a", "--allow-all", action="store_true",
        help="Allow all CLI commands (dangerous)",
    )
    args = parser.parse_args()

    app = EVATUI(
        debug=args.debug,
        allow_all_cli=args.allow_all,
        auto_greet=not args.no_greet,
    )
    app.run()
