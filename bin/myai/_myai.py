#!/usr/bin/env python3
# _myai.py

import os
import json
import sqlite3
import hashlib
import time
import sys
import shutil
import uuid
import mimetypes
import base64
import subprocess
import re
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple
import argparse
import requests


# ---------- permission helpers ----------
class PermissionManager:
    """Manages which paths the AI may read/write for a given conversation."""

    def __init__(self, data_dir: Path, conv_id: str):
        self.data_dir = data_dir
        self.conv_id = conv_id
        self.path = data_dir / conv_id / "allowed.list.json"
        self._ensure_file()

    def _ensure_file(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text(json.dumps({}, ensure_ascii=False, indent=2), encoding="utf-8")

    def _load(self) -> Dict[str, Any]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save(self, data: Dict[str, Any]):
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _norm(path: str) -> str:
        return str(Path(path).expanduser().resolve())

    def chmod(self, path: str, read: Optional[bool], write: Optional[bool], recursive: bool = False):
        key = self._norm(path)
        data = self._load()
        entry = data.get(key, {"read": False, "write": False, "recursive": recursive})
        if read is not None:
            entry["read"] = read
        if write is not None:
            entry["write"] = write
        entry["recursive"] = recursive
        data[key] = entry
        self._save(data)
        return key, entry

    def _matches(self, target: str, entry_key: str, recursive: bool) -> bool:
        """Does target path fall under entry_key (respecting recursive)?"""
        target_p = Path(target)
        base = Path(entry_key)
        try:
            if recursive:
                target_p.relative_to(base)
                return True
            return target_p == base or target_p.parent == base or base in target_p.parents
        except ValueError:
            return False

    def can_read(self, path: str) -> bool:
        target = self._norm(path)
        for key, entry in self._load().items():
            if self._matches(target, key, entry.get("recursive", False)):
                return bool(entry.get("read", False))
        return False

    def can_write(self, path: str) -> bool:
        target = self._norm(path)
        for key, entry in self._load().items():
            if self._matches(target, key, entry.get("recursive", False)):
                return bool(entry.get("write", False))
        return False

    def list_entries(self) -> Dict[str, Any]:
        return self._load()


# ---------- tool definitions ----------
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Execute a shell command on the local machine and return stdout/stderr. "
                           "Only use for commands needed to accomplish the user's task.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command to run, e.g. 'ls -l'."}
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a text file from disk. Requires read permission.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write (overwrite) a text file on disk. Requires write permission.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List directory contents. Requires read permission.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web for up-to-date information. Only available when network search is enabled.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
]


class DeepSeekChat:
    def __init__(self, api_key: str, base_url: str = "https://api.deepseek.com", model: str = "deepseek-flash"):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.data_dir = Path.home() / ".my" / "deepseek" / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.current_conv_id: Optional[str] = None
        self.thinking_enabled = False
        self.network_enabled = False
        self._perm: Optional[PermissionManager] = None
        self._load_conv_index()

    # ---------- per-conversation storage ----------
    def _conv_dir(self, conv_id: str) -> Path:
        d = self.data_dir / conv_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "upload").mkdir(exist_ok=True)
        return d

    def _db_path(self, conv_id: str) -> Path:
        return self._conv_dir(conv_id) / "file.db"

    def _history_path(self, conv_id: str) -> Path:
        return self._conv_dir(conv_id) / "history.json"

    def _init_db(self, conv_id: str):
        conn = sqlite3.connect(str(self._db_path(conv_id)))
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                original_name TEXT NOT NULL,
                stored_name TEXT NOT NULL,
                file_path TEXT NOT NULL,
                file_size INTEGER,
                mime_type TEXT,
                file_id TEXT,
                uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        conn.close()

    def _load_conv_index(self):
        pass

    def _load_history(self, conv_id: str) -> List[Dict[str, Any]]:
        p = self._history_path(conv_id)
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return []
        return []

    def _save_history(self, conv_id: str, history: List[Dict[str, Any]]):
        self._history_path(conv_id).write_text(
            json.dumps(history, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

    def _list_conversation_ids(self) -> List[str]:
        ids = []
        if self.data_dir.exists():
            for d in sorted(self.data_dir.iterdir()):
                if d.is_dir() and (d / "history.json").exists():
                    ids.append(d.name)
        return ids

    def _get_perm(self, conv_id: str) -> PermissionManager:
        if self._perm is None or self._perm.conv_id != conv_id:
            self._perm = PermissionManager(self.data_dir, conv_id)
        return self._perm

    # ---------- file helpers ----------
    def _sha256(self, path: Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()

    def _mime(self, path: Path) -> str:
        mt, _ = mimetypes.guess_type(str(path))
        return mt or "application/octet-stream"

    def _upload_via_api(self, path: Path) -> Optional[str]:
        url = f"{self.base_url}/files"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        with open(path, "rb") as f:
            files = {"file": (path.name, f, self._mime(path))}
            data = {"purpose": "user_data"}
            try:
                r = requests.post(url, headers=headers, files=files, data=data, timeout=120)
                r.raise_for_status()
                return r.json().get("id")
            except requests.exceptions.RequestException as e:
                print(f"[warn] Files API upload failed: {e}")
                return None

    def upload_file(self, file_path: str, conv_id: str) -> Dict[str, Any]:
        src = Path(file_path).expanduser().resolve()
        if not src.exists() or not src.is_file():
            raise FileNotFoundError(f"not a file: {src}")
        size = src.stat().st_size
        checksum = self._sha256(src)
        mime = self._mime(src)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        stored = f"{src.stem}_{ts}{src.suffix}"
        dest = self._conv_dir(conv_id) / "upload" / stored
        shutil.copy2(src, dest)

        api_file_id = None
        if mime.startswith("image/") and mime in ("image/jpeg", "image/png", "image/gif", "image/webp"):
            api_file_id = self._upload_via_api(dest)

        conn = sqlite3.connect(str(self._db_path(conv_id)))
        c = conn.cursor()
        c.execute("""
            INSERT INTO files (original_name, stored_name, file_path, file_size, mime_type, file_id)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (src.name, stored, str(dest), size, mime, api_file_id))
        fid = c.lastrowid
        conn.commit()
        conn.close()
        return {
            "id": fid,
            "original_name": src.name,
            "stored_name": stored,
            "file_path": str(dest),
            "file_size": size,
            "mime_type": mime,
            "api_file_id": api_file_id,
        }

    def _build_content_with_files(self, text: str, file_paths: List[str], conv_id: str) -> Any:
        if not file_paths:
            return text
        blocks = []
        if text:
            blocks.append({"type": "text", "text": text})
        for fp in file_paths:
            info = self.upload_file(fp, conv_id)
            mime = info["mime_type"]
            if info["api_file_id"] and mime.startswith("image/"):
                blocks.append({"type": "file", "file_id": info["api_file_id"]})
            elif mime.startswith("image/"):
                b64 = base64.b64encode(Path(info["file_path"]).read_bytes()).decode()
                blocks.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{b64}"}
                })
            else:
                try:
                    txt = Path(info["file_path"]).read_text(encoding="utf-8", errors="replace")
                except Exception:
                    txt = "[binary file]"
                blocks.append({"type": "text", "text": f"\n--- File: {info['original_name']} ---\n{txt}\n"})
        return blocks

    # ---------- tool execution ----------
    def _exec_tool(self, name: str, args: Dict[str, Any], conv_id: str) -> str:
        perm = self._get_perm(conv_id)
        if name == "run_command":
            cmd = args.get("command", "").strip()
            if not cmd:
                return "[error] empty command"
            # Show the command being run
            print(f"\n[run {cmd}]")
            # Best-effort permission check: extract paths in the command
            blocked = self._command_blocked_by_perm(cmd, perm)
            if blocked:
                return f"[error] permission denied: {blocked}"
            try:
                proc = subprocess.run(
                    cmd, shell=True, capture_output=True, text=True, timeout=120
                )
                out = proc.stdout or ""
                err = proc.stderr or ""
                res = out + (("\n[stderr]\n" + err) if err else "")
                # Print the result next to "AI >:"
                preview = res.strip()
                if preview:
                    print(f"AI >: {preview}")
                return res if res.strip() else "[no output]"
            except subprocess.TimeoutExpired:
                return "[error] command timed out"
            except Exception as e:
                return f"[error] {e}"

        if name == "read_file":
            path = args.get("path", "")
            if not perm.can_read(path):
                return f"[error] no read permission for {path}"
            try:
                return Path(path).read_text(encoding="utf-8", errors="replace")[:20000]
            except Exception as e:
                return f"[error] {e}"

        if name == "write_file":
            path = args.get("path", "")
            content = args.get("content", "")
            if not perm.can_write(path):
                return f"[error] no write permission for {path}"
            try:
                Path(path).write_text(content, encoding="utf-8")
                return f"[ok] wrote {len(content)} bytes to {path}"
            except Exception as e:
                return f"[error] {e}"

        if name == "list_dir":
            path = args.get("path", ".")
            if not perm.can_read(path):
                return f"[error] no read permission for {path}"
            try:
                items = []
                for p in sorted(Path(path).iterdir()):
                    items.append(p.name + ("/" if p.is_dir() else ""))
                return "\n".join(items) if items else "[empty]"
            except Exception as e:
                return f"[error] {e}"

        if name == "web_search":
            if not self.network_enabled:
                return "[error] network search is disabled; use :on-network to enable"
            return self._web_search(args.get("query", ""))

        return f"[error] unknown tool: {name}"

    def _command_blocked_by_perm(self, cmd: str, perm: PermissionManager) -> Optional[str]:
        """Very rough heuristic: check for write operators (>, >>, rm, mv, mkdir, touch)
        referencing paths that lack write permission."""
        write_indicators = re.findall(r'(?:>{1,2}|\b(?:rm|mv|cp|mkdir|touch|tee)\b)\s+([^\s;|&]+)', cmd)
        for target in write_indicators:
            if target.startswith("-"):
                continue
            if not perm.can_write(target):
                return f"write not allowed: {target}"
        # read indicators: cat/less/head/tail/grep <path>
        read_indicators = re.findall(r'\b(?:cat|less|head|tail|grep|wc|awk|sed)\b[^|;]*?\s([^\s;|&]+)', cmd)
        for target in read_indicators:
            if target.startswith("-") or target in ("|", ">"):
                continue
            p = Path(target)
            if not p.exists():
                continue
            if not perm.can_read(target):
                return f"read not allowed: {target}"
        return None

    def _web_search(self, query: str) -> str:
        """Simple web search via DuckDuckGo HTML endpoint (no API key needed)."""
        try:
            r = requests.get(
                "https://duckduckgo.com/html/",
                params={"q": query},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=20,
            )
            r.raise_for_status()
            text = r.text
            # Extract result snippets crudely
            results = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', text, re.S)
            titles = re.findall(r'class="result__a"[^>]*>(.*?)</a>', text, re.S)
            def clean(s):
                return re.sub(r"<[^>]+>", "", s).strip()
            out = []
            for i in range(min(5, len(results))):
                t = clean(titles[i]) if i < len(titles) else ""
                s = clean(results[i]) if i < len(results) else ""
                out.append(f"{i+1}. {t}\n   {s}")
            return "\n\n".join(out) if out else "[no results]"
        except Exception as e:
            return f"[error] web search failed: {e}"

    # ---------- chat ----------
    def _post_chat(self, messages: List[Dict], stream: bool = True,
                   tools: Optional[List[Dict]] = None) -> Tuple[Dict, Dict]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": stream,
            "thinking": {"type": "enabled" if self.thinking_enabled else "disabled"},
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        resp = requests.post(
            f"{self.base_url}/chat/completions",
            headers=headers,
            json=payload,
            stream=stream,
            timeout=300,
        )
        resp.raise_for_status()

        if stream:
            full = ""
            tool_calls: Dict[int, Dict] = {}
            for line in resp.iter_lines():
                if not line:
                    continue
                line = line.decode("utf-8")
                if line.startswith("data: "):
                    data = line[6:]
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                        choice = obj["choices"][0]
                        delta = choice.get("delta", {})
                        piece = delta.get("content", "")
                        if piece:
                            print(piece, end="", flush=True)
                            full += piece
                        if "tool_calls" in delta:
                            for tc in delta["tool_calls"]:
                                idx = tc.get("index", 0)
                                if idx not in tool_calls:
                                    tool_calls[idx] = {"id": "", "type": "function",
                                                       "function": {"name": "", "arguments": ""}}
                                if tc.get("id"):
                                    tool_calls[idx]["id"] = tc["id"]
                                fn = tc.get("function", {})
                                if fn.get("name"):
                                    tool_calls[idx]["function"]["name"] = fn["name"]
                                if fn.get("arguments"):
                                    tool_calls[idx]["function"]["arguments"] += fn["arguments"]
                    except Exception:
                        pass
            print()
            return {"content": full, "tool_calls": [tool_calls[k] for k in sorted(tool_calls)]}, {}
        else:
            obj = resp.json()
            msg = obj["choices"][0]["message"]
            return msg, obj.get("usage", {})

    def chat(self, text: str, file_paths: Optional[List[str]] = None,
             conv_id: Optional[str] = None, stream: bool = True) -> str:
        if conv_id is None:
            conv_id = str(uuid.uuid4())
        self._init_db(conv_id)
        self._get_perm(conv_id)  # ensure allowed.list.json exists
        history = self._load_history(conv_id)

        content = self._build_content_with_files(text or "", file_paths or [], conv_id)
        if content:
            history.append({"role": "user", "content": content})

        # Build active tool list (network gated)
        active_tools = [t for t in TOOLS
                        if t["function"]["name"] != "web_search" or self.network_enabled]

        # Agent loop: keep calling model until it stops requesting tools
        max_iters = 12
        for _ in range(max_iters):
            msg, usage = self._post_chat(history, stream=stream, tools=active_tools)
            tool_calls = msg.get("tool_calls") or []
            reply_text = msg.get("content") or ""

            if tool_calls:
                # Record assistant tool-call message
                history.append({
                    "role": "assistant",
                    "content": reply_text or None,
                    "tool_calls": tool_calls,
                })
                for tc in tool_calls:
                    fn = tc["function"]
                    name = fn["name"]
                    try:
                        args = json.loads(fn["arguments"] or "{}")
                    except Exception:
                        args = {}
                    result = self._exec_tool(name, args, conv_id)
                    history.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": result,
                    })
                # loop continues to let model react to tool results
                continue

            if reply_text.strip():
                history.append({"role": "assistant", "content": reply_text})
                self._save_history(conv_id, history)
                self.current_conv_id = conv_id
                return reply_text
            else:
                # empty reply retry
                print("[empty reply, retrying...]")
                continue

        # ran out of iterations
        self._save_history(conv_id, history)
        return "[max tool iterations reached]"

    def retry(self, conv_id: str, msg_index: int, stream: bool = True) -> str:
        history = self._load_history(conv_id)
        if msg_index < 0 or msg_index >= len(history):
            print(f"Index out of range (0-{len(history)-1})")
            return ""
        target = history[msg_index]
        if target["role"] == "user":
            prefix = history[:msg_index]
            history = prefix + [target]
        elif target["role"] == "assistant":
            if msg_index == 0 or history[msg_index - 1]["role"] != "user":
                print("Cannot retry an assistant message without preceding user message")
                return ""
            history = history[:msg_index]
        else:
            print(f"Unsupported role: {target['role']}")
            return ""

        self._save_history(conv_id, history)
        while True:
            reply, _ = self._post_chat(history, stream=stream)
            if (reply.get("content") or "").strip():
                break
            print("[empty reply, retrying...]")
        history.append({"role": "assistant", "content": reply["content"]})
        self._save_history(conv_id, history)
        return reply["content"]

    # ---------- token / balance ----------
    def look_token(self) -> Dict[str, Any]:
        url = f"{self.base_url}/user/balance"
        headers = {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}
        try:
            r = requests.get(url, headers=headers, timeout=30)
            r.raise_for_status()
            data = r.json()
            print(f"\nBalance available: {data.get('is_available')}")
            for b in data.get("balance_infos", []):
                print(f"  {b.get('currency')}: total={b.get('total_balance')} granted={b.get('granted_balance')} topped={b.get('topped_up_balance')}")
            return data
        except requests.exceptions.RequestException as e:
            print(f"Failed to query balance: {e}")
            return {"error": str(e)}

    # ---------- conversation management ----------
    def list_conversations(self):
        ids = self._list_conversation_ids()
        if not ids:
            print("No conversations.")
            return
        print(f"\n{'ID':<40} {'Messages':<10} {'Last updated':<25}")
        print("-" * 80)
        for cid in ids:
            h = self._load_history(cid)
            n = len(h)
            mtime = datetime.fromtimestamp(self._history_path(cid).stat().st_mtime).isoformat()[:19]
            print(f"{cid:<40} {n:<10} {mtime:<25}")

    def show_conversation(self, conv_id: str):
        h = self._load_history(conv_id)
        if not h:
            print(f"Conversation {conv_id} not found or empty.")
            return
        print(f"\nConversation: {conv_id}\n" + "-" * 70)
        for i, msg in enumerate(h):
            role = "User" if msg["role"] == "user" else "AI"
            content = msg.get("content")
            if isinstance(content, list):
                preview = "[multi-modal content]"
            elif content is None:
                preview = "[tool call]"
            else:
                preview = content[:300] + ("..." if len(content) > 300 else "")
            print(f"[{i}] {role}: {preview}")
            print("-" * 40)

    def delete_conversation(self, conv_id: str):
        d = self.data_dir / conv_id
        if d.exists():
            shutil.rmtree(d)
            print(f"Deleted: {conv_id}")
        else:
            print(f"Not found: {conv_id}")


# ---------- status bar ----------
def status_bar(client: DeepSeekChat) -> str:
    net = "YES" if client.network_enabled else "NO"
    think = "YES" if client.thinking_enabled else "NO"
    return f"[Online Search: {net}] [Deep Thinking: {think}]"


# ---------- interactive ----------
def interactive_mode(client: DeepSeekChat):
    print("DeepSeek Chat — type :help for commands, :exit to quit")
    print(status_bar(client))
    current_conv = None
    pending_files: List[str] = []

    while True:
        try:
            prompt = f"\n[{'new' if not current_conv else current_conv}]"
            if pending_files:
                prompt += f" [files: {', '.join(Path(f).name for f in pending_files)}]"
            prompt += " > "
            line = input(prompt).strip()

            # Always render status bar at bottom after any input
            if not line:
                print(status_bar(client))
                continue

            # ---- commands ----
            if line == ":exit":
                print("Bye.")
                break
            if line == ":help":
                print_help()
                print(status_bar(client))
                continue
            if line == ":new":
                current_conv = None
                pending_files.clear()
                print("New conversation.")
                print(status_bar(client))
                continue
            if line == ":list":
                client.list_conversations()
                print(status_bar(client))
                continue
            if line.startswith(":show "):
                client.show_conversation(line[6:].strip())
                print(status_bar(client))
                continue
            if line.startswith(":use "):
                current_conv = line[5:].strip()
                pending_files.clear()
                client._get_perm(current_conv)  # ensure permissions file
                print(f"Switched to {current_conv}")
                print(status_bar(client))
                continue
            if line == ":messages":
                if not current_conv:
                    print("No active conversation.")
                    print(status_bar(client))
                    continue
                h = client._load_history(current_conv)
                for i, m in enumerate(h):
                    role = "U" if m["role"] == "user" else ("T" if m["role"] == "tool" else "A")
                    c = m.get("content")
                    prev = c[:80] if isinstance(c, str) else ("[tool call]" if c is None else "[blocks]")
                    print(f"[{i}] {role}: {prev}")
                print(status_bar(client))
                continue
            if line == ":on-think":
                client.thinking_enabled = True
                print("Thinking mode ON.")
                print(status_bar(client))
                continue
            if line == ":off-think":
                client.thinking_enabled = False
                print("Thinking mode OFF.")
                print(status_bar(client))
                continue
            if line == ":on-network":
                client.network_enabled = True
                print("Network search ON.")
                print(status_bar(client))
                continue
            if line == ":off-network":
                client.network_enabled = False
                print("Network search OFF.")
                print(status_bar(client))
                continue
            if line == ":look-token":
                client.look_token()
                print(status_bar(client))
                continue
            if line.startswith(":file "):
                fp = line[6:].strip()
                if fp:
                    pending_files.append(fp)
                    print(f"Added pending file: {Path(fp).name}")
                print(status_bar(client))
                continue
            if line == ":show-files":
                if pending_files:
                    for i, f in enumerate(pending_files):
                        print(f"  {i}. {f}")
                else:
                    print("No pending files.")
                print(status_bar(client))
                continue
            if line.startswith(":remove-file "):
                try:
                    idx = int(line.split()[-1])
                    removed = pending_files.pop(idx)
                    print(f"Removed: {Path(removed).name}")
                except (ValueError, IndexError):
                    print("Usage: :remove-file <index>")
                print(status_bar(client))
                continue
            if line == ":clear-files":
                pending_files.clear()
                print("Pending files cleared.")
                print(status_bar(client))
                continue

            # ---- :chmod command ----
            if line.startswith(":chmod"):
                if not current_conv:
                    # create a conversation lazily so permission file exists
                    current_conv = str(uuid.uuid4())
                    client._get_perm(current_conv)
                handle_chmod(client, current_conv, line)
                print(status_bar(client))
                continue

            if line == ":retry" or line.startswith(":retry "):
                if not current_conv:
                    print("No active conversation.")
                    print(status_bar(client))
                    continue
                if line == ":retry":
                    h = client._load_history(current_conv)
                    idx = None
                    for i in range(len(h) - 1, -1, -1):
                        if h[i]["role"] == "user":
                            idx = i
                            break
                    if idx is None:
                        print("No user message to retry.")
                        print(status_bar(client))
                        continue
                else:
                    try:
                        idx = int(line.split()[1])
                    except (ValueError, IndexError):
                        print("Usage: :retry <index>")
                        print(status_bar(client))
                        continue
                print(f"Retrying message [{idx}]...")
                client.retry(current_conv, idx)
                print(status_bar(client))
                continue
            if line == ":delete" or line.startswith(":delete "):
                if line == ":delete":
                    if not current_conv:
                        print("No active conversation.")
                        print(status_bar(client))
                        continue
                    client.delete_conversation(current_conv)
                    current_conv = None
                else:
                    cid = line[8:].strip()
                    client.delete_conversation(cid)
                    if current_conv == cid:
                        current_conv = None
                print(status_bar(client))
                continue

            # ---- normal message ----
            if current_conv is None:
                current_conv = str(uuid.uuid4())
            reply = client.chat(
                text=line,
                file_paths=pending_files.copy(),
                conv_id=current_conv,
                stream=True,
            )
            pending_files.clear()
            if client.current_conv_id:
                current_conv = client.current_conv_id
            print(status_bar(client))
        except KeyboardInterrupt:
            print("\nBye.")
            break
        except Exception as e:
            print(f"Error: {e}")
            print(status_bar(client))


def handle_chmod(client: DeepSeekChat, conv_id: str, line: str):
    """
    Syntax:
      :chmod [-R] read|write|no-read|no-write|all|no-all <path>
    """
    parts = line.split()
    recursive = False
    if len(parts) >= 2 and parts[1] == "-R":
        recursive = True
        parts = [parts[0]] + parts[2:]
    if len(parts) < 3:
        print("Usage: :chmod [-R] read|write|no-read|no-write|all|no-all <path>")
        return
    action = parts[1]
    path = " ".join(parts[2:]).strip()
    perm = client._get_perm(conv_id)

    if action == "read":
        key, entry = perm.chmod(path, read=True, write=None, recursive=recursive)
    elif action == "no-read":
        key, entry = perm.chmod(path, read=False, write=None, recursive=recursive)
    elif action == "write":
        key, entry = perm.chmod(path, read=None, write=True, recursive=recursive)
    elif action == "no-write":
        key, entry = perm.chmod(path, read=None, write=False, recursive=recursive)
    elif action == "all":
        key, entry = perm.chmod(path, read=True, write=True, recursive=recursive)
    elif action == "no-all":
        key, entry = perm.chmod(path, read=False, write=False, recursive=recursive)
    else:
        print(f"Unknown chmod action: {action}")
        return

    r = "read" if entry.get("read") else "no-read"
    w = "write" if entry.get("write") else "no-write"
    print(f"Permissions for {key} (recursive={entry.get('recursive')}): {r}, {w}")


def print_help():
    print("""
Commands:
  :help                     show this help
  :exit                     quit
  :new                      start a new conversation
  :list                     list conversations
  :show <id>                show conversation details
  :use <id>                 switch to conversation
  :messages                 show message indices of current conversation
  :retry [index]            retry a message (default: last user)
  :delete [id]              delete conversation (default: current)
  :on-think                 enable thinking mode
  :off-think                disable thinking mode (default)
  :on-network               enable network search
  :off-network              disable network search (default)
  :look-token               query account balance
  :file <path>              add a file to pending list
  :show-files               list pending files
  :remove-file <idx>        remove a pending file
  :clear-files              clear pending files

Permissions (per conversation, stored in allowed.list.json):
  :chmod read <path>           grant read
  :chmod no-read <path>        revoke read
  :chmod write <path>          grant write
  :chmod no-write <path>       revoke write
  :chmod all <path>            grant read + write
  :chmod no-all <path>         revoke read + write
  :chmod -R all <path>         grant read + write recursively
  :chmod -R no-all <path>      revoke read + write recursively

Type plain text to chat. Pending files are attached to the next message.
The AI can call tools: run_command, read_file, write_file, list_dir, web_search.
""")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-key", default=os.environ.get("DEEPSEEK_API_KEY"))
    parser.add_argument("--base-url", default=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"))
    parser.add_argument("--model", default=os.environ.get("DEEPSEEK_MODEL", "deepseek-flash"))
    args = parser.parse_args()

    if not args.api_key:
        print("Error: DEEPSEEK_API_KEY not set.")
        sys.exit(1)

    client = DeepSeekChat(args.api_key, args.base_url, args.model)
    interactive_mode(client)


if __name__ == "__main__":
    main()
