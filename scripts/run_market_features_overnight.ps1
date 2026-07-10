param(
    [int]$RuntimeSeconds = 28800,
    [int]$SampleIntervalMs = 500,
    [int]$MaxOutputMb = 300,
    [string]$LabelsCsv = "data\market_features_analysis\binance_5m_labels_20260706_capture.csv"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$supervisorLog = Join-Path $RepoRoot "data\market_features\overnight_supervisor_$stamp.log"

function Write-SupervisorLog {
    param([string]$Message)
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message"
    Add-Content -Path $supervisorLog -Value $line
}

function Get-RecorderProcesses {
    Get-CimInstance Win32_Process -Filter "name = 'python.exe'" |
        Where-Object { $_.CommandLine -like "*record_market_features.py*" }
}

Write-SupervisorLog "Supervisor started. Waiting for existing record_market_features.py processes to finish."
while (@(Get-RecorderProcesses).Count -gt 0) {
    $count = @(Get-RecorderProcesses).Count
    Write-SupervisorLog "Existing recorder process count=$count; sleeping 60s."
    Start-Sleep -Seconds 60
}

$recordOut = Join-Path $RepoRoot "data\market_features\record_features_overnight_$stamp.out.log"
$recordErr = Join-Path $RepoRoot "data\market_features\record_features_overnight_$stamp.err.log"
Write-SupervisorLog "Starting overnight recorder runtime=$RuntimeSeconds sample_ms=$SampleIntervalMs max_mb=$MaxOutputMb."
Start-Process -FilePath ".\.venv\Scripts\python.exe" `
    -ArgumentList @(
        "research\record_market_features.py",
        "--max-runtime-seconds", "$RuntimeSeconds",
        "--sample-interval-ms", "$SampleIntervalMs",
        "--max-output-mb", "$MaxOutputMb"
    ) `
    -WorkingDirectory $RepoRoot `
    -RedirectStandardOutput $recordOut `
    -RedirectStandardError $recordErr `
    -WindowStyle Hidden

Start-Sleep -Seconds 10
while (@(Get-RecorderProcesses).Count -gt 0) {
    $count = @(Get-RecorderProcesses).Count
    Write-SupervisorLog "Overnight recorder active process count=$count; sleeping 120s."
    Start-Sleep -Seconds 120
}

Write-SupervisorLog "Recorder finished. Refreshing market feature analysis."
$analysisLog = Join-Path $RepoRoot "data\market_features\overnight_analysis_$stamp.log"
& ".\.venv\Scripts\python.exe" "research\analyze_market_features.py" "--fetch-binance-labels" "--labels-csv" $LabelsCsv `
    *> $analysisLog

Write-SupervisorLog "Running late continuation execution scanner."
$lateLog = Join-Path $RepoRoot "data\market_features\overnight_late_execution_$stamp.log"
& ".\.venv\Scripts\python.exe" "research\late_continuation_execution_scanner.py" `
    *> $lateLog

Write-SupervisorLog "Running strict late continuation execution scanner."
$strictLog = Join-Path $RepoRoot "data\market_features\overnight_late_execution_strict_$stamp.log"
& ".\.venv\Scripts\python.exe" "research\late_continuation_execution_scanner.py" `
    "--output-dir" "data\late_continuation_execution_strict" `
    "--min-train-attempts" "50" `
    "--min-test-attempts" "20" `
    "--min-train-windows" "40" `
    *> $strictLog

Write-SupervisorLog "Supervisor complete."
