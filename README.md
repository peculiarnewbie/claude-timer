# claude-timer

Start each Claude subscription's five-hour window with one tiny Haiku request when its previous window resets. The request goes directly through CLIProxyAPI's account-pinned management API, so it does not create Claude Code chat history.

Windows support is in [`windows/`](windows/), and Linux support is in [`linux/`](linux/). Both schedulers wake every 10 minutes by default, but saved reset times let them skip quota API calls until an account is due.

## Requirements

- Linux with Python 3.9 or newer and systemd user services, or Windows with PowerShell 7.5 or newer (`pwsh`) and Task Scheduler.
- [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) running locally, with Claude subscriptions already signed in and the management API enabled.
- The plaintext CLIProxyAPI management key. The Linux installer can read an existing management-token file. Both installers can use T3 settings when they contain a plaintext key; masked values such as `••••••` cannot be used.

The repo contains no CLIProxyAPI binary, OAuth credentials, or management key. Set up CLIProxyAPI and sign in to the accounts on each machine first.

## Linux

From the repo root, run as your regular user:

```sh
python3 linux/install.py \
  --account-emails first@example.com second@example.com \
  --management-key-file "$HOME/.cli-proxy-api/management-token" \
  --proxy-service cliproxyapi.service \
  --enable-linger
```

`--all-accounts` selects all currently enabled Claude accounts instead of listing emails. The installer validates accounts against the running proxy, copies the standalone Python scheduler to `~/.local/share/claude-timer/`, and enables `claude-timer.timer` under `~/.config/systemd/user/`. It installs a `Type=oneshot` service that the timer activates every 10 minutes, with an initial check after one minute. No Python packages or root installation are needed.

`--enable-linger` uses `loginctl enable-linger` to keep the user service manager running after logout and start it at boot, before login. This may require authentication on some distributions. The enabled timer survives reboots, catches up on missed calendar ticks, and resumes checking after sleep; it does not wake a sleeping laptop. The existing `cliproxyapi.service` is started as a dependency when `--proxy-service` is supplied. Use the name of your existing CLIProxyAPI user service, or omit this option when another service manages the proxy.

The management key stays in its original file when `--management-key-file` is used. Without that option, the installer tries `~/.cli-proxy-api/management-token`, then a matching plaintext T3 usage source, then a hidden interactive prompt. `--management-key-source t3` or `prompt` selects either explicitly. `--proxy-uri` changes the local address, `--account-names work personal` adds readable labels, and `--interval-minutes` sets a calendar tick interval from 1 to 1440 minutes. Ticks are spaced from midnight each day; intervals that do not divide 1440 have a shorter gap at midnight. Accounts added to the proxy later require running the installer again.

Configuration, state, and logs stay in the local install directory with owner-only permissions. The key file or T3 settings must remain accessible to your user. `XDG_DATA_HOME` and `XDG_CONFIG_HOME` override the default directories.

Check the installation:

```sh
python3 "$HOME/.local/share/claude-timer/warmup.py" --dry-run
systemctl --user status claude-timer.timer
systemctl --user list-timers claude-timer.timer
journalctl --user -u claude-timer.service -n 20
tail -n 20 "$HOME/.local/share/claude-timer/warmup.log"
loginctl show-user "$USER" -p Linger
```

`--dry-run` may query quota when an account is due, but never sends a Haiku request or changes saved state or logs. The scheduler persists each account's next check in `warmup-state.json`, two minutes after an active window's reset. A weekly quota at 100% postpones checks until the weekly reset. It records each warmup attempt before sending, so a timeout cannot trigger a duplicate request for five hours. Quota errors postpone checks for one hour and never trigger a warmup. A process lock prevents overlapping runs.

To stop or remove it:

```sh
systemctl --user disable --now claude-timer.timer
python3 linux/uninstall.py
```

Uninstall leaves local configuration, state, and logs for inspection. It also leaves user lingering enabled, since other user services may depend on it.

Run Linux integration tests with `python3 -m unittest discover -s linux -v`. These use a local HTTP server and do not contact Anthropic or consume quota.

## Windows install

Clone this repo, open PowerShell 7.5 or newer, and run from the repo root:

```powershell
./windows/install.ps1 -AccountEmails "first@example.com","second@example.com"
```

The installer checks that both accounts are present in CLIProxyAPI, copies `windows/warmup.ps1` to `%LOCALAPPDATA%\CLIProxyAPI`, writes a local `warmup-config.json`, and registers the `claude-timer` task under your Windows account. It uses T3's local management key if available; otherwise it prompts for the key without echoing it. The config and key remain on that PC. If your proxy uses a different location or port, pass `-ProxyUri`, `-ProxyExecutable`, and `-ProxyConfig`. To set readable account labels, use `-AccountNames "work","personal"`. `-IntervalMinutes` changes the task tick interval.

The task runs while your Windows user is signed in, including when the screen is locked. If the proxy is down and its executable path exists, the task starts it in a hidden window.

## Windows checks

```powershell
& "$env:LOCALAPPDATA\CLIProxyAPI\warmup.ps1" -DryRun
Get-ScheduledTaskInfo -TaskName claude-timer
Get-Content "$env:LOCALAPPDATA\CLIProxyAPI\warmup.log" -Tail 20
```

`-DryRun` may query quota when an account is due, but never sends a Haiku request or changes saved state. The task persists its next check per account in `warmup-state.json`. When a window is active, it schedules the next check two minutes after the reported reset. If a warmup is attempted, it records the attempt before sending to prevent repeated messages after a timeout. A weekly quota at 100% delays the next check until the weekly reset.

To stop it, run `./windows/uninstall.ps1`. This removes the task and leaves local state and configuration for inspection.

## API details

CLIProxyAPI's [management API](https://help.router-for.me/management/api) supplies each account's `auth_index` and forwards account-pinned upstream calls. The scheduler reads Anthropic's OAuth usage response for the five-hour `resets_at` and sends a `claude-haiku-4-5-20251001` Messages request only after the window expires. The OAuth usage response is an upstream API and could change; errors are logged and do not trigger a warmup.
