param(
	[Parameter(Mandatory = $true)][string[]]$AccountEmails,
	[string[]]$AccountNames,
	[string]$ProxyUri = "http://127.0.0.1:8317",
	[string]$ProxyExecutable = (Join-Path $env:LOCALAPPDATA "CLIProxyAPI\cli-proxy-api.exe"),
	[string]$ProxyConfig = (Join-Path $env:LOCALAPPDATA "CLIProxyAPI\config.yaml"),
	[ValidateSet("Auto", "T3", "Prompt")][string]$ManagementKeySource = "Auto",
	[ValidateRange(1, 1440)][int]$IntervalMinutes = 10
)

$ErrorActionPreference = "Stop"
$taskName = "claude-timer"
$legacyTaskName = "CLIProxyAPI-Claude-Window"
$installDir = Join-Path $env:LOCALAPPDATA "CLIProxyAPI"
$scriptPath = Join-Path $installDir "warmup.ps1"
$configPath = Join-Path $installDir "warmup-config.json"
$t3SettingsPath = Join-Path $env:USERPROFILE ".t3\userdata\settings.json"
$requiredVersion = [version]"7.5"
if ($PSVersionTable.PSVersion -lt $requiredVersion) { throw "PowerShell 7.5 or newer is required" }
$pwsh = (Get-Command pwsh.exe -ErrorAction Stop).Source

if ($AccountNames -and $AccountNames.Count -ne $AccountEmails.Count) {
	throw "AccountNames must have the same number of entries as AccountEmails"
}
$accounts = @()
for ($i = 0; $i -lt $AccountEmails.Count; $i++) {
	$email = $AccountEmails[$i].Trim().ToLowerInvariant()
	if ($email -notmatch "^[^@\s]+@[^@\s]+\.[^@\s]+$") { throw "Invalid account email: $email" }
	$name = if ($AccountNames) { $AccountNames[$i].Trim() } else { $email.Split("@")[0] }
	if ([string]::IsNullOrWhiteSpace($name)) { throw "Account name cannot be empty" }
	$accounts += @{ name = $name; email = $email }
}
if (@($accounts.email | Select-Object -Unique).Count -ne $accounts.Count) {
	throw "Account emails must be unique"
}
if (@($accounts.name | Select-Object -Unique).Count -ne $accounts.Count) {
	throw "Account names must be unique"
}

$uri = [uri]$ProxyUri
if ($uri.Scheme -ne "http" -or $uri.Host -notin @("localhost", "127.0.0.1", "::1")) {
	throw "ProxyUri must be a local HTTP address"
}
$ProxyUri = $ProxyUri.TrimEnd("/")

$managementKey = $null
$useT3 = $false
if ($ManagementKeySource -ne "Prompt" -and (Test-Path -LiteralPath $t3SettingsPath)) {
	$settings = Get-Content -LiteralPath $t3SettingsPath -Raw | ConvertFrom-Json
	$managementKey = $settings.usageLimitSources.cliproxy_local.managementKey
	$useT3 = -not [string]::IsNullOrWhiteSpace($managementKey)
}
if (-not $useT3) {
	if ($ManagementKeySource -eq "T3") { throw "T3 CLIProxyAPI management key was not found" }
	$secret = Read-Host "CLIProxyAPI management key" -AsSecureString
	$managementKey = [pscredential]::new("management", $secret).GetNetworkCredential().Password
	if ([string]::IsNullOrWhiteSpace($managementKey)) { throw "Management key cannot be empty" }
}

# Validate one key and the selected accounts before touching the scheduled task.
try {
	$authFiles = (Invoke-RestMethod -Uri "$ProxyUri/v0/management/auth-files" -Headers @{ "X-Management-Key" = $managementKey } -TimeoutSec 15).files
} catch {
	throw "Cannot reach CLIProxyAPI management API with this key. Start the proxy and check ProxyUri."
}
foreach ($account in $accounts) {
	if (-not ($authFiles | Where-Object { $_.provider -eq "claude" -and $_.email -eq $account.email })) {
		throw "Claude account is not authenticated in CLIProxyAPI: $($account.email)"
	}
}

New-Item -ItemType Directory -Path $installDir -Force | Out-Null
$config = [ordered]@{
	proxyUri = $ProxyUri
	proxyExecutable = if (Test-Path -LiteralPath $ProxyExecutable) { (Resolve-Path -LiteralPath $ProxyExecutable).Path } else { $null }
	proxyConfig = if (Test-Path -LiteralPath $ProxyConfig) { (Resolve-Path -LiteralPath $ProxyConfig).Path } else { $null }
	model = "claude-haiku-4-5-20251001"
	accounts = $accounts
	managementKey = if ($useT3) { $null } else { $managementKey }
	t3SettingsPath = if ($useT3) { $t3SettingsPath } else { $null }
}
$config | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $configPath -Encoding utf8
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "warmup.ps1") -Destination $scriptPath -Force

$action = New-ScheduledTaskAction -Execute $pwsh -Argument ('-NoProfile -NonInteractive -File "' + $scriptPath + '"')
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) -RepetitionDuration (New-TimeSpan -Days 3650)
$taskSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $taskSettings -Principal $principal -Description "Start Claude five-hour windows with a tiny account-pinned Haiku request." -Force | Out-Null
if (Get-ScheduledTask -TaskName $legacyTaskName -ErrorAction SilentlyContinue) {
	Unregister-ScheduledTask -TaskName $legacyTaskName -Confirm:$false
}

Write-Output "Installed $taskName ($IntervalMinutes-minute ticks)"
Write-Output "Local config: $configPath"
Write-Output "Local log: $(Join-Path $installDir 'warmup.log')"
Write-Output "Run '& `"$pwsh`" -NoProfile -File `"$scriptPath`" -DryRun' to inspect the next checks."
