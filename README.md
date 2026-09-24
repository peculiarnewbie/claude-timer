# Claude window starter for CLIProxyAPI

A Windows task that checks each selected Claude subscription's five-hour reset and sends one tiny Haiku request when a new window needs starting. The task wakes every 10 minutes by default, but saved reset times let it skip quota API calls until an account is due. The message goes directly through CLIProxyAPI's account-pinned management API, so it does not create Claude Code chat history.

## Requirements

- Windows with PowerShell 7.5 or newer (`pwsh`) and Task Scheduler.
- [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) running locally, with Claude subscriptions already signed in and the management API enabled.
- The plaintext CLIProxyAPI management key. If T3 Code has a local CLIProxyAPI hub configured, the installer can read its key from T3 settings instead.

The repo contains no CLIProxyAPI binary, OAuth credentials, or management key. Set up CLIProxyAPI and sign in to the accounts on each PC first.

## Install

Clone this repo, open PowerShell 7, and run:

```powershell
./install.ps1 -AccountEmails "first@example.com","second@example.com"
```

The installer checks that both accounts are present in CLIProxyAPI, copies `warmup.ps1` to `%LOCALAPPDATA%\CLIProxyAPI`, writes a local `warmup-config.json`, and registers `CLIProxyAPI-Claude-Window` under your Windows account. It uses T3's local management key if available; otherwise it prompts for the key without echoing it. The config and key remain on that PC. If your proxy uses a different location or port, pass `-ProxyUri`, `-ProxyExecutable`, and `-ProxyConfig`. To set readable account labels, use `-AccountNames "work","personal"`. `-IntervalMinutes` changes the task tick interval.

The task runs while your Windows user is signed in, including when the screen is locked. If the proxy is down and its executable path exists, the task starts it in a hidden window.

## Check it

```powershell
& "$env:LOCALAPPDATA\CLIProxyAPI\warmup.ps1" -DryRun
Get-ScheduledTaskInfo -TaskName CLIProxyAPI-Claude-Window
Get-Content "$env:LOCALAPPDATA\CLIProxyAPI\warmup.log" -Tail 20
```

`-DryRun` may query quota when an account is due, but never sends a Haiku request or changes saved state. The task persists its next check per account in `warmup-state.json`. When a window is active, it schedules the next check two minutes after the reported reset. If a warmup is attempted, it records the attempt before sending to prevent repeated messages after a timeout. A weekly quota at 100% delays the next check until the weekly reset.

To stop it, run `./uninstall.ps1`. This removes the task and leaves local state and configuration for inspection.

## API details

CLIProxyAPI's [management API](https://help.router-for.me/management/api) supplies each account's `auth_index` and forwards account-pinned upstream calls. The scheduler reads Anthropic's OAuth usage response for the five-hour `resets_at` and sends a `claude-haiku-4-5-20251001` Messages request only after the window expires. The OAuth usage response is an upstream API and could change; errors are logged and do not trigger a warmup.
