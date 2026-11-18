$bridgeRoot = Split-Path -Parent $PSScriptRoot
& (Join-Path $bridgeRoot '.venv/Scripts/python.exe') -m session_bridge.cli status
