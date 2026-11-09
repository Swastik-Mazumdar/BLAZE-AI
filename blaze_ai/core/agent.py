"""Local-model driven computer operator.

The agent asks the active Hugging Face chat model for one JSON action at a
time, executes it, feeds the result back, and repeats until the model calls
``finish``. It is deliberately provider-agnostic: no native tool-calling API is
required, so it works with small local models.

Safety is layered:
  * every risky action is shown to the user before it runs unless auto-approve
    is enabled for the task;
  * catastrophic shell patterns are always blocked, even with auto-approve;
  * the model gets a hard step limit and a per-command timeout.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..config import app_data_dir

log = logging.getLogger("blaze.agent")

MAX_STEPS = 8
COMMAND_TIMEOUT = 180
AGENT_DEADLINE_SECONDS = 150
MAX_OUTPUT = 6_000
MAX_PROMPT_CHARS = 9_000
MAX_ACTION_REPLY_CHARS = 48_000
CONFIRM_EACH_ACTION_TOOLS = frozenset(
    {
        "run_shell",
        "ffmpeg",
        "apple_script",
        "write_file",
        "type_text",
        "press_keys",
        "click",
        "clipboard_write",
    }
)

SYSTEM_PROMPT = """You are Blaze, an autonomous computer operator running on {platform}.
You complete real tasks by choosing ONE tool per turn.

Reply with exactly one JSON object and no other text. Never include analysis, reasoning, or <|channel|> text outside the JSON.
{{"thought": "short reason", "tool": "tool_name", "args": {{...}}}}

Available tools:
- run_shell: {{"command": "shell command"}} — terminal work, open URLs/apps with args, file ops.
- ffmpeg: {{"args": ["-i", "input.mp4", ...]}} — bundled ffmpeg for video/audio editing.
- apple_script: {{"script": "..."}} — macOS automation (Music play/search, Mail, Messages, Finder, browser tabs where supported).
- open_app: {{"name": "Safari"}} — launch/focus an app. Opening alone rarely completes compound requests.
- close_app: {{"name": "Safari"}}
- type_text: {{"text": "...", "press_enter": false}} — type into the focused app.
- press_keys: {{"keys": "cmd+l"}} — keyboard shortcuts (cmd+l address bar, cmd+f find, enter, etc.).
- click: {{"x": 500, "y": 300, "button": "left", "clicks": 1}}
- screenshot: {{"path": "optional.png"}}
- read_file: {{"path": "/path/to/file"}}
- write_file: {{"path": "/path/to/file", "content": "..."}}
- list_dir: {{"path": "/path"}}
- clipboard_read: {{}}
- clipboard_write: {{"text": "..."}}
- wait: {{"seconds": 2}}
- finish: {{"message": "final answer for the user"}}

Rules:
1. Never invent tool output. Wait for the result before the next step.
2. COMPLETE EVERY PART of the user's request. If asked to "open X and play/search/open Y", do all parts. After open_app succeeds, continue: e.g. Music playback -> apple_script (tell application "Music" to play or search); Brave/Safari/Chrome open a site -> run_shell `open -a "Brave Browser" "https://youtube.com"` or focus app + press_keys cmd+l + type_text URL + press enter. Do NOT call finish until every requested part is done or you have hit a real blocker.
3. Prefer ffmpeg for video/audio, apple_script for Music/Mail/Messages, run_shell/open for URLs.
4. If the task is only a question, greeting, or small talk, call finish immediately with the answer. Never call type_text for a greeting or question.
5. Keep shell commands non-destructive. Never use sudo, disk erase, or rm -rf on system paths.
6. When fully complete, call finish with a concise summary of what was done.
7. If a tool fails, try one different approach, then finish and explain the failure. Never repeat the exact same failing action.
8. If blocked by macOS Accessibility/automation permissions, stop and report the permission problem instead of retrying.
9. If you cannot comply safely or do not know how, call finish with message "Cannot comply with request. Please try again." Do not output hidden reasoning.

Platform: {platform}
User name: {user_name}
"""

_TOOL_ALIASES = {
    "shell": "run_shell",
    "bash": "run_shell",
    "run_command": "run_shell",
    "execute": "run_shell",
    "terminal": "run_shell",
    "applescript": "apple_script",
    "osascript": "apple_script",
    "open": "open_app",
    "launch": "open_app",
    "close": "close_app",
    "quit": "close_app",
    "type": "type_text",
    "type_text": "type_text",
    "write_text": "type_text",
    "paste": "type_text",
    "keys": "press_keys",
    "hotkey": "press_keys",
    "keypress": "press_keys",
    "mouse": "click",
    "screenshot": "screenshot",
    "screen": "screenshot",
    "read": "read_file",
    "read_file": "read_file",
    "write": "write_file",
    "write_file": "write_file",
    "ls": "list_dir",
    "list": "list_dir",
    "listdir": "list_dir",
    "clipboard": "clipboard_read",
    "copy": "clipboard_read",
    "paste_clipboard": "clipboard_write",
    "video": "ffmpeg",
    "edit_video": "ffmpeg",
    "wait": "wait",
    "sleep": "wait",
    "done": "finish",
    "answer": "finish",
    "finish": "finish",
}

_DANGEROUS = re.compile(
    r"(?:^|\s)(?:sudo|doas)\b"
    r"|\brm\s+-[a-z]*r[a-z]*f?[a-z]*\s+(?:/|~|\$HOME|\*)(?:[/\s*]|$)"
    r"|\bfind\s+/(?:\s|$)[^\n]*\s-delete\b"
    r"|\bmkfs(?:\.[a-z0-9]+)?(?:\s|$)"
    r"|\bdiskutil\s+erase"
    r"|\bdd\b[^\n]*\bof=/dev/"
    r"|\bchmod\s+-R\s+777\s+/"
    r"|>\s*/dev/(?:sd|disk|rdisk)"
    r"|\b(?:shutdown|reboot|halt|poweroff)\b"
    r"|/dev/(?:tcp|udp)/"
    r"|\b(?:nc|ncat|netcat)\b[^\n]*(?:\s-e\s|--exec|--sh-exec)"
    r"|\b(?:bash|sh)\b[^\n]*\s-i\b[^\n]*(?:/dev/tcp|nc\s)"
    r"|:\s*\(\s*\)\s*\{",
    re.IGNORECASE,
)

_BLOCKED_PROGRAMS = {
    "sudo", "doas", "mkfs", "shutdown", "reboot", "halt", "poweroff",
    "stop-computer", "restart-computer", "clear-disk", "format-volume",
    "format", "diskpart", "bcdedit", "sdelete", "sdelete64", "sshd",
    "eval",
}
_INLINE_SHELLS = {"sh", "bash", "zsh", "fish", "dash", "cmd", "powershell", "pwsh"}
_INLINE_CODE = {"python", "python2", "python3", "perl", "ruby", "node", "php"}
_UNSAFE_APPLESCRIPT = re.compile(
    r"\bdo\s+shell\s+script\b|\b(?:shut\s+down|restart)\b",
    re.IGNORECASE,
)
_UNSAFE_INLINE_CODE = re.compile(
    r"\b(?:exec|eval|compile)\s*\(|\bos\.system\s*\(|"
    r"\bsubprocess\.(?:run|Popen|call)\s*\(|\b__import__\s*\(|"
    r"\b(?:SSHTunnelForwarder|paramiko|asyncssh)\b",
    re.IGNORECASE,
)


def _dangerous_shell_command(command: str, *, depth: int = 0) -> bool:
    """Conservatively reject destructive commands, including quoted shell layers."""
    if depth > 4:
        return True
    unquoted = re.sub(r"[\"'`]", " ", command)
    if _DANGEROUS.search(command) or _DANGEROUS.search(unquoted):
        return True
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        # Broken quoting is ambiguous and should not be executed by the shell.
        return True

    segments: list[list[str]] = [[]]
    for token in tokens:
        if token and all(char in ";&|()" for char in token):
            segments.append([])
        else:
            segments[-1].append(token)

    for index, segment in enumerate(segments):
        if not segment:
            continue
        # Skip common NAME=value prefixes when finding the command.
        cmd_index = 0
        while cmd_index < len(segment) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", segment[cmd_index]):
            cmd_index += 1
        if cmd_index >= len(segment):
            continue
        executable = os.path.basename(segment[cmd_index]).lower().removesuffix(".exe")
        args = segment[cmd_index + 1 :]
        if executable in _BLOCKED_PROGRAMS or executable.startswith("mkfs."):
            return True
        if executable in {"ssh", "ssh.exe"}:
            # SSH config can create forwarding even when the command line has
            # no -L/-R/-D flags, so model-driven shell actions cannot invoke it.
            return True
        if executable in {
            "autossh", "sshuttle", "ngrok", "chisel", "frpc", "frps",
            "openvpn", "tor",
        }:
            return True
        if executable in {"nc", "ncat", "netcat", "socat"}:
            if any(
                arg.lower() in {"-listen", "listen", "tunnel", "-r", "--reverse"}
                or (arg.startswith("-") and not arg.startswith("--") and "l" in arg[1:].lower())
                or re.match(r"(?:TCP|UDP|OPENSSL)[46]?-LISTEN", arg, re.I)
                for arg in args
            ):
                return True
        if executable == "cloudflared" and any("tunnel" in arg.lower() for arg in args):
            return True
        if executable == "kubectl" and "port-forward" in {arg.lower() for arg in args}:
            return True
        if executable in {"docker", "podman"} and any(
            arg in {"-p", "--publish"} or arg.startswith("--publish=") for arg in args
        ):
            return True
        if executable == "tailscale" and any(
            arg.lower() in {"serve", "funnel"} for arg in args
        ):
            return True
        if executable == "rm":
            options = " ".join(arg.lower() for arg in args if arg.startswith("-"))
            if "r" in options or "recursive" in options:
                return True
        if executable == "find" and any(arg in {"-delete", "-exec", "-execdir"} for arg in args):
            return True
        if executable == "dd" and any(re.match(r"of\s*=\s*/dev/", arg, re.I) for arg in args):
            return True
        if executable == "vssadmin" and "delete" in {arg.lower() for arg in args}:
            return True
        if executable == "wbadmin" and "delete" in {arg.lower() for arg in args}:
            return True
        if executable == "chmod" and "-r" in {arg.lower() for arg in args} and any(
            arg == "777" or arg.startswith("/") for arg in args
        ):
            return True
        if executable in {"remove-item", "del", "rmdir"} and any(
            arg.lower() in {"-recurse", "-recursive", "/s", "-force", "/q"} for arg in args
        ):
            return True
        if executable in _INLINE_SHELLS | _INLINE_CODE:
            for arg_index, arg in enumerate(args[:-1]):
                if arg.lower() in {"-c", "-command", "/c", "-e"}:
                    if executable in _INLINE_CODE and _UNSAFE_INLINE_CODE.search(args[arg_index + 1]):
                        return True
                    if _dangerous_shell_command(args[arg_index + 1], depth=depth + 1):
                        return True
                if arg.lower() in {"-encodedcommand", "-enc"} and arg_index + 1 < len(args):
                    try:
                        import base64

                        decoded = base64.b64decode(args[arg_index + 1], validate=True)
                        text = decoded.decode("utf-16le", errors="strict")
                    except (ValueError, UnicodeError):
                        return True
                    if _dangerous_shell_command(text, depth=depth + 1):
                        return True
        # A pipe or chained fetch into an interpreter executes remote text directly.
        if index > 0 and executable in _INLINE_SHELLS and tokens and "|" in tokens:
            return True
        if executable in {"curl", "wget", "invoke-webrequest", "iwr"} and any(
            next_segment and os.path.basename(next_segment[0]).lower() in _INLINE_SHELLS
            for next_segment in segments[index + 1 :]
        ):
            return True
    return False


@dataclass(frozen=True)
class AgentAction:
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    thought: str = ""

    @property
    def summary(self) -> str:
        if self.tool == "run_shell":
            return f"$ {self.args.get('command', '')}"
        if self.tool == "apple_script":
            script = str(self.args.get("script", ""))
            return script if len(script) <= 240 else script[:237] + "…"
        if self.tool == "ffmpeg":
            args = self.args.get("args", [])
            return "ffmpeg " + " ".join(str(a) for a in args)
        if self.tool in {"open_app", "close_app"}:
            return str(self.args.get("name", ""))
        if self.tool == "type_text":
            text = str(self.args.get("text", ""))
            return text if len(text) <= 240 else text[:237] + "…"
        if self.tool == "write_file":
            return str(self.args.get("path", ""))
        return json.dumps(self.args, ensure_ascii=False)[:240]

    @property
    def description(self) -> str:
        if self.tool == "run_shell":
            return f"Run this shell command?\n\n{self.args.get('command', '')}"
        if self.tool == "apple_script":
            return f"Run this AppleScript?\n\n{self.args.get('script', '')}"
        if self.tool == "ffmpeg":
            return f"Run ffmpeg?\n\n{self.summary}"
        if self.tool == "type_text":
            return f"Type this into the focused app?\n\n{self.args.get('text', '')}"
        if self.tool == "press_keys":
            return f"Press keyboard shortcut: {self.args.get('keys', '')}"
        if self.tool == "click":
            return (
                f"Click at ({self.args.get('x', '?')}, {self.args.get('y', '?')}) "
                f"with the {self.args.get('button', 'left')} button."
            )
        if self.tool == "write_file":
            return f"Write file?\n\n{self.args.get('path', '')}"
        if self.tool in {"open_app", "close_app"}:
            return f"{'Open' if self.tool == 'open_app' else 'Close'} {self.args.get('name', '')}?"
        return f"Run {self.tool}?\n\n{self.summary}"


@dataclass(frozen=True)
class AgentStep:
    action: AgentAction
    output: str
    ok: bool


class AgentError(RuntimeError):
    pass


_NOOP_TOOLS = {"none", "noop", "no_op", "no_action", "stop", "null", "nothing"}
_INFORMATIVE_TOOLS = {
    "run_shell",
    "ffmpeg",
    "apple_script",
    "list_dir",
    "read_file",
    "screenshot",
    "clipboard_read",
}


def extract_final_text(raw: str) -> str:
    """Return user-facing text, unwrapping raw agent/tool JSON when present."""
    original = (raw or "").strip()
    if not original:
        return ""
    text = _strip_thinking(original)
    action = parse_action(text or original)
    if action is not None and action.tool == "finish":
        message = str(action.args.get("message") or action.thought or "").strip()
        if message:
            return message
    candidate = (text or original).strip()
    if candidate.lstrip().startswith("{") and (
        '"tool"' in candidate or '"action"' in candidate or '"thought"' in candidate
    ):
        # A non-finish action leaked to a display path; never show raw internals.
        if action is not None and action.tool != "finish":
            return "Cannot comply with request. Please try again."
        extracted = _extract_finish_message(candidate)
        if extracted:
            return extracted
    # Never leak Harmony/analysis thinking into the UI.
    if "<|channel|>" in original or "<|message|>" in original:
        if text and '"tool"' not in text and '"action"' not in text and "{" not in text[:1]:
            return text or "Cannot comply with request. Please try again."
        return "Cannot comply with request. Please try again."
    return text or original


def _final_answer(last_success: str, raw: str) -> str:
    if last_success:
        return f"Done. Last successful result:\n\n{last_success[:4000]}"
    text = extract_final_text(raw)
    return text or "Done."


_INPUT_PERMISSION_HINTS = (
    "not allowed to send keystrokes",
    "not allowed assistive access",
    "osascript is not allowed",
    "accessibility",
    "operation not permitted",
)


def _is_input_permission_error(output: str) -> bool:
    lowered = (output or "").lower()
    return any(hint in lowered for hint in _INPUT_PERMISSION_HINTS)


def _permission_reply() -> str:
    return (
        "I could not type because macOS blocked Blaze's Accessibility access. "
        "Open System Settings → Privacy & Security → Accessibility, enable "
        "Blaze AI, then try again."
    )


def _platform_name() -> str:
    if sys.platform == "darwin":
        return "macOS"
    if os.name == "nt":
        return "Windows"
    return "Linux"


_INVALID_ESCAPE_RE = re.compile(r'\\(?!["\\/bfnrtu])')


def _loads_tolerant(text: str) -> Any | None:
    """Parse JSON, repairing common small-model mistakes (invalid escapes, controls)."""
    try:
        return json.loads(text)
    except ValueError:
        pass
    repaired = _INVALID_ESCAPE_RE.sub(r"\\\\", text)
    # Escape raw control characters that are illegal inside JSON strings.
    repaired = "".join(
        f"\\u{ord(ch):04x}" if ch in ("\t", "\n", "\r") or (ord(ch) < 32) else ch
        for ch in repaired
    )
    try:
        return json.loads(repaired)
    except ValueError:
        pass
    # Last resort: fix only backslash escapes without touching structure.
    try:
        return json.loads(_INVALID_ESCAPE_RE.sub(r"\\\\", text.replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")))
    except ValueError:
        return None


def _extract_finish_message(raw: str) -> str | None:
    """Pull a user-facing finish message from malformed tool JSON."""
    text = (raw or "").strip()
    if not text or '"finish"' not in text.lower() and '"tool"' not in text:
        return None
    # Try to capture args.message value (handles escaped quotes/backslashes imperfectly
    # but well enough for display).
    patterns = [
        r'"message"\s*:\s*"((?:[^"\\]|\\.)*)"',
        r'"response"\s*:\s*"((?:[^"\\]|\\.)*)"',
        r'"text"\s*:\s*"((?:[^"\\]|\\.)*)"',
        r'"answer"\s*:\s*"((?:[^"\\]|\\.)*)"',
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.DOTALL)
        if match:
            try:
                return json.loads(f'"{match.group(1)}"')
            except ValueError:
                value = match.group(1)
                value = value.replace("\\n", "\n").replace("\\t", "\t").replace('\\"', '"')
                value = _INVALID_ESCAPE_RE.sub(r"\\", value)
                return value.strip() or None
    return None


def _strip_thinking(text: str) -> str:
    result = (text or "").strip()
    if not result:
        return ""
    result = re.sub(r"<think\b[^>]*>.*?</think>", "", result, flags=re.IGNORECASE | re.DOTALL)
    m = re.search(
        r"<\|channel\|>\s*final(?:<\|message\|>|:)\s*(.*?)(?:<\|end\|>|<\|channel\|>|$)",
        result,
        re.IGNORECASE | re.DOTALL,
    )
    if m:
        result = m.group(1).strip()
    if "<|channel|>" in result or "<|message|>" in result:
        # Prefer a JSON tool action/object if present (models sometimes leak analysis + JSON).
        for candidate in re.finditer(r"\{", result):
            js = candidate.start()
            je = result.rfind("}")
            if je <= js:
                continue
            snippet = result[js : je + 1]
            if '"tool"' in snippet or '"action"' in snippet:
                result = snippet
                return result.strip()
        result = re.sub(r"<\|(?:channel|message|start|end|return)\|>[^<\n]*", "", result, flags=re.IGNORECASE)
    return result.strip()


def parse_action(raw: str) -> AgentAction | None:
    """Extract one tool action from a model reply, tolerating small-model noise."""
    text = _strip_thinking(raw)
    if not text:
        return None
    if len(text) > MAX_ACTION_REPLY_CHARS:
        return None
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        finish = _extract_finish_message(text)
        return AgentAction("finish", {"message": finish}, "") if finish else None
    payload = _loads_tolerant(text[start : end + 1])
    if not isinstance(payload, dict):
        finish = _extract_finish_message(text)
        return AgentAction("finish", {"message": finish}, "") if finish else None
    tool = str(payload.get("tool") or payload.get("action") or "").strip().lower()
    if not tool:
        # Some models put the command at the top level.
        if payload.get("command"):
            tool = "run_shell"
        elif payload.get("script"):
            tool = "apple_script"
        else:
            finish = _extract_finish_message(text)
            return AgentAction("finish", {"message": finish}, "") if finish else None
    tool = _TOOL_ALIASES.get(tool, tool)
    args = payload.get("args")
    if not isinstance(args, dict):
        args = {}
        for key in (
            "command",
            "script",
            "name",
            "text",
            "keys",
            "path",
            "content",
            "x",
            "y",
            "button",
            "clicks",
            "seconds",
            "message",
            "args",
        ):
            if key in payload:
                args[key] = payload[key]
    if tool == "finish" and "message" not in args:
        for key in ("message", "response", "text", "answer", "summary"):
            if payload.get(key):
                args["message"] = payload[key]
                break
        if "message" not in args:
            extracted = _extract_finish_message(text)
            args["message"] = extracted or str(payload.get("thought") or "Done.")
    return AgentAction(tool=tool, args=args, thought=str(payload.get("thought") or ""))


class ComputerAgent:
    """Execute a natural-language task through iterative model/tool turns."""

    def __init__(
        self,
        model,
        *,
        user_name: str = "friend",
        confirm: Callable[[AgentAction], bool] | None = None,
        typing=None,
        before_input: Callable[[AgentAction], None] | None = None,
        max_steps: int = MAX_STEPS,
        timeout: int = COMMAND_TIMEOUT,
        max_seconds: int = AGENT_DEADLINE_SECONDS,
    ) -> None:
        self.model = model
        self.user_name = user_name
        self.confirm = confirm
        self.typing = typing
        self.before_input = before_input
        self.max_steps = max(1, min(30, int(max_steps)))
        self.timeout = max(5, min(600, int(timeout)))
        self.max_seconds = max(30, min(900, int(max_seconds)))
        self._auto_approve = False
        self.cancelled = False

    def run(
        self,
        task: str,
        *,
        context: list[dict[str, str]] | None = None,
        on_step: Callable[[AgentStep], None] | None = None,
    ) -> str:
        task = (task or "").strip()
        if not task:
            return "There was nothing to do."
        prompt = SYSTEM_PROMPT.format(
            platform=_platform_name(),
            user_name=self.user_name,
        )
        messages: list[dict[str, str]] = [
            {"role": "user", "content": f"{prompt}\n\nUSER TASK:\n{task}"}
        ]
        if context:
            # A short recap of the thread helps continuity without bloating the
            # prompt for small-context local models.
            recap = "\n".join(
                f"{item['role']}: {item['content'][:400]}"
                for item in context[-2:]
                if item.get("content")
            )
            if recap:
                messages[0]["content"] += f"\n\nRECENT CHAT:\n{recap}"

        last_success = ""
        started = time.monotonic()
        last_signature = ""
        repeat_count = 0
        for step_index in range(self.max_steps):
            if self.cancelled:
                return "Stopped."
            if time.monotonic() - started > self.max_seconds:
                return (
                    "I stopped because the task took too long without finishing. "
                    "Try a smaller, more specific request."
                )
            raw = self._respond(messages)
            if len(raw) > MAX_ACTION_REPLY_CHARS:
                return "I stopped because the model returned an oversized action response."
            action = parse_action(raw)
            if action is None:
                messages.append({"role": "assistant", "content": raw})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "That was not valid JSON. Reply with ONE JSON object only, "
                            'for example {"thought":"...","tool":"finish","args":{"message":"..."}}.'
                        ),
                    }
                )
                raw = self._respond(messages)
                action = parse_action(raw)
                if action is None:
                    extracted = _extract_finish_message(raw) or _extract_finish_message(
                        messages[-2]["content"] if len(messages) >= 2 else ""
                    )
                    return extracted or extract_final_text(raw) or "The model did not return an action."

            if action.tool == "finish":
                return str(action.args.get("message") or action.thought or "Done.")

            if action.tool in _NOOP_TOOLS:
                messages.append({"role": "assistant", "content": raw})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Do not stop without a result. Reply with ONE JSON object: "
                            "either a valid tool call, or "
                            '{"tool":"finish","args":{"message":"final answer"}}.'
                        ),
                    }
                )
                raw = self._respond(messages)
                retry = parse_action(raw)
                if retry is None or retry.tool in _NOOP_TOOLS:
                    return _final_answer(last_success, raw)
                action = retry
                if action.tool == "finish":
                    return str(action.args.get("message") or action.thought or "Done.")

            signature = f"{action.tool}:{json.dumps(action.args, sort_keys=True, default=str)}"
            if signature == last_signature:
                repeat_count += 1
            else:
                repeat_count = 0
            last_signature = signature
            if repeat_count >= 2:
                return (
                    "I stopped because the model kept repeating the same action "
                    "without making progress. Try rephrasing the request."
                )

            if action.tool not in _TOOL_ALIASES.values() and action.tool not in {
                "run_shell",
                "ffmpeg",
                "apple_script",
                "open_app",
                "close_app",
                "type_text",
                "press_keys",
                "click",
                "screenshot",
                "read_file",
                "write_file",
                "list_dir",
                "clipboard_read",
                "clipboard_write",
                "wait",
            }:
                output, ok = f"Unknown tool: {action.tool}", False
            else:
                if not self._approved(action):
                    return "Stopped before running that action."
                output, ok = self._execute(action)

            step = AgentStep(action=action, output=output, ok=ok)
            if on_step is not None:
                on_step(step)
            if (
                not ok
                and action.tool in {"type_text", "press_keys", "click"}
                and _is_input_permission_error(output)
            ):
                return _permission_reply()
            if ok and output and action.tool in _INFORMATIVE_TOOLS:
                last_success = output
            messages.append({"role": "assistant", "content": raw})
            nudge = ""
            if step_index >= self.max_steps - 2:
                nudge = (
                    "\n\nSTEP LIMIT NEAR: call finish NOW with a concise summary "
                    "of what was done, even if the task is only partially complete."
                )
            task_lower = task.lower()
            if ok and action.tool == "open_app" and re.search(
                r"\b(and|then|after|play|search|open|go to|find|type|browse|visit|watch|listen)\b",
                task_lower,
            ):
                nudge += (
                    "\n\nThe app is open but the user asked for more than just opening it. "
                    "Do the next requested step now (apple_script, run_shell with URL, "
                    "press_keys/type_text/wait). Do not finish yet."
                )
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"TOOL RESULT ({action.tool}, {'ok' if ok else 'error'}):\n"
                        f"{output[:MAX_OUTPUT]}\n\n"
                        f"USER TASK: {task[:600]}\n"
                        "Complete EVERY part of the user task. Only call finish when all "
                        "requested parts are done (or truly blocked). Otherwise choose the "
                        "next tool."
                        f"{nudge}"
                    ),
                }
            )
            messages = _trim(messages)

        if last_success:
            return (
                f"I reached the {self.max_steps}-step limit before finishing. "
                "Here is the last successful result:\n\n"
                f"{last_success[:3000]}\n\n"
                "Send “continue” to keep going."
            )
        return (
            f"I reached the {self.max_steps}-step limit before finishing. "
            "The last actions are shown above; send “continue” to keep going."
        )

    # ------------------------------------------------------------- approval
    def _respond(self, messages: list[dict[str, str]]) -> str:
        responder = getattr(self.model, "respond_action", None)
        if callable(responder):
            response = responder(messages, self.user_name)
        else:
            response = self.model.respond(messages, self.user_name)
        if len(response) > MAX_ACTION_REPLY_CHARS:
            return response[: MAX_ACTION_REPLY_CHARS + 1]
        return response

    def _approved(self, action: AgentAction) -> bool:
        # Catastrophic shell operations can never be auto-approved.
        if action.tool == "run_shell":
            command = str(action.args.get("command", ""))
            if _dangerous_shell_command(command):
                return False
        if action.tool == "ffmpeg":
            raw_args = action.args.get("args", [])
            joined = " ".join(str(a) for a in raw_args)
            if _dangerous_shell_command(joined) or any(
                re.match(r"^/dev/(?:sd|disk|rdisk|nvme)", str(arg), re.I)
                for arg in raw_args
            ):
                return False
        if action.tool == "apple_script" and _UNSAFE_APPLESCRIPT.search(
            str(action.args.get("script", ""))
        ):
            return False
        if self._auto_approve and action.tool not in CONFIRM_EACH_ACTION_TOOLS:
            return True
        if self.confirm is None:
            return True
        allowed = bool(self.confirm(action))
        if (
            allowed
            and action.tool not in CONFIRM_EACH_ACTION_TOOLS
            and getattr(self.confirm, "always", False)
        ):
            self._auto_approve = True
        return allowed

    # ---------------------------------------------------------------- tools
    def _execute(self, action: AgentAction) -> tuple[str, bool]:
        try:
            handler = getattr(self, f"_tool_{action.tool}", None)
            if handler is None:
                return f"Unknown tool: {action.tool}", False
            if action.tool in {"type_text", "press_keys", "click"} and self.before_input:
                self.before_input(action)
            return handler(action.args)
        except subprocess.TimeoutExpired:
            return f"Timed out after {self.timeout}s.", False
        except Exception as exc:  # noqa: BLE001 - tool failures are data for the model
            log.exception("Agent tool failed: %s", action.tool)
            return f"{type(exc).__name__}: {exc}", False

    def _tool_run_shell(self, args: dict[str, Any]) -> tuple[str, bool]:
        command = str(args.get("command", "")).strip()
        if not command:
            return "No command supplied.", False
        if _dangerous_shell_command(command):
            return (
                "Blocked for safety: that command can damage the system. "
                "Use a narrower, non-destructive command.",
                False,
            )
        timeout = min(self.timeout, max(1, int(args.get("timeout", self.timeout))))
        env = os.environ.copy()
        ffmpeg = _ffmpeg_path()
        if ffmpeg:
            env["BLAZE_FFMPEG"] = ffmpeg
            env["PATH"] = str(Path(ffmpeg).parent) + os.pathsep + env.get("PATH", "")
        result = subprocess.run(
            command,
            shell=True,
            cwd=str(Path.home()),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            env=env,
        )
        return _format_result(result), result.returncode == 0

    def _tool_ffmpeg(self, args: dict[str, Any]) -> tuple[str, bool]:
        binary = _ffmpeg_path()
        if not binary:
            return "ffmpeg is not available in this build.", False
        raw_args = args.get("args", args.get("command", []))
        if isinstance(raw_args, str):
            parts = _split_args(raw_args)
        elif isinstance(raw_args, list):
            parts = [str(part) for part in raw_args]
        else:
            return "ffmpeg args must be a list or string.", False
        if not parts:
            return "No ffmpeg arguments supplied.", False
        result = subprocess.run(
            [binary, *parts],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=self.timeout,
        )
        return _format_result(result), result.returncode == 0

    def _tool_apple_script(self, args: dict[str, Any]) -> tuple[str, bool]:
        if sys.platform != "darwin":
            return "AppleScript is only available on macOS.", False
        script = str(args.get("script", "")).strip()
        if not script:
            return "No AppleScript supplied.", False
        if _UNSAFE_APPLESCRIPT.search(script):
            return "Blocked for safety: shell execution and power controls are not allowed through AppleScript.", False
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=self.timeout,
        )
        return _format_result(result), result.returncode == 0

    def _tool_open_app(self, args: dict[str, Any]) -> tuple[str, bool]:
        name = str(args.get("name") or args.get("app") or "").strip()
        if not name:
            return "No application name supplied.", False
        if sys.platform == "darwin":
            cmd = ["open", "-a", name]
        elif os.name == "nt":
            try:
                os.startfile(name)  # type: ignore[attr-defined]
                return f"Opened {name}.", True
            except OSError as exc:
                return f"Could not open {name}: {exc}", False
        else:
            cmd = ["xdg-open", name]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
        if result.returncode == 0:
            return f"Opened {name}.", True
        return _format_result(result), False

    def _tool_close_app(self, args: dict[str, Any]) -> tuple[str, bool]:
        name = str(args.get("name") or args.get("app") or "").strip()
        if not name:
            return "No application name supplied.", False
        if sys.platform == "darwin":
            script = (
                "on run argv\n"
                'tell application (item 1 of argv) to quit\n'
                "end run"
            )
            result = subprocess.run(
                ["osascript", "-e", script, name], capture_output=True, text=True, timeout=30
            )
        elif os.name == "nt":
            result = subprocess.run(
                ["taskkill", "/IM", f"{name}.exe"], capture_output=True, text=True, timeout=30
            )
        else:
            result = subprocess.run(
                ["pkill", "-f", name], capture_output=True, text=True, timeout=30
            )
        if result.returncode == 0:
            return f"Closed {name}.", True
        return _format_result(result), False

    def _tool_type_text(self, args: dict[str, Any]) -> tuple[str, bool]:
        text = str(args.get("text", ""))
        if not text:
            return "No text supplied.", False
        press_enter = bool(args.get("press_enter", False))
        if self.typing is None:
            return "The text injection service is not available.", False
        self.typing.type_text(text, press_enter=press_enter)
        return f"Typed {len(text)} characters into the focused app.", True

    def _tool_press_keys(self, args: dict[str, Any]) -> tuple[str, bool]:
        keys = str(args.get("keys", "")).strip()
        if not keys:
            return "No keys supplied.", False
        try:
            from pynput.keyboard import Controller, Key
        except ImportError:
            return "pynput is not installed.", False
        controller = Controller()
        tokens = [token.strip().lower() for token in re.split(r"\s*\+\s*", keys) if token.strip()]
        special = {
            "cmd": Key.cmd,
            "command": Key.cmd,
            "win": Key.cmd,
            "super": Key.cmd,
            "ctrl": Key.ctrl,
            "control": Key.ctrl,
            "alt": Key.alt,
            "option": Key.alt,
            "shift": Key.shift,
            "enter": Key.enter,
            "return": Key.enter,
            "tab": Key.tab,
            "esc": Key.esc,
            "escape": Key.esc,
            "space": Key.space,
            "backspace": Key.backspace,
            "delete": Key.delete,
            "up": Key.up,
            "down": Key.down,
            "left": Key.left,
            "right": Key.right,
            "home": Key.home,
            "end": Key.end,
            "pageup": Key.page_up,
            "pagedown": Key.page_down,
            "f1": Key.f1,
            "f2": Key.f2,
            "f3": Key.f3,
            "f4": Key.f4,
            "f5": Key.f5,
            "f6": Key.f6,
            "f7": Key.f7,
            "f8": Key.f8,
            "f9": Key.f9,
            "f10": Key.f10,
            "f11": Key.f11,
            "f12": Key.f12,
        }
        modifiers = [special[token] for token in tokens[:-1] if token in special]
        final = tokens[-1]
        key = special.get(final, final if len(final) == 1 else None)
        if key is None:
            return f"Unknown key: {final}", False
        for modifier in modifiers:
            controller.press(modifier)
        try:
            controller.press(key)
            controller.release(key)
        finally:
            for modifier in reversed(modifiers):
                controller.release(modifier)
        return f"Pressed {keys}.", True

    def _tool_click(self, args: dict[str, Any]) -> tuple[str, bool]:
        try:
            from pynput.mouse import Button, Controller
        except ImportError:
            return "pynput is not installed.", False
        x = int(args.get("x", 0))
        y = int(args.get("y", 0))
        button_name = str(args.get("button", "left")).lower()
        button = {
            "left": Button.left,
            "right": Button.right,
            "middle": Button.middle,
        }.get(button_name, Button.left)
        clicks = max(1, min(3, int(args.get("clicks", 1))))
        controller = Controller()
        controller.position = (x, y)
        time.sleep(0.08)
        controller.click(button, clicks)
        return f"Clicked at ({x}, {y}).", True

    def _tool_screenshot(self, args: dict[str, Any]) -> tuple[str, bool]:
        target = str(args.get("path", "")).strip()
        if target:
            path = Path(target).expanduser()
        else:
            folder = app_data_dir() / "screenshots"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"agent-{datetime_stamp()}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        if sys.platform == "darwin":
            cmd = ["screencapture", "-x", str(path)]
        elif os.name == "nt":
            env = os.environ.copy()
            env["BLAZE_SCREENSHOT_PATH"] = str(path)
            cmd = [
                "powershell",
                "-NoProfile",
                "-Command",
                "Add-Type -AssemblyName System.Windows.Forms;"
                "Add-Type -AssemblyName System.Drawing;"
                "$b=[System.Windows.Forms.Screen]::PrimaryScreen.Bounds;"
                f"$bmp=New-Object System.Drawing.Bitmap $b.Width,$b.Height;"
                "$g=[System.Drawing.Graphics]::FromImage($bmp);"
                "$g.CopyFromScreen($b.Location,[System.Drawing.Point]::Empty,$b.Size);"
                "$bmp.Save($env:BLAZE_SCREENSHOT_PATH)",
            ]
        else:
            tool = shutil.which("gnome-screenshot") or shutil.which("import")
            if not tool:
                return "No screenshot tool is available.", False
            cmd = [tool, str(path)] if tool.endswith("gnome-screenshot") else [tool, str(path)]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            env=env if os.name == "nt" else None,
        )
        if result.returncode == 0 and path.exists():
            return f"Screenshot saved to {path}", True
        return _format_result(result), False

    def _tool_read_file(self, args: dict[str, Any]) -> tuple[str, bool]:
        path = Path(str(args.get("path", ""))).expanduser()
        if not path.is_file():
            return f"File not found: {path}", False
        data = path.read_text(encoding="utf-8", errors="replace")
        return data[:20_000], True

    def _tool_write_file(self, args: dict[str, Any]) -> tuple[str, bool]:
        raw_path = str(args.get("path", "")).strip()
        if not raw_path:
            return "No path supplied.", False
        path = Path(raw_path).expanduser()
        content = str(args.get("content", ""))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} characters to {path}", True

    def _tool_list_dir(self, args: dict[str, Any]) -> tuple[str, bool]:
        path = Path(str(args.get("path", "~"))).expanduser()
        if not path.is_dir():
            return f"Directory not found: {path}", False
        entries = []
        for entry in sorted(path.iterdir())[:250]:
            suffix = "/" if entry.is_dir() else ""
            entries.append(f"{entry.name}{suffix}")
        return "\n".join(entries) or "(empty)", True

    def _tool_clipboard_read(self, _args: dict[str, Any]) -> tuple[str, bool]:
        if sys.platform == "darwin":
            cmd = ["pbpaste"]
        elif os.name == "nt":
            cmd = ["powershell", "-NoProfile", "-Command", "Get-Clipboard"]
        else:
            tool = shutil.which("wl-paste") or shutil.which("xclip")
            if not tool:
                return "No clipboard tool is available.", False
            cmd = [tool, "-selection", "clipboard", "-o"] if tool.endswith("xclip") else [tool]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return _format_result(result), result.returncode == 0

    def _tool_clipboard_write(self, args: dict[str, Any]) -> tuple[str, bool]:
        text = str(args.get("text", ""))
        if sys.platform == "darwin":
            cmd = ["pbcopy"]
        elif os.name == "nt":
            cmd = [
                "powershell",
                "-NoProfile",
                "-Command",
                "Set-Clipboard -Value ([Console]::In.ReadToEnd())",
            ]
        else:
            tool = shutil.which("wl-copy") or shutil.which("xclip")
            if not tool:
                return "No clipboard tool is available.", False
            cmd = [tool, "-selection", "clipboard"] if tool.endswith("xclip") else [tool]
        result = subprocess.run(
            cmd,
            input=text,
            text=True,
            capture_output=True,
            timeout=10,
        )
        if result.returncode == 0:
            return "Copied to clipboard.", True
        return _format_result(result), False

    def _tool_wait(self, args: dict[str, Any]) -> tuple[str, bool]:
        seconds = min(30.0, max(0.0, float(args.get("seconds", 1))))
        time.sleep(seconds)
        return f"Waited {seconds:.1f}s.", True


def _ffmpeg_path() -> str | None:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg")


def _format_result(result: subprocess.CompletedProcess) -> str:
    parts = []
    if result.stdout:
        parts.append(result.stdout.strip())
    if result.stderr:
        parts.append(result.stderr.strip())
    output = "\n".join(part for part in parts if part).strip()
    if not output:
        output = f"(no output, exit {result.returncode})"
    if len(output) > MAX_OUTPUT:
        output = output[:MAX_OUTPUT] + "\n… output truncated"
    return output


def _split_args(value: str) -> list[str]:
    import shlex

    try:
        return shlex.split(value)
    except ValueError:
        return value.split()


def _trim(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    """Keep the opening instructions plus the most recent exchanges."""
    if not messages:
        return messages
    head = messages[0]
    tail = messages[1:]
    while tail and sum(len(item["content"]) for item in tail) > MAX_PROMPT_CHARS:
        tail = tail[2:] if len(tail) > 2 else tail[1:]
    return [head, *tail]


def datetime_stamp() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y%m%d-%H%M%S")
