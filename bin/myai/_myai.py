#!/usr/bin/env python3

import os
import json
import sqlite3
import hashlib
import time
import sys
import shutil
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict, Any
import argparse
import requests

class DeepSeekChat:
    def __init__(self, api_key: str, base_url: str = "https://api.deepseek.com/v1"):
        """
        Initialize the DeepSeek client
        Args:
          api_key: DeepSeek API key
          base_url: API base URL
        """
        self.api_key = api_key
        self.base_url = base_url
        self.data_dir = Path.home() / '.my' / 'deepseek'
        self.history_file = self.data_dir / 'history.json'
        self.db_file = self.data_dir / 'file.db'
        self.upload_dir = self.data_dir / 'uploads'
        self._setup_directories()
        self._init_database()
        self.history = self._load_history()

    def _setup_directories(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.upload_dir.mkdir(parents=True, exist_ok=True)

    def _init_database(self):
        conn = sqlite3.connect(str(self.db_file))
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                original_name TEXT NOT NULL,
                stored_name TEXT NOT NULL,
                file_path TEXT NOT NULL,
                file_size INTEGER,
                mime_type TEXT,
                checksum TEXT,
                conversation_id TEXT,
                uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_conversation_id 
            ON files(conversation_id)
        ''')
        conn.commit()
        conn.close()

    def _load_history(self) -> List[Dict[str, Any]]:
        if self.history_file.exists():
            try:
                with open(self.history_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                print(f"Failed to load history: {e}")
                return []
        return []

    def _save_history(self):
        try:
            with open(self.history_file, 'w', encoding='utf-8') as f:
                json.dump(self.history, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Failed to save the history: {e}")

    def _calculate_checksum(self, file_path: str) -> str:
        sha256_hash = hashlib.sha256()
        with open(file_path, "rb") as f:
            for byte_block in iter(lambda: f.read(4096), b""):
                sha256_hash.update(byte_block)
        return sha256_hash.hexdigest()

    def _get_mime_type(self, file_path: str) -> str:
        import mimetypes
        mime_type, _ = mimetypes.guess_type(file_path)
        if mime_type:
            return mime_type
        return 'application/octet-stream'

    def upload_file(self, file_path: str, conversation_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Upload the file and record it to the database
        Args:
            file_path: The file path to be uploaded
            conversation_id: Associated conversation ID
        Returns: File Info Dictionary
        """
        file_path = Path(file_path)
        if not file_path.exists():
            raise FileNotFoundError(f"The file does not exist: {file_path}")
        if not file_path.is_file():
            raise ValueError(f"The path is not a file: {file_path}")
        file_size = file_path.stat().st_size
        checksum = self._calculate_checksum(str(file_path))
        mime_type = self._get_mime_type(str(file_path))
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        original_name = file_path.name
        if '.' in original_name:
            name, ext = original_name.rsplit('.', 1)
            stored_name = f"{name}_{timestamp}.{ext}"
        else:
            stored_name = f"{original_name}_{timestamp}"
        dest_path = self.upload_dir / stored_name
        shutil.copy2(str(file_path), str(dest_path))
        conn = sqlite3.connect(str(self.db_file))
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO files (original_name, stored_name, file_path, file_size, mime_type, checksum, conversation_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (original_name, stored_name, str(dest_path), file_size, mime_type, checksum, conversation_id))
        file_id = cursor.lastrowid
        conn.commit()
        conn.close()
        file_info = {
            'id': file_id,
            'original_name': original_name,
            'stored_name': stored_name,
            'file_path': str(dest_path),
            'file_size': file_size,
            'mime_type': mime_type,
            'checksum': checksum,
            'conversation_id': conversation_id
        }
        print(f"The file has been uploaded: {original_name} -> {stored_name}")
        return file_info

    def _read_file_content(self, file_path: str) -> str:
        file_path = Path(file_path)
        try:
            with open(file_path, 'rb') as f:
                raw_bytes = f.read()
            return raw_bytes.decode('latin-1')
        except Exception as e:
            return f"[Failed to read the file: {file_path.name}, error: {str(e)}]"

    def chat(self, message: str, file_paths: Optional[List[str]] = None, 
             conversation_id: Optional[str] = None, stream: bool = True,
             retry_message_index: Optional[int] = None) -> str:
        """
        Send a message to DeepSeek
        Args:
            message: User message
            file_paths: List of file paths
            conversation_id: Conversation ID
            stream: Whether to use streaming output
            retry_message_index: Index of the message to retry (starting from 0), if specified, retry that message
        Returns:
            AI reply content
        """
        if retry_message_index is not None and conversation_id:
            messages = self._get_conversation_messages(conversation_id)
            if retry_message_index < len(messages):
                user_msg = messages[retry_message_index]
                if user_msg.get('role') == 'user':
                    messages = messages[:retry_message_index]
                    self._truncate_conversation(conversation_id, retry_message_index)
                    return self._send_chat_request(
                        message=user_msg['content'],
                        file_paths=file_paths,
                        messages=messages,
                        conversation_id=conversation_id,
                        stream=stream
                    )
                else:
                    print("Error: The specified index is not a user message")
                    return ""
            else:
                print(f"Error: Message index {retry_message_index} is out of range")
                return ""
        return self._send_chat_request(
            message=message,
            file_paths=file_paths,
            messages=None,
            conversation_id=conversation_id,
            stream=stream
        )

    def _send_chat_request(self, message: str, file_paths: Optional[List[str]] = None,
                          messages: Optional[List[Dict[str, str]]] = None,
                          conversation_id: Optional[str] = None,
                          stream: bool = True) -> str:
        content = message
        if file_paths:
            file_contents = []
            for file_path in file_paths:
                try:
                    file_info = self.upload_file(file_path, conversation_id)
                    file_content = self._read_file_content(file_path)
                    file_contents.append(f"File: {file_info['original_name']}\nContent:\n{file_content}")
                except Exception as e:
                    print(f"Failed to process the file {file_path}: {e}")
            if file_contents:
                content = f"{message}\n\n--- Additional file content ---\n" + "\n\n".join(file_contents)
        if messages is None:
            messages = self._get_conversation_messages(conversation_id) if conversation_id else []
        messages.append({"role": "user", "content": content})
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        data = {
            "model": "deepseek-v4-pro",
            "messages": messages,
            "stream": stream
        }
        try:
            response = requests.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=data,
                stream=stream
            )
            response.raise_for_status()
            if stream:
                assistant_message = self._handle_stream_response(response)
            else:
                result = response.json()
                assistant_message = result['choices'][0]['message']['content']
            if conversation_id:
                self._save_conversation(
                    conversation_id=conversation_id,
                    user_message=content,
                    assistant_message=assistant_message,
                    append=True
                )
            else:
                self._save_conversation(
                    conversation_id=None,
                    user_message=content,
                    assistant_message=assistant_message,
                    append=False
                )
            return assistant_message
        except requests.exceptions.RequestException as e:
            error_msg = f"API request failed: {e}"
            print(error_msg)
            return error_msg

    def _truncate_conversation(self, conversation_id: str, keep_until_index: int):
        for conv in self.history:
            if conv.get('id') == conversation_id:
                messages = conv.get('messages', [])
                conv['messages'] = messages[:keep_until_index]
                conv['updated_at'] = datetime.now().isoformat()
                self._save_history()
                break

    def _get_conversation_messages(self, conversation_id: str) -> List[Dict[str, str]]:
        for conv in self.history:
            if conv.get('id') == conversation_id:
                return conv.get('messages', [])
        return []

    def get_message_history(self, conversation_id: str) -> List[Dict[str, str]]:
        return self._get_conversation_messages(conversation_id)

    def get_conversations(self) -> List[Dict[str, Any]]:
        return self.history

    def _handle_stream_response(self, response) -> str:
        full_message = ""
        for line in response.iter_lines():
            if line:
                line = line.decode('utf-8')
                if line.startswith('data: '):
                    data_str = line[6:]
                    if data_str == '[DONE]':
                        break
                    try:
                        data = json.loads(data_str)
                        if 'choices' in data and len(data['choices']) > 0:
                            delta = data['choices'][0].get('delta', {})
                            content = delta.get('content', '')
                            if content:
                                print(content, end='', flush=True)
                                full_message += content
                    except json.JSONDecodeError:
                        continue
        print()
        return full_message

    def _save_conversation(self, conversation_id: Optional[str], 
                          user_message: str, assistant_message: str,
                          append: bool = True):
        if not conversation_id:
            conversation_id = f"conv_{int(time.time())}"
        conv_index = None
        for i, conv in enumerate(self.history):
            if conv.get('id') == conversation_id:
                conv_index = i
                break
        if conv_index is not None:
            if append:
                self.history[conv_index]['messages'].append({
                    "role": "user",
                    "content": user_message,
                    "timestamp": datetime.now().isoformat()
                })
                self.history[conv_index]['messages'].append({
                    "role": "assistant",
                    "content": assistant_message,
                    "timestamp": datetime.now().isoformat()
                })
            else:
                self.history[conv_index]['messages'].append({
                    "role": "assistant",
                    "content": assistant_message,
                    "timestamp": datetime.now().isoformat()
                })
            self.history[conv_index]['updated_at'] = datetime.now().isoformat()
        else:
            self.history.append({
                'id': conversation_id,
                'title': user_message[:50] + ('...' if len(user_message) > 50 else ''),
                'created_at': datetime.now().isoformat(),
                'updated_at': datetime.now().isoformat(),
                'messages': [
                    {
                        "role": "user",
                        "content": user_message,
                        "timestamp": datetime.now().isoformat()
                    },
                    {
                        "role": "assistant",
                        "content": assistant_message,
                        "timestamp": datetime.now().isoformat()
                    }
                ]
            })
        self._save_history()

    def list_conversations(self):
        if not self.history:
            print("No conversation history yet")
            return
        print(f"\n{'ID':<20} {'Title':<40} {'Created At':<20} {'Message Count':<10}")
        print("-" * 90)
        for conv in self.history:
            msg_count = len(conv.get('messages', [])) // 2
            print(f"{conv['id']:<20} {conv['title']:<40} {conv['created_at'][:19]:<20} {msg_count:<10}")

    def show_conversation(self, conversation_id: str):
        """Display the details of a specific conversation"""
        for conv in self.history:
            if conv.get('id') == conversation_id:
                print(f"\nConversation: {conv['title']}")
                print(f"Created At: {conv['created_at']}")
                print(f"Updated At: {conv['updated_at']}")
                print("-" * 80)
                messages = conv.get('messages', [])
                for idx, msg in enumerate(messages):
                    role = "User" if msg['role'] == 'user' else "AI"
                    print(f"\n[{idx}] [{role}] {msg.get('timestamp', '')}")
                    # Truncate long content for display
                    content = msg['content']
                    if len(content) > 500:
                        content = content[:500] + "... (content truncated)"
                    print(content)
                    print("-" * 40)
                return
        print(f"Conversation not found: {conversation_id}")

    def delete_conversation(self, conversation_id: str):
        """Delete a specific conversation"""
        for i, conv in enumerate(self.history):
            if conv.get('id') == conversation_id:
                del self.history[i]
                self._save_history()
                print(f"Deleted conversation: {conversation_id}")
                return
        print(f"Conversation not found: {conversation_id}")

def main():
    """Main function"""
    parser = argparse.ArgumentParser(description='DeepSeek Chat CLI Tool')
    parser.add_argument('--api-key', help='DeepSeek API key (can also be set via DEEPSEEK_API_KEY environment variable)')
    parser.add_argument('--base-url', default='https://api.deepseek.com/v1', help='API base URL')
    subparsers = parser.add_subparsers(dest='command', help='Command')
    # Chat command
    chat_parser = subparsers.add_parser('chat', help='Send a message')
    chat_parser.add_argument('message', help='User message')
    chat_parser.add_argument('-f', '--file', action='append', help='File path to upload (can be used multiple times)')
    chat_parser.add_argument('-c', '--conversation', help='Conversation ID (continue an existing conversation)')
    chat_parser.add_argument('--no-stream', action='store_true', help='Disable streaming output')
    chat_parser.add_argument('--retry', type=int, help='Retry the message at the specified index')
    # List command
    list_parser = subparsers.add_parser('list', help='List conversation history')
    # Show command
    show_parser = subparsers.add_parser('show', help='Show conversation details')
    show_parser.add_argument('conversation_id', help='Conversation ID')
    # Delete command
    delete_parser = subparsers.add_parser('delete', help='Delete a conversation')
    delete_parser.add_argument('conversation_id', help='Conversation ID')
    # Files command
    files_parser = subparsers.add_parser('files', help='List uploaded files')
    files_parser.add_argument('-c', '--conversation', help='Filter by conversation ID')
    args = parser.parse_args()
    # Get API Key
    api_key = args.api_key or os.environ.get('DEEPSEEK_API_KEY')
    if not api_key:
        print("Error: Please set the DEEPSEEK_API_KEY environment variable or use the --api-key argument")
        print("Example: export DEEPSEEK_API_KEY='your-api-key'")
        sys.exit(1)
    # Create client
    client = DeepSeekChat(api_key=api_key, base_url=args.base_url)
    # Execute command
    if args.command == 'chat':
        client.chat(
            message=args.message,
            file_paths=args.file,
            conversation_id=args.conversation,
            stream=not args.no_stream,
            retry_message_index=args.retry
        )
    elif args.command == 'list':
        client.list_conversations()
    elif args.command == 'show':
        client.show_conversation(args.conversation_id)
    elif args.command == 'delete':
        client.delete_conversation(args.conversation_id)
    elif args.command == 'files':
        client.list_files(args.conversation)
    else:
        # Default to interactive mode
        interactive_mode(client)

def read_multiline_input(prompt: str) -> str:
    print(prompt, end='', flush=True)
    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            print()
            break
        lines.append(line)
    text = '\n'.join(lines).strip()
    if not text:
        return ""
    first_line = lines[0].strip()
    single_line_commands = {
        'exit', 'help', 'new', 'list', 'retry', 'r',
        'messages', 'msgs', 'file-upload-only', 'fuo',
        'show-files', 'sf', 'clear-files', 'cf',
    }
    first_word = first_line.split()[0].lower() if first_line else ''
    is_command = (
        first_word in single_line_commands
        or first_line.startswith(('show ', 'use ', 'file ', 'remove-file ', 'rf '))
    )
    if is_command:
        return first_line
    return text

def interactive_mode(client: DeepSeekChat):
    """Interactive mode"""
    print("=" * 36)
    print("DeepSeek Chat Interactive Mode")
    print("Type 'help' for help, 'exit' to quit")
    print("=" * 36)
    current_conversation_id = None
    pending_files = []  # List of files waiting to be sent
    while True:
        try:
            # Display prompt
            if pending_files:
                file_names = [Path(f).name for f in pending_files]
                file_indicator = f" [Pending files: {', '.join(file_names)}]"
            else:
                file_indicator = ""
                prompt = f"\n[{current_conversation_id or 'new'}]{file_indicator} You (Ctrl+D to send): "
                user_input = read_multiline_input(prompt)
                if not user_input:
                    continue
            # Handle commands
            if user_input.lower() == 'exit':
                print("Goodbye!")
                break
            elif user_input.lower() == 'help':
                show_help()
                continue
            elif user_input.lower() == 'new':
                current_conversation_id = None
                pending_files = []  # Clear pending files
                print("New conversation created")
                continue
            elif user_input.lower() == 'list':
                client.list_conversations()
                continue
            elif user_input.lower().startswith('show '):
                conv_id = user_input[5:].strip()
                client.show_conversation(conv_id)
                continue
            elif user_input.lower().startswith('use '):
                current_conversation_id = user_input[4:].strip()
                pending_files = []  # Clear pending files when switching conversations
                print(f"Switched to conversation: {current_conversation_id}")
                continue
            # Retry commands
            elif user_input.lower().startswith('retry ') or user_input.lower().startswith('r '):
                try:
                    # Extract index
                    parts = user_input.split()
                    if len(parts) < 2:
                        print("Usage: retry <message_index> or r <message_index>")
                        continue
                    msg_index = int(parts[1])
                    if not current_conversation_id:
                        print("Please select a conversation first (use 'use <conversation_id>')")
                        continue
                    # Get message history
                    messages = client.get_message_history(current_conversation_id)
                    if msg_index >= len(messages):
                        print(f"Message index {msg_index} is out of range (0-{len(messages)-1})")
                        continue
                    # Show the message to retry
                    msg = messages[msg_index]
                    if msg.get('role') != 'user':
                        print("Only user messages can be retried")
                        continue
                    print(f"\nRetrying message [{msg_index}]:")
                    print(msg['content'][:200] + ("..." if len(msg['content']) > 200 else ""))
                    print("\nGenerating new reply...")
                    # Execute retry
                    print("AI: ", end='')
                    client.chat(
                        message="",  # Empty message, will use history
                        conversation_id=current_conversation_id,
                        retry_message_index=msg_index
                    )
                except ValueError:
                    print("Please provide a valid message index, e.g.: retry 2")
                except Exception as e:
                    print(f"Retry failed: {e}")
                continue
            # Quick retry of the last message
            elif user_input.lower() == 'retry' or user_input.lower() == 'r':
                if not current_conversation_id:
                    print("Please select a conversation first (use 'use <conversation_id>')")
                    continue
                messages = client.get_message_history(current_conversation_id)
                if not messages:
                    print("No messages in the conversation")
                    continue
                # Find the last user message
                last_user_idx = None
                for i in range(len(messages) - 1, -1, -1):
                    if messages[i].get('role') == 'user':
                        last_user_idx = i
                        break
                if last_user_idx is None:
                    print("No user message found")
                    continue
                # Check if there's already an AI reply after the user message
                if last_user_idx + 1 < len(messages) and messages[last_user_idx + 1].get('role') == 'assistant':
                    print(f"Retrying last message [{last_user_idx}]")
                    print("Generating new reply...")
                    print("AI: ", end='')
                    client.chat(
                        message="",
                        conversation_id=current_conversation_id,
                        retry_message_index=last_user_idx
                    )
                else:
                    print("The last user message has no reply yet, cannot retry")
                continue
            # Show conversation message list
            elif user_input.lower() == 'messages' or user_input.lower() == 'msgs':
                if not current_conversation_id:
                    print("Please select a conversation first (use 'use <conversation_id>')")
                    continue
                messages = client.get_message_history(current_conversation_id)
                if not messages:
                    print("No messages in the conversation")
                    continue
                print(f"\nConversation message list ({len(messages)} messages):")
                print("-" * 60)
                for idx, msg in enumerate(messages):
                    role = "User" if msg['role'] == 'user' else "AI"
                    content_preview = msg['content'][:50] + ("..." if len(msg['content']) > 50 else "")
                    print(f"[{idx}] {role}: {content_preview}")
                print("-" * 60)
                print("Use 'retry <index>' to retry a specific message")
                continue
            # File upload related commands
            elif user_input.lower().startswith('file-upload-only ') or user_input.lower().startswith('fuo '):
                # Extract file paths after the command
                cmd_end = user_input.index(' ') + 1
                file_paths_str = user_input[cmd_end:].strip()
                # Support multiple file paths separated by spaces
                file_paths = file_paths_str.split()
                if file_paths:
                    success_count = 0
                    for file_path in file_paths:
                        try:
                            file_info = client.upload_file(file_path, current_conversation_id)
                            pending_files.append(file_path)
                            print(f"✓ Added: {file_info['original_name']}")
                            success_count += 1
                        except Exception as e:
                            print(f"✗ Upload failed: {file_path} - {e}")
                    print(f"Successfully added {success_count}/{len(file_paths)} files, current pending files: {len(pending_files)}")
                else:
                    print("Please provide file paths, e.g.: fuo file1.txt file2.md")
                continue
            elif user_input.lower() == 'file-upload-only' or user_input.lower() == 'fuo':
                print("Usage: fuo <file1> [file2] [file3] ...")
                print("Example: fuo /path/to/file1.txt /path/to/file2.md")
                continue
            elif user_input.lower() == 'show-files' or user_input.lower() == 'sf':
                if pending_files:
                    print(f"\nCurrent pending files ({len(pending_files)}):")
                    for i, file_path in enumerate(pending_files, 1):
                        file_name = Path(file_path).name
                        file_size = Path(file_path).stat().st_size if Path(file_path).exists() else "unknown"
                        print(f"  {i}. {file_name} ({file_size} bytes)")
                else:
                    print("No pending files")
                continue
            elif user_input.lower() == 'clear-files' or user_input.lower() == 'cf':
                pending_files.clear()
                print("All pending files cleared")
                continue
            elif user_input.lower().startswith('remove-file ') or user_input.lower().startswith('rf '):
                try:
                    index = int(user_input.split()[-1]) - 1
                    if 0 <= index < len(pending_files):
                        removed_file = pending_files.pop(index)
                        print(f"Removed from pending list: {Path(removed_file).name}")
                    else:
                        print("Invalid file index")
                except (ValueError, IndexError):
                    print("Please use the correct format: remove-file <number> or rf <number>")
                continue
            elif user_input.lower().startswith('file '):
                file_path = user_input[5:].strip()
                if file_path:
                    # Add file to pending list
                    try:
                        file_info = client.upload_file(file_path, current_conversation_id)
                        pending_files.append(file_path)
                        print(f"File uploaded and added to pending list: {file_info['original_name']}")
                    except Exception as e:
                        print(f"Failed to process file: {e}")
                continue
            # Send message (including pending files)
            print(f"\nAI: ", end='')
            # If there are pending files, send them together with the message
            if pending_files:
                client.chat(
                    message=user_input,
                    file_paths=pending_files.copy(),  # Use a copy to avoid modifying the original list
                    conversation_id=current_conversation_id
                )
                pending_files.clear()  # Clear pending files after sending
            else:
                client.chat(
                    message=user_input,
                    conversation_id=current_conversation_id
                )
            # Update conversation ID
            if not current_conversation_id and client.history:
                current_conversation_id = client.history[-1]['id']
        except KeyboardInterrupt:
            print("\n\nGoodbye!")
            break
        except Exception as e:
            print(f"Error: {e}")

def show_help():
    """Display help information"""
    help_text = """
Available commands:
  help                    - Show help
  exit                    - Exit the program
  new                     - Start a new conversation
  list                    - List conversation history
  show <conversation_id>  - Show conversation details
  use <conversation_id>   - Switch to a specific conversation
  messages (msgs)         - Show the message list of the current conversation
  
Retry features:
  retry (r)               - Retry the last user message
  retry <index> (r <index>) - Retry the user message at the specified index
  
File management:
  file-upload-only (fuo)  - Upload files only, without analysis
  file <file_path>        - Upload a file (compatibility mode)
  show-files (sf)         - Show the current pending file list
  remove-file <number> (rf) - Remove a file from the pending list
  clear-files (cf)        - Clear all pending files
  
Simply type text to chat with the AI.
If there are pending files, they will be sent together with the message.
    """
    print(help_text)

if __name__ == '__main__':
    main()
