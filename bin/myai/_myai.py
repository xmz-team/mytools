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
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple
import argparse
import requests


class DeepSeekChat:
    def __init__(self, api_key: str, base_url: str = "https://api.deepseek.com", model: str = "deepseek-flash"):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.data_dir = Path.home() / ".my" / "deepseek" / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.current_conv_id: Optional[str] = None
        self.thinking_enabled = False  # default: thinking OFF
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
        pass  # conversations are discovered from directory listing

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
        """Upload image to DeepSeek Files API, return file_id."""
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

        # Try Files API for image types
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
        """Return either plain string or list-of-blocks for chat completions."""
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
                # fallback: base64 inline
                b64 = base64.b64encode(Path(info["file_path"]).read_bytes()).decode()
                blocks.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{b64}"}
                })
            else:
                # text file: read as text and embed
                try:
                    txt = Path(info["file_path"]).read_text(encoding="utf-8", errors="replace")
                except Exception:
                    txt = "[binary file]"
                blocks.append({"type": "text", "text": f"\n--- File: {info['original_name']} ---\n{txt}\n"})
        return blocks

    # ---------- chat ----------
    def _post_chat(self, messages: List[Dict], stream: bool = True) -> Tuple[str, Dict]:
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
                        delta = obj["choices"][0].get("delta", {})
                        piece = delta.get("content", "")
                        if piece:
                            print(piece, end="", flush=True)
                            full += piece
                    except Exception:
                        pass
            print()
            return full, {}
        else:
            obj = resp.json()
            return obj["choices"][0]["message"]["content"], obj.get("usage", {})

    def chat(self, text: str, file_paths: Optional[List[str]] = None,
             conv_id: Optional[str] = None, stream: bool = True) -> str:
        # ensure conversation
        if conv_id is None:
            conv_id = str(uuid.uuid4())
        self._init_db(conv_id)
        history = self._load_history(conv_id)

        content = self._build_content_with_files(text or "", file_paths or [], conv_id)

        # append user message
        if content:
            history.append({"role": "user", "content": content})

        # loop until non-empty assistant reply
        while True:
            reply, usage = self._post_chat(history, stream=stream)
            if reply.strip():
                break
            print("[empty reply, retrying...]")

        history.append({"role": "assistant", "content": reply})
        self._save_history(conv_id, history)
        self.current_conv_id = conv_id
        return reply

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
            if reply.strip():
                break
            print("[empty reply, retrying...]")
        history.append({"role": "assistant", "content": reply})
        self._save_history(conv_id, history)
        return reply

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
            content = msg["content"]
            if isinstance(content, list):
                preview = "[multi-modal content]"
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


# ---------- interactive ----------
def interactive_mode(client: DeepSeekChat):
    print("DeepSeek Chat — type :help for commands, :exit to quit")
    current_conv = None
    pending_files: List[str] = []

    while True:
        try:
            prompt = f"\n[{'new' if not current_conv else current_conv}]"
            if pending_files:
                prompt += f" [files: {', '.join(Path(f).name for f in pending_files)}]"
            prompt += " > "
            line = input(prompt).strip()
            if not line:
                continue

            # ---- commands ----
            if line == ":exit":
                print("Bye.")
                break
            if line == ":help":
                print_help()
                continue
            if line == ":new":
                current_conv = None
                pending_files.clear()
                print("New conversation.")
                continue
            if line == ":list":
                client.list_conversations()
                continue
            if line.startswith(":show "):
                client.show_conversation(line[6:].strip())
                continue
            if line.startswith(":use "):
                current_conv = line[5:].strip()
                pending_files.clear()
                print(f"Switched to {current_conv}")
                continue
            if line == ":messages":
                if not current_conv:
                    print("No active conversation.")
                    continue
                h = client._load_history(current_conv)
                for i, m in enumerate(h):
                    role = "U" if m["role"] == "user" else "A"
                    c = m["content"]
                    prev = c[:80] if isinstance(c, str) else "[blocks]"
                    print(f"[{i}] {role}: {prev}")
                continue
            if line == ":on-think":
                client.thinking_enabled = True
                print("Thinking mode ON.")
                continue
            if line == ":off-think":
                client.thinking_enabled = False
                print("Thinking mode OFF.")
                continue
            if line == ":look-token":
                client.look_token()
                continue
            if line.startswith(":file "):
                fp = line[6:].strip()
                if fp:
                    pending_files.append(fp)
                    print(f"Added pending file: {Path(fp).name}")
                continue
            if line == ":show-files":
                if pending_files:
                    for i, f in enumerate(pending_files):
                        print(f"  {i}. {f}")
                else:
                    print("No pending files.")
                continue
            if line.startswith(":remove-file "):
                try:
                    idx = int(line.split()[-1])
                    removed = pending_files.pop(idx)
                    print(f"Removed: {Path(removed).name}")
                except (ValueError, IndexError):
                    print("Usage: :remove-file <index>")
                continue
            if line == ":clear-files":
                pending_files.clear()
                print("Pending files cleared.")
                continue
            if line == ":retry" or line.startswith(":retry "):
                if not current_conv:
                    print("No active conversation.")
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
                        continue
                else:
                    try:
                        idx = int(line.split()[1])
                    except (ValueError, IndexError):
                        print("Usage: :retry <index>")
                        continue
                print(f"Retrying message [{idx}]...")
                client.retry(current_conv, idx)
                continue
            if line == ":delete" or line.startswith(":delete "):
                if line == ":delete":
                    if not current_conv:
                        print("No active conversation.")
                        continue
                    client.delete_conversation(current_conv)
                    current_conv = None
                else:
                    cid = line[8:].strip()
                    client.delete_conversation(cid)
                    if current_conv == cid:
                        current_conv = None
                continue

            # ---- normal message ----
            reply = client.chat(
                text=line,
                file_paths=pending_files.copy(),
                conv_id=current_conv,
                stream=True,
            )
            pending_files.clear()
            if current_conv is None and client.current_conv_id:
                current_conv = client.current_conv_id
        except KeyboardInterrupt:
            print("\nBye.")
            break
        except Exception as e:
            print(f"Error: {e}")


def print_help():
    print("""
Commands:
  :help                 show this help
  :exit                 quit
  :new                  start a new conversation
  :list                 list conversations
  :show <id>            show conversation details
  :use <id>             switch to conversation
  :messages             show message indices of current conversation
  :retry [index]        retry a message (default: last user)
  :delete [id]          delete conversation (default: current)
  :on-think             enable thinking mode
  :off-think            disable thinking mode (default)
  :look-token           query account balance
  :file <path>          add a file to pending list
  :show-files           list pending files
  :remove-file <idx>    remove a pending file
  :clear-files          clear pending files

Type plain text to chat. Pending files are attached to the next message.
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
