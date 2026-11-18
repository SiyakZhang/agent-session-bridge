$ErrorActionPreference = 'Stop'
$bridgeRoot = Split-Path -Parent $PSScriptRoot
$bridgePython = Join-Path $bridgeRoot '.venv/Scripts/python.exe'
$bridgeState = Join-Path $env:USERPROFILE '.dingtalk-session-bridge'
Start-Process -FilePath $bridgePython -ArgumentList '-m', 'session_bridge.cli', 'run' -WorkingDirectory $bridgeRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $bridgeState 'stdout.log') -RedirectStandardError (Join-Path $bridgeState 'stderr.log')
