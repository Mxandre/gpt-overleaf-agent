# GPT Developer Mode With ngrok

This example shows how to expose the local overleaf-agent MCP server to GPT Developer Mode.

## 1. Install The Project

From the repository root:

```powershell
conda create -n overleaf-agent python=3.11 -y
conda activate overleaf-agent
pip install -e .
```

## 2. Choose A Workspace

The workspace is the folder that contains your LaTeX projects.

```powershell
$env:WORKSPACE_DIR="E:\overleaf-agent\workspace"
```

You can also put this in a local `.env` file:

```text
WORKSPACE_DIR=E:\overleaf-agent\workspace
```

## 3. Start The Local HTTP MCP Server

Recommended, robust Windows command:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start_http_server.ps1 `
  -WorkspaceDir "E:\overleaf-agent\workspace" `
  -Port 8000 `
  -PythonExe "D:\Anaconda\envs\overleaf-agent\python.exe"
```

Adjust `-PythonExe` to your conda environment's Python executable.

If `overleaf-agent-mcp` is already available in your shell, this direct command also works:

```powershell
$env:WORKSPACE_DIR="E:\overleaf-agent\workspace"
overleaf-agent-mcp --http --host 127.0.0.1 --port 8000 --path /mcp
```

The local MCP URL is:

```text
http://127.0.0.1:8000/mcp
```

## 4. Start ngrok

Open another PowerShell window:

```powershell
ngrok http 8000
```

ngrok prints a public HTTPS URL such as:

```text
https://example-name.ngrok-free.app
```

The GPT MCP URL should be:

```text
https://example-name.ngrok-free.app/mcp
```

The `/mcp` suffix matters because the server endpoint is mounted at `/mcp`.

## 5. Configure GPT Developer Mode

In GPT Developer Mode, add an MCP server that points to the ngrok MCP URL:

```text
https://example-name.ngrok-free.app/mcp
```

Keep both terminals running:

- `overleaf-agent-mcp` or `scripts/start_http_server.ps1`
- `ngrok http 8000`

## 6. Smoke Test

Ask GPT:

```text
Use overleaf-agent to list my workspace projects.
```

GPT should call:

```text
list_workspace_projects
```

If it returns projects from your local `WORKSPACE_DIR`, the connection is working.

## Troubleshooting

If GPT uses `/mnt/data` or `/mnt/user-data/uploads`, ask it to use `list_workspace_projects` first.

If GPT cannot connect, check:

- The local server is still running.
- ngrok is still running.
- The GPT MCP URL ends with `/mcp`.
- The ngrok URL has not changed.
- Your firewall allows local port `8000`.

If the helper script uses the wrong Python, pass `-PythonExe` explicitly:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start_http_server.ps1 `
  -WorkspaceDir "E:\overleaf-agent\workspace" `
  -Port 8000 `
  -PythonExe "D:\Anaconda\envs\overleaf-agent\python.exe"
```
