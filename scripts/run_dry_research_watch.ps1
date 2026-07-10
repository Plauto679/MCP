param(
    [int]$RuntimeSeconds = 14400,
    [int]$StructuralIntervalSeconds = 300,
    [int]$T270IntervalSeconds = 900,
    [int]$FeatureRuntimeSeconds = 14400,
    [int]$FeatureMaxOutputMb = 300,
    [int64]$HoldoutStartTs = 0,
    [switch]$SkipFeatureRecorder
)

$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$logDir = Join-Path $RepoRoot "data\dry_research_watch"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$supervisorLog = Join-Path $logDir "supervisor_$stamp.log"

function Write-WatchLog {
    param([string]$Message)
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message"
    Add-Content -Path $supervisorLog -Value $line
}

$structIterations = [Math]::Max([Math]::Ceiling($RuntimeSeconds / [double]$StructuralIntervalSeconds), 1)
$t270Iterations = [Math]::Max([Math]::Ceiling($RuntimeSeconds / [double]$T270IntervalSeconds), 1)

Write-WatchLog "Starting dry research watch runtime_s=$RuntimeSeconds structural_iterations=$structIterations t270_iterations=$t270Iterations."

$structOut = Join-Path $logDir "structural_$stamp.out.log"
$structErr = Join-Path $logDir "structural_$stamp.err.log"
Start-Process -FilePath ".\.venv\Scripts\python.exe" `
    -ArgumentList @(
        "research\structural_arbitrage_scanner.py",
        "--output-dir", "data\structural_arbitrage",
        "--iterations", "$structIterations",
        "--interval-seconds", "$StructuralIntervalSeconds",
        "--market-limit", "220",
        "--event-limit", "30",
        "--max-book-tokens", "900",
        "--min-gross-edge", "0.001",
        "--min-net-edge", "0",
        "--min-top-shares", "5"
    ) `
    -WorkingDirectory $RepoRoot `
    -RedirectStandardOutput $structOut `
    -RedirectStandardError $structErr `
    -WindowStyle Hidden
Write-WatchLog "Structural arbitrage scanner launched out=$structOut err=$structErr."

$t270Out = Join-Path $logDir "t270_$stamp.out.log"
$t270Err = Join-Path $logDir "t270_$stamp.err.log"
$t270Args = @(
    "research\t270_frozen_validator.py",
    "--output-dir", "data\t270_frozen_validation",
    "--fetch-binance-labels",
    "--iterations", "$t270Iterations",
    "--interval-seconds", "$T270IntervalSeconds",
    "--label-sleep-seconds", "0",
    "--settle-lag-seconds", "20"
)
if ($HoldoutStartTs -gt 0) {
    $t270Args += @("--holdout-start-ts", "$HoldoutStartTs")
}
Start-Process -FilePath ".\.venv\Scripts\python.exe" `
    -ArgumentList $t270Args `
    -WorkingDirectory $RepoRoot `
    -RedirectStandardOutput $t270Out `
    -RedirectStandardError $t270Err `
    -WindowStyle Hidden
Write-WatchLog "Frozen t270 validator launched out=$t270Out err=$t270Err."

if ($SkipFeatureRecorder) {
    Write-WatchLog "Skipping queued lightweight feature recorder by request."
} else {
    $featureOut = Join-Path $logDir "features_after_current_$stamp.out.log"
    $featureErr = Join-Path $logDir "features_after_current_$stamp.err.log"
    Start-Process -FilePath "powershell.exe" `
        -ArgumentList @(
            "-NoProfile",
            "-ExecutionPolicy", "Bypass",
            "-File", "scripts\run_market_features_overnight.ps1",
            "-RuntimeSeconds", "$FeatureRuntimeSeconds",
            "-SampleIntervalMs", "500",
            "-MaxOutputMb", "$FeatureMaxOutputMb",
            "-LabelsCsv", "data\t270_frozen_validation\binance_5m_labels.csv"
        ) `
        -WorkingDirectory $RepoRoot `
        -RedirectStandardOutput $featureOut `
        -RedirectStandardError $featureErr `
        -WindowStyle Hidden
    Write-WatchLog "Queued next lightweight feature recorder. It waits for any active recorder before starting."
}
Write-WatchLog "Dry research watch launch complete."
