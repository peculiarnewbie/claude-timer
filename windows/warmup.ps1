param([switch]$DryRun)

$ErrorActionPreference = "Stop"
$baseDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$statePath = Join-Path $baseDir "warmup-state.json"
$logPath = Join-Path $baseDir "warmup.log"
$configPath = Join-Path $baseDir "warmup-config.json"
if (-not (Test-Path -LiteralPath $configPath)) { throw "Missing $configPath; run install.ps1" }
$config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json -DateKind String
$proxyUri = ([string]$config.proxyUri).TrimEnd("/")
$proxyPort = ([uri]$proxyUri).Port
$accounts = @($config.accounts)
if ($accounts.Count -eq 0) { throw "No Claude accounts configured" }

function Write-Log([string]$message) {
	$line = "$(Get-Date -Format o) $message"
	Add-Content -LiteralPath $logPath -Value $line
	if ($DryRun) { Write-Output $line }
}

function Save-State {
	if ($DryRun) { return }
	$tmpPath = "$statePath.tmp"
	$state | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $tmpPath -Encoding utf8
	Move-Item -LiteralPath $tmpPath -Destination $statePath -Force
}

function Invoke-Management([string]$path, [string]$method, $body = $null) {
	$params = @{
		Uri = "$proxyUri/v0/management/$path"
		Method = $method
		Headers = @{ "X-Management-Key" = $managementKey }
		TimeoutSec = 30
	}
	if ($null -ne $body) {
		$params.ContentType = "application/json"
		$params.Body = $body | ConvertTo-Json -Depth 10 -Compress
	}
	Invoke-RestMethod @params
}

function Get-Quota([string]$authIndex) {
	$response = Invoke-Management "api-call" "POST" @{
		auth_index = $authIndex
		method = "GET"
		url = "https://api.anthropic.com/api/oauth/usage"
		header = @{
			Authorization = 'Bearer $TOKEN$'
			"anthropic-beta" = "oauth-2025-04-20"
		}
	}
	if ($response.status_code -ne 200) {
		throw "Quota endpoint returned HTTP $($response.status_code)"
	}
	$response.body | ConvertFrom-Json -DateKind String
}

function Send-Warmup([string]$authIndex) {
	$payload = @{
		model = $config.model
		max_tokens = 8
		messages = @(@{ role = "user"; content = "Reply OK." })
	} | ConvertTo-Json -Depth 5 -Compress
	$response = Invoke-Management "api-call" "POST" @{
		auth_index = $authIndex
		method = "POST"
		url = "https://api.anthropic.com/v1/messages"
		header = @{
			Authorization = 'Bearer $TOKEN$'
			"anthropic-version" = "2023-06-01"
			"anthropic-beta" = "claude-code-20250219,oauth-2025-04-20"
			"Content-Type" = "application/json"
			"User-Agent" = "claude-cli/2.1.280 (external, cli)"
		}
		data = $payload
	}
	if ($response.status_code -ne 200) {
		throw "Haiku request returned HTTP $($response.status_code)"
	}
	$result = $response.body | ConvertFrom-Json
	if ($result.type -ne "message") { throw "Haiku returned an unexpected response" }
}

$mutex = [System.Threading.Mutex]::new($false, "Local\CLIProxyAPIClaudeWindow")
if (-not $mutex.WaitOne(0)) { exit 0 }
try {
	$state = @{}
	if (Test-Path -LiteralPath $statePath) {
		$state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json -AsHashtable -DateKind String
	}

	if (-not (Get-NetTCPConnection -LocalPort $proxyPort -State Listen -ErrorAction SilentlyContinue)) {
		if ($DryRun) { throw "Proxy is not running" }
		if (-not $config.proxyExecutable -or -not (Test-Path -LiteralPath $config.proxyExecutable)) {
			throw "Proxy is not running and proxyExecutable is missing"
		}
		$proxyArgs = if ($config.proxyConfig) { '-config "' + [string]$config.proxyConfig + '"' } else { @() }
		Start-Process -FilePath $config.proxyExecutable -ArgumentList $proxyArgs -WorkingDirectory (Split-Path -Parent $config.proxyExecutable) -WindowStyle Hidden
		$ready = $false
		for ($i = 0; $i -lt 20; $i++) {
			Start-Sleep -Seconds 1
			if (Get-NetTCPConnection -LocalPort $proxyPort -State Listen -ErrorAction SilentlyContinue) { $ready = $true; break }
		}
		if (-not $ready) { throw "Proxy did not start" }
		Write-Log "Started CLIProxyAPI"
	}

	$managementKey = $config.managementKey
	if ([string]::IsNullOrWhiteSpace($managementKey) -and $config.t3SettingsPath) {
		$settings = Get-Content -LiteralPath $config.t3SettingsPath -Raw | ConvertFrom-Json
		$managementKey = $settings.usageLimitSources.cliproxy_local.managementKey
	}
	if ([string]::IsNullOrWhiteSpace($managementKey)) { throw "Management key is missing" }
	$authFiles = $null

	foreach ($account in $accounts) {
		try {
			$now = [DateTimeOffset]::UtcNow
			$entry = $state[$account.Name]
			if ($null -eq $entry) { $entry = @{}; $state[$account.Name] = $entry }
			if ($entry.nextCheckUtc) {
				$nextCheck = [DateTimeOffset]::Parse($entry.nextCheckUtc)
				if ($now -lt $nextCheck) {
					if ($DryRun) { Write-Log "$($account.Name): next check $($nextCheck.ToLocalTime().ToString('o'))" }
					continue
				}
			}

			if ($null -eq $authFiles) { $authFiles = (Invoke-Management "auth-files" "GET").files }
			$file = $authFiles | Where-Object { $_.email -eq $account.Email -and $_.provider -eq "claude" } | Select-Object -First 1
			if ($null -eq $file -or -not $file.auth_index) { throw "Claude account is missing" }
			if ($file.status -and $file.status -ne "ready" -and $file.status -ne "active") {
				throw "Claude account status: $($file.status)"
			}
			$quota = Get-Quota $file.auth_index
			if ($quota.seven_day -and $quota.seven_day.utilization -ge 100) {
				$next = if ($quota.seven_day.resets_at) { [DateTimeOffset]::Parse($quota.seven_day.resets_at).AddMinutes(2) } else { $now.AddHours(1) }
				$entry.nextCheckUtc = $next.ToUniversalTime().ToString("o")
				Save-State
				Write-Log "$($account.Name): weekly quota exhausted; next check $($next.ToLocalTime().ToString('o'))"
				continue
			}
			if ($quota.five_hour -and $quota.five_hour.resets_at) {
				$reset = [DateTimeOffset]::Parse($quota.five_hour.resets_at)
				if ($reset -gt $now.AddMinutes(1)) {
					$next = $reset.AddMinutes(2)
					$entry.nextCheckUtc = $next.ToUniversalTime().ToString("o")
					Save-State
					Write-Log "$($account.Name): window active; next check $($next.ToLocalTime().ToString('o'))"
					continue
				}
			}
			if ($entry.lastWarmupUtc) {
				$earliest = [DateTimeOffset]::Parse($entry.lastWarmupUtc).AddHours(5)
				if ($now -lt $earliest) {
					$entry.nextCheckUtc = $earliest.ToUniversalTime().ToString("o")
					Save-State
					Write-Log "$($account.Name): recent warmup; next check $($earliest.ToLocalTime().ToString('o'))"
					continue
				}
			}
			if ($DryRun) {
				Write-Log "$($account.Name): would send Haiku warmup"
				continue
			}

			# Record the attempt first so a timeout cannot lead to repeated messages.
			$entry.lastWarmupUtc = $now.ToString("o")
			$entry.nextCheckUtc = $now.AddHours(5).ToString("o")
			Save-State
			Send-Warmup $file.auth_index
			Write-Log "$($account.Name): Haiku warmup succeeded"
			try {
				$after = Get-Quota $file.auth_index
				if ($after.five_hour -and $after.five_hour.resets_at) {
					$next = [DateTimeOffset]::Parse($after.five_hour.resets_at).AddMinutes(2)
					if ($next -gt $now.AddHours(4)) {
						$entry.nextCheckUtc = $next.ToUniversalTime().ToString("o")
						Save-State
					}
				}
			} catch { Write-Log "$($account.Name): post-warmup quota check failed: $($_.Exception.Message)" }
		} catch {
			Write-Log "$($account.Name): $($_.Exception.Message)"
			if (-not $DryRun) {
				$entry = $state[$account.Name]
				if ($null -ne $entry -and (-not $entry.nextCheckUtc -or
					[DateTimeOffset]::Parse($entry.nextCheckUtc) -le [DateTimeOffset]::UtcNow)) {
					$entry.nextCheckUtc = [DateTimeOffset]::UtcNow.AddHours(1).ToString("o")
					Save-State
				}
			}
		}
	}
} catch {
	Write-Log "Scheduler error: $($_.Exception.Message)"
	exit 1
} finally {
	$mutex.ReleaseMutex()
	$mutex.Dispose()
}
