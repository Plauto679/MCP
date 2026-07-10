param(
    [int]$Bursts = 6,
    [int]$IntervalSeconds = 3600,
    [int]$MaxOutputMb = 500
)

$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$logPath = Join-Path $RepoRoot "data\market_ws\ws_bursts_supervisor_$stamp.log"

function Write-BurstLog {
    param([string]$Message)
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message"
    Add-Content -Path $logPath -Value $line
}

function Get-DirectorySizeMb {
    param([string]$Path)
    if (-not (Test-Path $Path)) {
        return 0
    }
    $sum = (Get-ChildItem -Path $Path -File -Recurse | Measure-Object -Property Length -Sum).Sum
    if ($null -eq $sum) {
        return 0
    }
    return [math]::Round($sum / 1MB, 3)
}

function Get-FullRecorderProcesses {
    Get-CimInstance Win32_Process -Filter "name = 'python.exe'" |
        Where-Object { $_.CommandLine -like "*record_market_ws.py*" }
}

Write-BurstLog "WS burst supervisor started bursts=$Bursts interval_s=$IntervalSeconds mode=continuous_one_window max_output_mb=$MaxOutputMb."

for ($index = 1; $index -le $Bursts; $index++) {
    $sizeMb = Get-DirectorySizeMb "data\market_ws"
    if ($sizeMb -ge $MaxOutputMb) {
        Write-BurstLog "Disk guard stop before burst=$index size_mb=$sizeMb max_output_mb=$MaxOutputMb."
        break
    }

    while (@(Get-FullRecorderProcesses).Count -gt 0) {
        Write-BurstLog "Existing full WS recorder active; waiting 30s before burst=$index."
        Start-Sleep -Seconds 30
    }

    $burstStamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $out = Join-Path $RepoRoot "data\market_ws\ws_burst_${burstStamp}.out.log"
    $err = Join-Path $RepoRoot "data\market_ws\ws_burst_${burstStamp}.err.log"
    Write-BurstLog "Starting burst=$index output_size_mb=$sizeMb."

    $process = Start-Process -FilePath ".\.venv\Scripts\python.exe" `
        -ArgumentList @(
            "research\record_market_ws.py",
            "--continuous",
            "--max-windows", "1",
            "--window-grace-seconds", "3",
            "--no-raw",
            "--storage", "sqlite",
            "--summary-interval-ms", "250"
        ) `
        -WorkingDirectory $RepoRoot `
        -RedirectStandardOutput $out `
        -RedirectStandardError $err `
        -WindowStyle Hidden `
        -PassThru

    Wait-Process -Id $process.Id
    $errSize = (Get-Item $err -ErrorAction SilentlyContinue).Length
    $newSizeMb = Get-DirectorySizeMb "data\market_ws"
    Write-BurstLog "Completed burst=$index process_id=$($process.Id) err_bytes=$errSize output_size_mb=$newSizeMb."

    if ($index -lt $Bursts) {
        Start-Sleep -Seconds $IntervalSeconds
    }
}

Write-BurstLog "WS burst supervisor complete."
