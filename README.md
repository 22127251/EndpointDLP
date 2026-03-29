# EndpointDLP - Demo Mode

## Quick Start

### Run All Components (Recommended)

```powershell
# Open 3 PowerShell windows: QueueManager, ClipboardInterceptor, mitmproxy
.\script\run_all.ps1 -Decision allow
```

This will:

1. Open **QueueManager** (C# Core) - pipe server with priority/non-priority queues + **interactive analyzer**
2. Open **ClipboardInterceptor** (.NET) - clipboard monitor
3. Open **mitmproxy** (Python) - HTTP interceptor with system proxy

**To stop:** Close each window or run:

```powershell
.\script\stop_all.ps1
```

---

### Run Individual Components

#### 1. QueueManager (C# Core) - WITH INTERACTIVE ANALYZER

```powershell
# PowerShell
.\script\run_queue_manager.ps1

# Or batch
.\script\run_queue_manager.bat

# Or manually
dotnet run --project src\QueueManager
```

**This is the main component** - it will:

- Start named pipe server on `\\.\pipe\dlp_upload`
- Wait for chunks from clipboard and browser
- **Ask you to press [a]llow or [b]lock for each chunk**
- Reconstruct original text when all chunks are ALLOW
- BLOCK if ANY chunk is blocked

#### 2. Clipboard Interceptor (.NET)

```powershell
# PowerShell
.\script\run_clipboard.ps1

# Or batch
.\script\run_clipboard.bat

# Or manually
dotnet run --project src\ClipboardInterceptor
```

This will:

- Monitor clipboard for text copies
- Chunk text into 500-word segments
- Send chunks to QueueManager via pipe
- Priority queue (analyzed first)

#### 3. Network Interceptor (mitmproxy)

```powershell
# PowerShell (with venv auto-setup)
.\script\run_mitmproxy.ps1
```

This will:

- Create/activate Python venv if needed
- Install dependencies from requirements.txt
- Enable system proxy (127.0.0.1:8080)
- Start mitmdump with addon.py

Manual alternative:

```bash
pip install -r requirements.txt
mitmdump -s addon.py --listen-port 8080
```

---

## Testing

### Test Clipboard Interception

1. Run QueueManager:

   ```powershell
   .\script\run_queue_manager.ps1
   ```

2. Run ClipboardInterceptor:

   ```powershell
   .\script\run_clipboard.ps1
   ```

3. Copy any text (e.g., from a webpage or document)

4. In QueueManager window, you'll see:

   ```
   ????????????????????????????????????????????????????????????
   ?  Chunk  1/3 | Message: clipboard_abc123...
   ?  Channel: clipboard | Priority: True  | Words: 500
   ????????????????????????????????????????????????????????????
   ?  This is the text content you copied...
   ????????????????????????????????????????????????????????????

   [a]llow | [b]lock | [q]uit >
   ```

5. Press `a` to allow or `b` to block

6. If all chunks are ALLOW, you'll see the reconstructed text

### Test Browser Upload Interception

1. Run QueueManager:

   ```powershell
   .\script\run_queue_manager.ps1
   ```

2. Run mitmproxy:

   ```powershell
   .\script\run_mitmproxy.ps1
   ```

3. Upload a `.txt` file to any website

4. In QueueManager window, you'll see the chunks and can analyze them

---

## Policy

- **ALL chunks must be ALLOW** ? overall ALLOW
- **ANY chunk BLOCK** ? overall BLOCK
- First chunk BLOCK ? immediately block, skip remaining chunks
- Text is reconstructed from chunks by removing 50-word overlaps

---

## Troubleshooting

**Proxy not working?**

```powershell
# Manually disable proxy
Set-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings" -Name ProxyEnable -Value 0
netsh winhttp reset proxy
```

**Port 8080 in use?**

```powershell
# Find and kill process using port 8080
netstat -ano | findstr :8080
taskkill /PID <PID> /F
```

**QueueManager not starting?**

```bash
# Check .NET SDK
dotnet --version

# Restore and rebuild
dotnet restore src/EndpointDLP.slnx
dotnet build src/EndpointDLP.slnx
```

**Python venv issues?**

```powershell
# Delete and recreate venv
Remove-Item -Recurse -Force .\.venv
python -m venv .\.venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```
