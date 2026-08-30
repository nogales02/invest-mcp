<#
.SYNOPSIS
    One-shot installer for invest-mcp on Windows.

.DESCRIPTION
    Does everything after "install Python + the InVEST Workbench + get this repo":

        1. finds a Python >= 3.10
        2. creates the .venv and installs invest-mcp into it (editable)
        3. runs `invest-mcp setup`  -> builds the invest-geo / invest-cal conda envs
        4. runs `invest-mcp doctor` -> checks the result
        5. prints the client-registration snippets

    Safe to re-run (idempotent): an existing .venv / conda env is reused.

.PARAMETER SkipEnvs
    Skip step 3. You get model browse / validate / run, but not preflight_geo
    or run_calibration.

.PARAMETER GeoOnly
    In step 3 build only the invest-geo env (geospatial preflight + summaries).

.PARAMETER CalOnly
    In step 3 build only the invest-cal env (model calibration).

.PARAMETER Conda
    Full path to a conda / mamba / micromamba executable, if auto-detection
    fails. Never point this at a condabin\*.bat shim.

.PARAMETER Http
    Also install the [http] extra (uvicorn) for `serve --transport streamable-http`.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1 -SkipEnvs
#>
[CmdletBinding()]
param(
    [switch] $SkipEnvs,
    [switch] $GeoOnly,
    [switch] $CalOnly,
    [string] $Conda,
    [switch] $Http
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$VenvDir  = Join-Path $RepoRoot ".venv"
$VenvPy   = Join-Path $VenvDir "Scripts\python.exe"

function Say  ($m) { Write-Host "==> $m" -ForegroundColor Cyan }
function Ok   ($m) { Write-Host "    [ok] $m" -ForegroundColor Green }
function Warn ($m) { Write-Host "    [!!] $m" -ForegroundColor Yellow }
function Die  ($m) { Write-Host "    [XX] $m" -ForegroundColor Red; exit 1 }

Say "invest-mcp bootstrap  (repo: $RepoRoot)"

# --- 0. this must be a real checkout, not a pip-from-git install --------------
if (-not (Test-Path (Join-Path $RepoRoot "environment-geo.yml"))) {
    Die "environment-geo.yml not found next to this script's parent folder. Run this from a `git clone` of the repo."
}

# --- 1. find a Python >= 3.10 -----------------------------------------------
function Find-Python {
    $cands = @()
    $pyLauncher = Get-Command "py" -ErrorAction SilentlyContinue
    if ($pyLauncher) { $cands += ,@("py", "-3") }
    foreach ($n in @("python", "python3")) {
        $c = Get-Command $n -ErrorAction SilentlyContinue
        if ($c) { $cands += ,@($c.Source) }
    }
    foreach ($c in $cands) {
        try {
            $exe  = $c[0]; $pre = @($c[1..($c.Length-1)])
            $vraw = & $exe @pre "-c" "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -eq 0 -and $vraw) {
                $parts = $vraw.Trim().Split(".")
                if ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 10) {
                    return ,@($exe) + $pre
                }
            }
        } catch { }
    }
    return $null
}

if (Test-Path $VenvPy) {
    Ok ".venv already exists"
} else {
    Say "Looking for Python >= 3.10 ..."
    $py = Find-Python
    if (-not $py) {
        Die "No Python >= 3.10 on PATH. Install it from https://www.python.org/downloads/windows/ (tick 'Add python.exe to PATH'), open a NEW terminal, retry."
    }
    Ok "using $($py -join ' ')"
    Say "Creating .venv ..."
    & $py[0] @($py[1..($py.Length-1)]) "-m" "venv" $VenvDir
    if (-not (Test-Path $VenvPy)) { Die "venv creation failed" }
    Ok ".venv created"
}

# --- 2. install invest-mcp into the venv ----------------------------------
Say "Installing invest-mcp into .venv ..."
& $VenvPy "-m" "pip" "install" "--upgrade" "pip" "--quiet"
$target = if ($Http) { "$RepoRoot[http]" } else { $RepoRoot }
& $VenvPy "-m" "pip" "install" "-e" $target
if ($LASTEXITCODE -ne 0) { Die "pip install failed" }
Ok "invest-mcp installed"

# --- 3. conda envs -------------------------------------------------------
if ($SkipEnvs) {
    Warn "skipping conda envs (-SkipEnvs): preflight_geo + run_calibration will be disabled"
} else {
    Say "Building conda envs (invest-geo / invest-cal). This is the slow part -- 10-25 min, mostly downloads."
    $setupArgs = @("-m", "invest_mcp", "setup")
    if ($GeoOnly) { $setupArgs += "--geo" }
    if ($CalOnly) { $setupArgs += "--cal" }
    if ($Conda)   { $setupArgs += @("--conda", $Conda) }
    & $VenvPy @setupArgs
    if ($LASTEXITCODE -ne 0) {
        Warn "`invest-mcp setup` reported errors. The server still works for model browse/validate/run."
        Warn "See docs\INSTALACION-PASO-A-PASO.md section 10, or re-run:  .venv\Scripts\python -m invest_mcp setup"
    }
}

# --- 4. doctor ---------------------------------------------------------
Say "Checking the install ..."
& $VenvPy "-m" "invest_mcp" "doctor"
$doctorRc = $LASTEXITCODE

# --- 5. client registration ------------------------------------------
Say "Register the server with your client:"
Write-Host ""
Write-Host "  Claude Code  -- run this once:" -ForegroundColor White
& $VenvPy "-m" "invest_mcp" "mcp-config" "--client" "claude-code"
Write-Host ""
Write-Host "  Claude Desktop / other  -- put this in the client's MCP config:" -ForegroundColor White
& $VenvPy "-m" "invest_mcp" "mcp-config"
Write-Host ""

if ($doctorRc -eq 0) {
    Ok "Done. `doctor` is green."
} else {
    Warn "Done, but `doctor` flagged something above. invest itself must be [ok]; invest-geo/-cal [warn] just means those features are off."
}
Write-Host "Full walk-through: docs\INSTALACION-PASO-A-PASO.md" -ForegroundColor Cyan
