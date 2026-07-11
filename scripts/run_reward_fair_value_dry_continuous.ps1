param(
    [string]$RunName = "reward_fair_value_dry_live",
    [int]$CycleSeconds = 600,
    [int]$StopAfterCycles = 0,
    [int]$RewardMaxMarkets = 300,
    [double]$RewardMinDailyRate = 5.0,
    [double]$RewardMinVolume24h = 500.0,
    [double]$RewardMinHoursToEnd = 1.0,
    [int]$RewardRowsPerScan = 80,
    [int]$ExternalMaxMarkets = 800,
    [double]$ExternalMinVolume24h = 100.0,
    [double]$ExternalMaxYesSpread = 0.15,
    [int]$ExternalRowsPerScan = 120,
    [int]$MaxPaperEvents = 200000,
    [int]$PaperEveryCycles = 1,
    [int]$AnalysisEveryCycles = 3,
    [double]$MaxSnapshotAgeHours = 24.0,
    [double]$MaxHistoryMb = 128.0,
    [string]$ShadowActiveOutputDir = "data\shadow_paper_trader_active_live",
    [string]$ShadowHoldOutputDir = "data\shadow_paper_trader_hold_live",
    [string]$ShadowTakerActiveOutputDir = "data\shadow_paper_trader_taker_active_live",
    [string]$ShadowTakerHoldOutputDir = "data\shadow_paper_trader_taker_hold_live"
)

$ErrorActionPreference = "Continue"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    $Python = "python"
}

$RunDir = Join-Path $RepoRoot "data\$RunName"
$LogDir = Join-Path $RunDir "logs"
$HeartbeatPath = Join-Path $RunDir "heartbeat.json"
$PidPath = Join-Path $RunDir "supervisor.pid"
$SupervisorLog = Join-Path $RunDir "supervisor.log"

New-Item -ItemType Directory -Force -Path $RunDir | Out-Null
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

if (Test-Path $PidPath) {
    $oldPidText = (Get-Content -Path $PidPath -ErrorAction SilentlyContinue | Select-Object -First 1)
    $oldPid = 0
    if ([int]::TryParse([string]$oldPidText, [ref]$oldPid)) {
        $oldProcess = Get-Process -Id $oldPid -ErrorAction SilentlyContinue
        if ($oldProcess) {
            throw "Continuous dry supervisor already appears to be running with pid=$oldPid."
        }
    }
}
Set-Content -Path $PidPath -Value $PID -Encoding ASCII

function Write-SupervisorLog {
    param([string]$Message)
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message"
    Add-Content -Path $SupervisorLog -Value $line -Encoding UTF8
    Write-Host $line
}

function Rotate-IfLarge {
    param(
        [string]$Path,
        [double]$MaxMb
    )
    $item = Get-Item -LiteralPath $Path -ErrorAction SilentlyContinue
    if (-not $item) {
        return
    }
    if ($item.Length -lt ($MaxMb * 1MB)) {
        return
    }
    $archiveDir = Join-Path $item.DirectoryName "archive"
    New-Item -ItemType Directory -Force -Path $archiveDir | Out-Null
    $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $target = Join-Path $archiveDir ("{0}_{1}{2}" -f $item.BaseName, $stamp, $item.Extension)
    Move-Item -LiteralPath $item.FullName -Destination $target
    Write-SupervisorLog "rotated path=$Path archived_to=$target size_mb=$([math]::Round($item.Length / 1MB, 2))"
}

function Invoke-Step {
    param(
        [string]$Name,
        [string[]]$Arguments
    )
    $outPath = Join-Path $LogDir "$Name.last.out.log"
    $errPath = Join-Path $LogDir "$Name.last.err.log"
    Write-SupervisorLog "step_start name=$Name"
    & $Python @Arguments > $outPath 2> $errPath
    $exitCode = $LASTEXITCODE
    Write-SupervisorLog "step_done name=$Name exit=$exitCode out=$outPath err=$errPath"
    return $exitCode
}

function Write-Heartbeat {
    param(
        [int]$Cycle,
        [datetime]$CycleStart,
        [datetime]$CycleEnd,
        [hashtable]$ExitCodes,
        [int]$SleepSeconds
    )
    $heartbeat = [ordered]@{
        run_name = $RunName
        mode = "dry_research_only"
        pid = $PID
        cycle = $Cycle
        cycle_start = $CycleStart.ToString("o")
        cycle_end = $CycleEnd.ToString("o")
        next_cycle_after_seconds = $SleepSeconds
        exit_codes = $ExitCodes
        outputs = [ordered]@{
            market_making_history = "data\market_making\candidate_history.csv"
            external_fair_value_history = "data\external_fair_value\candidate_history.csv"
            maker_paper_report = "data\market_making_paper_live\report.json"
            evaluator_report = "data\reward_fair_value_evaluator_live\report.json"
            shadow_paper_active_report = "$ShadowActiveOutputDir\report.json"
            shadow_paper_hold_report = "$ShadowHoldOutputDir\report.json"
            shadow_paper_taker_active_report = "$ShadowTakerActiveOutputDir\report.json"
            shadow_paper_taker_hold_report = "$ShadowTakerHoldOutputDir\report.json"
            shadow_action_history = "data\reward_fair_value_evaluator_live\shadow_action_history.csv"
            supervisor_log = "data\$RunName\supervisor.log"
        }
    }
    $heartbeat | ConvertTo-Json -Depth 5 | Set-Content -Path $HeartbeatPath -Encoding UTF8
}

Write-SupervisorLog "continuous dry supervisor starting pid=$PID cycle_seconds=$CycleSeconds stop_after_cycles=$StopAfterCycles"
Write-SupervisorLog "safety mode=dry_research_only no_auth no_orders"

$cycle = 0
while ($true) {
    $cycle += 1
    $cycleStart = Get-Date
    $exitCodes = @{}
    Write-SupervisorLog "cycle_start cycle=$cycle"

    Rotate-IfLarge -Path "data\market_making\candidate_history.csv" -MaxMb $MaxHistoryMb
    Rotate-IfLarge -Path "data\market_making\scan_log.jsonl" -MaxMb $MaxHistoryMb
    Rotate-IfLarge -Path "data\external_fair_value\candidate_history.csv" -MaxMb $MaxHistoryMb
    Rotate-IfLarge -Path "data\external_fair_value\scan_log.jsonl" -MaxMb $MaxHistoryMb
    Rotate-IfLarge -Path "data\reward_fair_value_evaluator_live\shadow_action_history.csv" -MaxMb $MaxHistoryMb
    Rotate-IfLarge -Path "$ShadowActiveOutputDir\paper_orders.csv" -MaxMb $MaxHistoryMb
    Rotate-IfLarge -Path "$ShadowActiveOutputDir\paper_order_events.csv" -MaxMb $MaxHistoryMb
    Rotate-IfLarge -Path "$ShadowHoldOutputDir\paper_orders.csv" -MaxMb $MaxHistoryMb
    Rotate-IfLarge -Path "$ShadowHoldOutputDir\paper_order_events.csv" -MaxMb $MaxHistoryMb
    Rotate-IfLarge -Path "$ShadowTakerActiveOutputDir\paper_orders.csv" -MaxMb $MaxHistoryMb
    Rotate-IfLarge -Path "$ShadowTakerActiveOutputDir\paper_order_events.csv" -MaxMb $MaxHistoryMb
    Rotate-IfLarge -Path "$ShadowTakerHoldOutputDir\paper_orders.csv" -MaxMb $MaxHistoryMb
    Rotate-IfLarge -Path "$ShadowTakerHoldOutputDir\paper_order_events.csv" -MaxMb $MaxHistoryMb
    Rotate-IfLarge -Path $SupervisorLog -MaxMb 32.0

    $rewardArgs = @(
        "research\market_making_scanner.py",
        "--output-dir", "data\market_making",
        "--max-markets", "$RewardMaxMarkets",
        "--request-timeout-s", "30",
        "--min-daily-rate", "$RewardMinDailyRate",
        "--min-volume-24hr", "$RewardMinVolume24h",
        "--min-hours-to-end", "$RewardMinHoursToEnd",
        "--max-history-rows-per-scan", "$RewardRowsPerScan",
        "--iterations", "1"
    )
    $exitCodes["market_making_scan"] = Invoke-Step -Name "market_making_scan" -Arguments $rewardArgs

    $externalArgs = @(
        "research\external_fair_value_scanner.py",
        "--output-dir", "data\external_fair_value",
        "--max-markets", "$ExternalMaxMarkets",
        "--request-timeout-s", "30",
        "--min-volume-24hr", "$ExternalMinVolume24h",
        "--max-yes-spread", "$ExternalMaxYesSpread",
        "--max-history-rows-per-scan", "$ExternalRowsPerScan",
        "--iterations", "1"
    )
    $exitCodes["external_fair_value_scan"] = Invoke-Step -Name "external_fair_value_scan" -Arguments $externalArgs

    if ($PaperEveryCycles -gt 0 -and (($cycle % $PaperEveryCycles) -eq 0)) {
        $paperArgs = @(
            "research\market_making_paper_simulator.py",
            "--history-csv", "data\market_making\candidate_history.csv",
            "--output-dir", "data\market_making_paper_live",
            "--max-events", "$MaxPaperEvents"
        )
        $exitCodes["market_making_paper"] = Invoke-Step -Name "market_making_paper" -Arguments $paperArgs
    }

    $evalArgs = @(
        "research\reward_fair_value_evaluator.py",
        "--maker-history-csv", "data\market_making\candidate_history.csv",
        "--external-history-csv", "data\external_fair_value\candidate_history.csv",
        "--maker-paper-events-csv", "data\market_making_paper_live\maker_paper_events.csv",
        "--output-dir", "data\reward_fair_value_evaluator_live",
        "--max-snapshot-age-hours", "$MaxSnapshotAgeHours"
    )
    $exitCodes["reward_fair_value_evaluator"] = Invoke-Step -Name "reward_fair_value_evaluator" -Arguments $evalArgs

    $shadowPaperArgs = @(
        "research\shadow_paper_trader.py",
        "--shadow-actions-csv", "data\reward_fair_value_evaluator_live\shadow_actions.csv",
        "--maker-history-csv", "data\market_making\candidate_history.csv",
        "--output-dir", "$ShadowActiveOutputDir"
    )
    $exitCodes["shadow_paper_trader_active"] = Invoke-Step -Name "shadow_paper_trader_active" -Arguments $shadowPaperArgs

    $shadowHoldArgs = @(
        "research\shadow_paper_trader.py",
        "--shadow-actions-csv", "data\reward_fair_value_evaluator_live\shadow_actions.csv",
        "--maker-history-csv", "data\market_making\candidate_history.csv",
        "--output-dir", "$ShadowHoldOutputDir",
        "--take-profit-per-share", "999",
        "--stop-loss-per-share", "999",
        "--max-position-cycles", "1000000",
        "--max-position-without-update-hours", "999999",
        "--disable-stale-position-close"
    )
    $exitCodes["shadow_paper_trader_hold"] = Invoke-Step -Name "shadow_paper_trader_hold" -Arguments $shadowHoldArgs

    $shadowTakerActiveArgs = @(
        "research\shadow_paper_trader.py",
        "--shadow-actions-csv", "data\reward_fair_value_evaluator_live\shadow_actions.csv",
        "--maker-history-csv", "data\market_making\candidate_history.csv",
        "--output-dir", "$ShadowTakerActiveOutputDir",
        "--directional-entry-mode", "taker"
    )
    $exitCodes["shadow_paper_trader_taker_active"] = Invoke-Step -Name "shadow_paper_trader_taker_active" -Arguments $shadowTakerActiveArgs

    $shadowTakerHoldArgs = @(
        "research\shadow_paper_trader.py",
        "--shadow-actions-csv", "data\reward_fair_value_evaluator_live\shadow_actions.csv",
        "--maker-history-csv", "data\market_making\candidate_history.csv",
        "--output-dir", "$ShadowTakerHoldOutputDir",
        "--directional-entry-mode", "taker",
        "--take-profit-per-share", "999",
        "--stop-loss-per-share", "999",
        "--max-position-cycles", "1000000",
        "--max-position-without-update-hours", "999999",
        "--disable-stale-position-close"
    )
    $exitCodes["shadow_paper_trader_taker_hold"] = Invoke-Step -Name "shadow_paper_trader_taker_hold" -Arguments $shadowTakerHoldArgs

    if ($AnalysisEveryCycles -gt 0 -and (($cycle % $AnalysisEveryCycles) -eq 0)) {
        $analysisArgs = @(
            "research\analyze_shadow_actions.py",
            "--history-csv", "data\reward_fair_value_evaluator_live\shadow_action_history.csv",
            "--output-dir", "data\shadow_action_analysis_live"
        )
        $exitCodes["shadow_action_analysis"] = Invoke-Step -Name "shadow_action_analysis" -Arguments $analysisArgs
    }

    $cycleEnd = Get-Date
    $elapsedSeconds = [int][math]::Ceiling(($cycleEnd - $cycleStart).TotalSeconds)
    $sleepSeconds = [math]::Max($CycleSeconds - $elapsedSeconds, 5)
    Write-Heartbeat -Cycle $cycle -CycleStart $cycleStart -CycleEnd $cycleEnd -ExitCodes $exitCodes -SleepSeconds $sleepSeconds
    Write-SupervisorLog "cycle_done cycle=$cycle elapsed_seconds=$elapsedSeconds sleep_seconds=$sleepSeconds"

    if ($StopAfterCycles -gt 0 -and $cycle -ge $StopAfterCycles) {
        Write-SupervisorLog "stop_after_cycles reached; exiting."
        break
    }
    Start-Sleep -Seconds $sleepSeconds
}
