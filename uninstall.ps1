$ErrorActionPreference = "Stop"
$taskName = "CLIProxyAPI-Claude-Window"
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($task) {
	Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
	Write-Output "Removed scheduled task $taskName"
} else {
	Write-Output "Scheduled task $taskName is not installed"
}
Write-Output "Local config, state, and log remain in $env:LOCALAPPDATA\CLIProxyAPI"
