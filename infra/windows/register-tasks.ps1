<#
.SYNOPSIS
  Windows Task Scheduler jobs for the AI Agent OS running in WSL2 (same jobs as infra/systemd on Linux).

.DESCRIPTION
  Run as your normal user (no elevation needed):
      powershell -ExecutionPolicy Bypass -File register-tasks.ps1
  Creates, under \AIOS\ in Task Scheduler:
    - backup          daily 03:00  bash infra/scripts/backup.sh  (pg_dump + Hermes state + .env copy)
    - hermes-restart  daily 04:00  docker compose --profile agent restart hermes
  Both run only while you are signed in (WSL and Docker Desktop live in your session) and catch up at the
  next opportunity when the PC was off or asleep at that time. -Unregister removes them.

.PARAMETER Distro
  WSL distribution that holds the repo.
.PARAMETER RepoPath
  Repo path inside that distribution.
#>
[CmdletBinding()]
param(
  [string]$Distro = 'Ubuntu-24.04',
  [string]$RepoPath = '~/agent-system',
  [switch]$Unregister
)

$ErrorActionPreference = 'Stop'
$taskPath = '\AIOS\'
$jobs = @(
  @{ Name = 'backup'; At = '03:00'; Command = 'bash infra/scripts/backup.sh';
     Description = 'AI Agent OS: pg_dump, Hermes state and .env copy into backups/<ts> (14 days kept)' },
  @{ Name = 'hermes-restart'; At = '04:00'; Command = 'docker compose --profile agent restart hermes';
     Description = 'AI Agent OS: nightly Hermes gateway restart' }
)

if ($Unregister) {
  foreach ($job in $jobs) {
    Unregister-ScheduledTask -TaskPath $taskPath -TaskName $job.Name -Confirm:$false -ErrorAction SilentlyContinue
  }
  Write-Host 'AIOS scheduled tasks removed.'
  return
}

if ($RepoPath -notmatch '^[~/][A-Za-z0-9._/-]*$') { throw "RepoPath must be a Linux path, got '$RepoPath'" }
& wsl.exe --distribution $Distro -- bash -lc "test -f $RepoPath/Makefile"
if ($LASTEXITCODE -ne 0) { throw "no repo at $RepoPath in WSL distribution '$Distro' (clone it there first)" }
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -ExecutionTimeLimit (New-TimeSpan -Hours 2) -MultipleInstances IgnoreNew

foreach ($job in $jobs) {
  $script = "cd $RepoPath && $($job.Command)"
  $action = New-ScheduledTaskAction -Execute 'wsl.exe' -Argument "--distribution $Distro -- bash -lc `"$script`""
  $trigger = New-ScheduledTaskTrigger -Daily -At $job.At
  Register-ScheduledTask -TaskPath $taskPath -TaskName $job.Name -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Description $job.Description -Force | Out-Null
  Write-Host ("registered {0}{1} at {2}: {3}" -f $taskPath, $job.Name, $job.At, $script)
}
Write-Host "Check them in Task Scheduler (taskschd.msc) under AIOS, or: Get-ScheduledTask -TaskPath '$taskPath'"
