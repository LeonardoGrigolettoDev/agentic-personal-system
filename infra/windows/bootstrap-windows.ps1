#Requires -RunAsAdministrator
<#
.SYNOPSIS
  Windows side of the AI Agent OS setup: WSL2 + Ubuntu 24.04, the WSL VM memory budget and Docker Desktop.

.DESCRIPTION
  Idempotent. Run in an elevated PowerShell:
      powershell -ExecutionPolicy Bypass -File .\bootstrap-windows.ps1
  Then reboot if asked, open "Ubuntu 24.04" once to create your Linux user, and follow docs/RUNBOOK.md
  section 1 (Windows): enable WSL integration in Docker Desktop, clone the repo INSIDE Ubuntu and run
  infra/scripts/bootstrap-host.sudo.sh there.

  Everything else (Makefile, compose, scripts) runs inside WSL2, exactly as on Linux.
  Ollama is not installed here yet (local models are configured later).

.PARAMETER Distro
  WSL distribution to install (default Ubuntu-24.04: the repo expects Ubuntu noble).
.PARAMETER WslMemoryGB
  Memory cap of the WSL2 VM, which also hosts Docker Desktop's engine. 10 GB leaves ~6 GB to Windows on a
  16 GB machine; the stack itself needs ~4 GB (core + Hermes + sandbox), more with whisper/edge.
.PARAMETER WslSwapGB
  Swap of the WSL2 VM.
.PARAMETER Force
  Overwrite an existing %UserProfile%\.wslconfig (a .bak copy is kept).
#>
[CmdletBinding()]
param(
  [string]$Distro = 'Ubuntu-24.04',
  [ValidateRange(4, 256)][int]$WslMemoryGB = 10,
  [ValidateRange(0, 64)][int]$WslSwapGB = 8,
  [switch]$Force
)

$ErrorActionPreference = 'Stop'
$env:WSL_UTF8 = '1'  # wsl.exe prints UTF-16 otherwise
function Step([string]$Message) { Write-Host "`n==> $Message" -ForegroundColor Cyan }
$needsReboot = $false

Step 'Windows version'
$os = Get-CimInstance Win32_OperatingSystem
$build = [int]$os.BuildNumber
if ($build -lt 19045) { throw "Windows 10 22H2 (build 19045) or Windows 11 is required; this is build $build." }
Write-Host "$($os.Caption) build $build"

Step 'WSL2'
& wsl.exe --status *> $null
if ($LASTEXITCODE -ne 0) {
  & wsl.exe --install --no-distribution
  if ($LASTEXITCODE -ne 0) { throw 'wsl --install failed (is virtualization enabled in the BIOS/UEFI?)' }
  $needsReboot = $true
} else {
  & wsl.exe --update
}
& wsl.exe --set-default-version 2 | Out-Null

Step "WSL distribution $Distro"
$installed = @(& wsl.exe --list --quiet 2>$null | ForEach-Object { $_.Trim() } | Where-Object { $_ })
if ($installed -contains $Distro) {
  Write-Host "$Distro is already installed"
} elseif ($needsReboot) {
  Write-Host "Reboot first, then run this script again to install $Distro."
} else {
  & wsl.exe --install --distribution $Distro --no-launch
  if ($LASTEXITCODE -ne 0) { throw "wsl --install --distribution $Distro failed" }
  Write-Host "$Distro installed. Open it once from the Start menu to create your Linux user."
}

Step '.wslconfig (memory of the WSL2 VM, which also runs Docker Desktop)'
$wslConfig = Join-Path $env:USERPROFILE '.wslconfig'
$content = @"
# AI Agent OS: WSL2 VM budget (Docker Desktop runs in this VM too). Apply with: wsl --shutdown
[wsl2]
memory=${WslMemoryGB}GB
swap=${WslSwapGB}GB

[general]
# keep the Ubuntu distro (systemd timers) alive while Windows is on
instanceIdleTimeout=-1

[experimental]
autoMemoryReclaim=gradual
sparseVhd=true
"@
if ((Test-Path $wslConfig) -and -not $Force) {
  Write-Warning "$wslConfig exists and was left untouched. Recommended content (rerun with -Force to write it):"
  Write-Host $content
} else {
  if (Test-Path $wslConfig) { Copy-Item $wslConfig "$wslConfig.bak" -Force }
  Set-Content -Path $wslConfig -Value $content -Encoding ascii
  Write-Host "wrote $wslConfig (memory=${WslMemoryGB}GB, swap=${WslSwapGB}GB)"
}

Step 'Docker Desktop'
$dockerExe = Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'
if (Test-Path $dockerExe) {
  Write-Host 'Docker Desktop is already installed'
} else {
  if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    throw 'winget not found: install "App Installer" from the Microsoft Store, or Docker Desktop by hand.'
  }
  & winget install --id Docker.DockerDesktop --exact --accept-source-agreements --accept-package-agreements
  if ($LASTEXITCODE -ne 0) { throw 'winget could not install Docker Desktop' }
  $needsReboot = $true
}

Step 'Next steps'
if ($needsReboot) { Write-Host '0. REBOOT Windows (WSL and/or Docker Desktop were just installed), then rerun this script.' }
@"
1. Start Docker Desktop: Settings > General > 'Use the WSL 2 based engine' and 'Start Docker Desktop when
   you sign in' on; Settings > Resources > WSL integration > enable '$Distro' > Apply & restart.
2. Open '$Distro', create your user, then inside Ubuntu:
     git clone git@github.com:LeonardoGrigolettoDev/agentic-personal-system.git ~/agent-system
     cd ~/agent-system && sudo bash infra/scripts/bootstrap-host.sudo.sh
3. In PowerShell: wsl --shutdown   (applies .wslconfig and systemd), reopen Ubuntu, then:
     cd ~/agent-system && make tools env doctor
4. Optional, nightly backup + Hermes restart from Windows:
     powershell -ExecutionPolicy Bypass -File infra\windows\register-tasks.ps1
   (run it from the repo folder as seen by Windows: \\wsl.localhost\$Distro\home\<user>\agent-system)
"@ | Write-Host
