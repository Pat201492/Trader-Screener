# Monthly dealer-hedging re-run (#361): refresh the real dataset, then run the
# declared 27-trial grid (and, once 60+ days of point-in-time OI exist, the
# OI-matched grid) and write Research/dealer_hedging_results.json -- what the
# Dealer Hedging tab shows. The build is cached, so only new days are fetched.
#
# Install (1st of each month, 23:30 -- after the 22:00 nightly pipeline):
#   powershell -ExecutionPolicy Bypass -File Research\run_dealer_hedging_monthly.ps1 -Install
# Remove:
#   powershell -ExecutionPolicy Bypass -File Research\run_dealer_hedging_monthly.ps1 -Uninstall
# Run now:
#   powershell -ExecutionPolicy Bypass -File Research\run_dealer_hedging_monthly.ps1
#
# Log: ~/.edgar-scrubber/research/dealer_hedging/monthly.log
param([switch]$Install, [switch]$Uninstall, [string]$TaskName = 'DealerHedgingMonthly')

$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task '$TaskName'."
    } else { Write-Host "No scheduled task named '$TaskName'." }
    return
}

if ($Install) {
    # The ScheduledTasks cmdlets have no monthly trigger; schtasks does.
    # schtasks wants the inner quotes as \" ; PowerShell needs `" to emit a quote.
    $tr = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File \`"$PSCommandPath\`""
    & schtasks.exe /Create /F /TN $TaskName /SC MONTHLY /D 1 /ST 23:30 /RL LIMITED /TR $tr | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "schtasks /Create failed ($LASTEXITCODE)" }
    # Run late if the PC was off/asleep on the 1st; never two at once.
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 12) -MultipleInstances IgnoreNew
    Set-ScheduledTask -TaskName $TaskName -Settings $settings | Out-Null
    Write-Host "Installed '$TaskName' (1st of each month, 23:30)."
    return
}

$dir = Join-Path $env:USERPROFILE '.edgar-scrubber\research\dealer_hedging'
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$log = Join-Path $dir 'monthly.log'
function Log($m) { Add-Content -Path $log -Value ("[{0}] {1}" -f (Get-Date -Format s), $m) }

# The SEC requires a contactable user agent; it lives in the user's env.
if (-not $env:EDGAR_USER_AGENT) {
    $env:EDGAR_USER_AGENT = [Environment]::GetEnvironmentVariable('EDGAR_USER_AGENT', 'User')
}
Set-Location $repo
Log "start (repo $repo)"
& cmd.exe /c "python -u Research\dealer_hedging_data.py build --months 36 --per-month 4 >> `"$log`" 2>&1"
$build = $LASTEXITCODE
Log "build exit $build"
if ($build -ne 0) { Log 'build failed -- real run skipped'; exit $build }
& cmd.exe /c "python -u Research\dealer_hedging_causal_test.py --real >> `"$log`" 2>&1"
$real = $LASTEXITCODE
Log "real run exit $real"
exit $real
