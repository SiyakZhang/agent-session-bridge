$ErrorActionPreference = 'Stop'
$bridgeRoot = Split-Path -Parent $PSScriptRoot
$bridgePython = (Resolve-Path (Join-Path $bridgeRoot '.venv/Scripts/pythonw.exe')).Path
$bridgeUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$bridgeAction = New-ScheduledTaskAction -Execute $bridgePython -Argument '-m session_bridge.cli run' -WorkingDirectory $bridgeRoot
$bridgeTrigger = New-ScheduledTaskTrigger -AtLogOn -User $bridgeUser
$bridgePrincipal = New-ScheduledTaskPrincipal -UserId $bridgeUser -LogonType Interactive -RunLevel Limited
$bridgeSettings = New-ScheduledTaskSettingsSet -Hidden -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 100 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName 'DingTalk Session Bridge' -Action $bridgeAction -Trigger $bridgeTrigger -Principal $bridgePrincipal -Settings $bridgeSettings -Description 'Multi-computer Codex and Claude session notifications and exact-session replies' -Force | Select-Object TaskName,State
