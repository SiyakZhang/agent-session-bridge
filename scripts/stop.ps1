$ErrorActionPreference = 'Stop'
$bridgeTask = Get-ScheduledTask -TaskName 'DingTalk Session Bridge' -ErrorAction SilentlyContinue
if ($bridgeTask) { Stop-ScheduledTask -TaskName 'DingTalk Session Bridge' }
$bridgeRoot = Split-Path -Parent $PSScriptRoot
$bridgePython = (Resolve-Path (Join-Path $bridgeRoot '.venv/Scripts/python.exe')).Path
$bridgePythonw = (Resolve-Path (Join-Path $bridgeRoot '.venv/Scripts/pythonw.exe')).Path
$bridgeProcesses = Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" | Where-Object { ($_.ExecutablePath -eq $bridgePython -or $_.ExecutablePath -eq $bridgePythonw) -and $_.CommandLine -match 'session_bridge.cli.*run' }
foreach ($bridgeProcess in $bridgeProcesses) {
    Stop-Process -Id $bridgeProcess.ProcessId
}
