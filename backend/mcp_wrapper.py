#!/usr/bin/env python
"""
MCP Wrapper for Antigravity IDE on Windows.
This wrapper normalizes CRLF line endings to LF to fix the 
"invalid trailing data at the end of stream" error.
"""
import subprocess
import sys
import os

def main():
    # Get the directory of this script
    script_dir = os.path.dirname(os.path.abspath(__file__))
    mcp_server_path = os.path.join(script_dir, "mcp_server.py")
    
    # Start the actual MCP server
    process = subprocess.Popen(
        [sys.executable, mcp_server_path],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=sys.stderr,
        bufsize=0,
        cwd=script_dir
    )
    
    import threading
    
    def forward_stdin():
        """Forward stdin to the subprocess, normalizing CRLF to LF."""
        try:
            while True:
                data = sys.stdin.buffer.read(1)
                if not data:
                    break
                # Skip \r characters (convert CRLF to LF)
                if data == b'\r':
                    continue
                process.stdin.write(data)
                process.stdin.flush()
        except Exception:
            pass
        finally:
            try:
                process.stdin.close()
            except Exception:
                pass
    
    def forward_stdout():
        """Forward stdout from the subprocess, normalizing CRLF to LF."""
        try:
            while True:
                data = process.stdout.read(1)
                if not data:
                    break
                # Skip \r characters (convert CRLF to LF)
                if data != b'\r':
                    sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()
        except Exception:
            pass
    
    # Start threads for bidirectional communication
    stdin_thread = threading.Thread(target=forward_stdin, daemon=True)
    stdout_thread = threading.Thread(target=forward_stdout, daemon=True)
    
    stdin_thread.start()
    stdout_thread.start()
    
    # Wait for the process to complete
    process.wait()
    stdout_thread.join(timeout=1)

if __name__ == "__main__":
    main()