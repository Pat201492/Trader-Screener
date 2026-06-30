# Creates a "Catan Options" shortcut on your Windows Desktop that launches the game.
# Run once:   powershell -ExecutionPolicy Bypass -File create_desktop_shortcut.ps1
$game = $PSScriptRoot
$bat  = Join-Path $game "run_game.bat"
$desktop = [Environment]::GetFolderPath("Desktop")
$lnk = Join-Path $desktop "Catan Options.lnk"
$py = (Get-Command python -ErrorAction SilentlyContinue).Source

$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut($lnk)
$sc.TargetPath = $bat
$sc.WorkingDirectory = $game
$sc.Description = "Catan Options - learn to trade options"
if ($py) { $sc.IconLocation = "$py,0" } else { $sc.IconLocation = "$env:SystemRoot\System32\shell32.dll,137" }
$sc.Save()
Write-Host "Created shortcut: $lnk"
