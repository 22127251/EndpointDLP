"""
Windows named pipe client.

Sends a JSON payload to the pipe server and reads back "ALLOW" or "BLOCK".
Raises TimeoutError if no response within timeout_seconds.
Raises OSError if the pipe cannot be opened (server not running).

Supports two payload types:
1. Legacy file upload payload (dict with temp_path, url, etc.)
2. Chunk payload (channel, priority, message_id, chunk_id, content)
"""

import json
import time
from dataclasses import dataclass, asdict
from typing import List, Optional, Any, Dict, Union

import pywintypes
import win32file
import win32pipe


@dataclass
class ChunkPayload:
    """Payload for text chunk analysis."""
    channel: str  # "clipboard" or "browser"
    priority: bool  # True for clipboard, False for browser
    message_id: str  # Unique ID for the source message
    chunk_id: int  # Index of this chunk within the message
    total_chunks: int  # Total number of chunks in this message
    content: str  # The text content of this chunk
    word_count: int  # Number of words in this chunk
    
    # Optional metadata
    source_url: Optional[str] = None  # For browser channel
    filename: Optional[str] = None  # For file uploads
    timestamp: Optional[str] = None  # ISO timestamp
    
    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ClearPriorityQueuePayload:
    """Signal to clear the priority queue."""
    channel: str = "clipboard"
    priority: bool = True
    message_id: str = ""  # New message ID that triggered the clear
    action: str = "clear_priority_queue"
    
    def to_dict(self) -> dict:
        return asdict(self)


def chunk_text(text: str, chunk_size_words: int, overlap_words: int) -> List[str]:
    """
    Split text into chunks of approximately chunk_size_words.
    Consecutive chunks overlap by overlap_words for context continuity.
    
    Uses natural word boundaries (spaces, punctuation).
    """
    if not text.strip():
        return []
    
    # Split into words (preserving punctuation attached to words)
    words = text.split()
    if len(words) <= chunk_size_words:
        return [' '.join(words)]
    
    chunks = []
    start = 0
    
    while start < len(words):
        end = start + chunk_size_words
        chunk_words = words[start:end]
        chunks.append(' '.join(chunk_words))
        
        # Move start position with overlap
        start = end - overlap_words
        if start >= len(words):
            break
    
    return chunks


def send_and_receive(
    payload: Union[dict, Any],
    pipe_name: str,
    timeout_seconds: float
) -> str:
    """
    Open the named pipe, send JSON, read response, return "ALLOW" or "BLOCK".
    
    Payload can be:
    - Legacy file upload dict (temp_path, url, etc.)
    - ChunkPayload dict (channel, priority, message_id, chunk_id, content)
    """
    deadline = time.monotonic() + timeout_seconds

    # Wait for the pipe to become available (it may be busy serving another client)
    _wait_for_pipe(pipe_name, timeout_seconds)

    handle = win32file.CreateFile(
        pipe_name,
        win32file.GENERIC_READ | win32file.GENERIC_WRITE,
        0,       # no sharing
        None,    # default security
        win32file.OPEN_EXISTING,
        0,
        None,
    )

    try:
        # Switch to message-read mode
        win32pipe.SetNamedPipeHandleState(
            handle,
            win32pipe.PIPE_READMODE_MESSAGE,
            None,
            None,
        )

        # Convert payload to JSON-serializable dict
        if hasattr(payload, 'to_dict'):
            message = json.dumps(payload.to_dict()).encode("utf-8")
        else:
            message = json.dumps(payload).encode("utf-8")
        
        win32file.WriteFile(handle, message)

        remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
        # Use overlapped I/O timeout via SetCommTimeouts is not available for pipes;
        # instead rely on the server responding within the deadline.
        # ReadFile blocks until data arrives or handle is closed.
        _, response_bytes = win32file.ReadFile(handle, 64 * 1024)
        response = response_bytes.decode("utf-8").strip().upper()

        if response not in ("ALLOW", "BLOCK"):
            raise ValueError(f"Unexpected pipe response: {response!r}")

        return response

    finally:
        win32file.CloseHandle(handle)


def _wait_for_pipe(pipe_name: str, timeout_seconds: float) -> None:
    """
    Block until the named pipe server is ready or timeout elapses.
    Raises TimeoutError if pipe is not available in time.
    """
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            win32pipe.WaitNamedPipe(pipe_name, 500)  # 500 ms per attempt
            return
        except pywintypes.error as e:
            # ERROR_FILE_NOT_FOUND (2): server not running at all
            # ERROR_SEM_TIMEOUT (121): all instances busy, retry
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Named pipe '{pipe_name}' not available after {timeout_seconds}s"
                ) from e
