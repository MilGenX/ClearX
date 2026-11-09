# Run in Windows (Boot Camp), PowerShell as Administrator, from this folder.
# Needs: amdvbflash.exe (AMD's flash tool, from TechPowerUp) and Python 3 on PATH.
#   .\flash.ps1 backup              -> two backups, verified identical
#   .\flash.ps1 flash  modded.rom   -> verifies, then flashes
#   .\flash.ps1 restore             -> flashes backup1.rom back
param([Parameter(Mandatory)][ValidateSet("backup","flash","restore")]$Cmd, $Rom)
$ErrorActionPreference = "Stop"
$tool = ".\amdvbflash.exe"
if (!(Test-Path $tool)) { throw "Put amdvbflash.exe in this folder first." }

function Adapter {
  $list = & $tool -i
  Write-Host $list
  $amd = $list | Select-String "Polaris|575|67DF|67EF" | Select-Object -First 1
  if (!$amd) { throw "No Polaris adapter found in amdvbflash -i output." }
  return ($amd.Line.Trim() -split "\s+")[0]
}

switch ($Cmd) {
  "backup" {
    $a = Adapter
    & $tool -s $a backup1.rom; & $tool -s $a backup2.rom
    if ((Get-FileHash backup1.rom).Hash -ne (Get-FileHash backup2.rom).Hash) { throw "Backups differ — do NOT flash." }
    python romtool.py check backup1.rom; if ($LASTEXITCODE) { throw "Backup failed checks." }
    Write-Host "Backup verified. Copy backup1.rom to a USB drive AND cloud storage before going further." -Fore Green
  }
  "flash" {
    if (!$Rom) { throw "Usage: .\flash.ps1 flash modded.rom" }
    if (!(Test-Path backup1.rom)) { throw "Run backup first." }
    python romtool.py compare backup1.rom $Rom; if ($LASTEXITCODE) { throw "Safety check failed — not flashing." }
    if (!(Get-Service sshd -ErrorAction SilentlyContinue) -or (Get-Service sshd).Status -ne "Running") {
      Write-Warning "OpenSSH Server isn't running. Without it you can't restore blind if the screen goes dark."
    }
    $c = Read-Host "Type FLASH to write $Rom to the GPU (stay plugged in, don't interrupt)"
    if ($c -ne "FLASH") { "Cancelled."; exit }
    & $tool -p (Adapter) $Rom
    Write-Host "Done. Shut down fully (not restart), then boot." -Fore Green
  }
  "restore" { & $tool -p (Adapter) backup1.rom; Write-Host "Original vBIOS restored. Shut down fully." }
}
