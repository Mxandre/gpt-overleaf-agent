param(
    [string]$WorkspaceDir = "workspace",
    [int]$Port = 8000,
    [string]$HostAddress = "127.0.0.1",
    [string]$Path = "/mcp",
    [string]$PythonExe = ""
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir
$env:WORKSPACE_DIR = $WorkspaceDir
$env:PYTHONPATH = if ($env:PYTHONPATH) {
    "$RepoRoot\src;$env:PYTHONPATH"
}
else {
    "$RepoRoot\src"
}

Write-Host "Starting overleaf-agent MCP server..."
Write-Host "Workspace: $env:WORKSPACE_DIR"
Write-Host "Local URL: http://$HostAddress`:$Port$Path"
Write-Host ""
Write-Host "To expose this server to GPT Developer Mode with ngrok, run this in another terminal:"
Write-Host "  ngrok http $Port"
Write-Host ""

$Command = Get-Command overleaf-agent-mcp -ErrorAction SilentlyContinue
if ($Command) {
    overleaf-agent-mcp --http --host $HostAddress --port $Port --path $Path
}
else {
    if (-not $PythonExe -and $env:CONDA_PREFIX) {
        $CondaPython = Join-Path $env:CONDA_PREFIX "python.exe"
        if (Test-Path $CondaPython) {
            $PythonExe = $CondaPython
        }
    }
    if (-not $PythonExe) {
        $PythonExe = "python"
    }

    Write-Host "Command 'overleaf-agent-mcp' was not found. Falling back to:"
    Write-Host "  $PythonExe -m overleaf_agent.mcp_server --http --host $HostAddress --port $Port --path $Path"
    Write-Host "CONDA_PREFIX: $env:CONDA_PREFIX"
    Write-Host "PYTHONPATH: $env:PYTHONPATH"
    Write-Host ""
    & $PythonExe -m overleaf_agent.mcp_server --http --host $HostAddress --port $Port --path $Path
}
