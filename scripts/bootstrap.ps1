<#
.SYNOPSIS
    One-shot installer for invest-mcp on Windows.

.DESCRIPTION
    Does everything after "install the InVEST Workbench + get this repo":

        1. picks where the server lives:
             - a conda/micromamba env `invest-mcp`  (default when no system
               Python >= 3.10 is on PATH, or with -CondaServer), OR
             - a plain .venv from your system Python (with -Venv)
        2. installs invest-mcp into it (editable)
        3. runs `invest-mcp setup`  -> builds the invest-geo / invest-cal conda envs
        4. runs `invest-mcp doctor` -> checks the result
        5. prints the client-registration snippets

    You do NOT need to install Python yourself: the InVEST Workbench bundles a
    `micromamba.exe`, and this script uses it for all three envs.

    Safe to re-run (idempotent): an existing env is reused.

.PARAMETER CondaServer
    Force the server into a conda/micromamba env (no system Python touched).

.PARAMETER Venv
    Force the server into a .venv from a system Python >= 3.10 (fails if none).

.PARAMETER SkipEnvs
    Skip step 3. Model browse / validate / run still work; preflight_geo and
    run_calibration do not.

.PARAMETER GeoOnly
    In step 3 build only the invest-geo env.

.PARAMETER CalOnly
    In step 3 build only the invest-cal env.

.PARAMETER Conda
    Full path to a conda / mamba / micromamba executable, if auto-detection
    fails. Never a condabin\*.bat shim.

.PARAMETER Http
    Also install the [http] extra (uvicorn) for `serve --transport streamable-http`.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1 -CondaServer -SkipEnvs
#>
[CmdletBinding()]
param(
    [switch] $CondaServer,
    [switch] $Venv,
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

if (-not (Test-Path (Join-Path $RepoRoot "environment-geo.yml"))) {
    Die "environment-geo.yml not found. Run this from a git clone / ZIP of the repo."
}
if ($CondaServer -and $Venv) { Die "-CondaServer and -Venv are mutually exclusive." }

# --------------------------------------------------------------------------
# discovery helpers
# --------------------------------------------------------------------------
function Find-Python {
    # a system Python >= 3.10, as an argv array (e.g. @("py","-3") or @("C:\...\python.exe"))
    $cands = @()
    if (Get-Command "py" -ErrorAction SilentlyContinue) { $cands += ,@("py", "-3") }
    foreach ($n in @("python", "python3")) {
        $c = Get-Command $n -ErrorAction SilentlyContinue
        if ($c) { $cands += ,@($c.Source) }
    }
    foreach ($c in $cands) {
        try {
            $exe = $c[0]; $pre = @($c[1..($c.Length - 1)])
            $v = & $exe @pre "-c" "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -eq 0 -and $v) {
                $p = $v.Trim().Split(".")
                if ([int]$p[0] -eq 3 -and [int]$p[1] -ge 10) { return ,@($exe) + $pre }
            }
        } catch { }
    }
    return $null
}

function Find-Conda {
    # a REAL conda/mamba/micromamba executable -- never a condabin\*.bat shim.
    if ($Conda) {
        if ((Test-Path $Conda) -and ($Conda -notmatch '\.(bat|cmd)$')) { return $Conda }
        Die "-Conda '$Conda' is not a usable executable."
    }
    foreach ($v in @($env:MAMBA_EXE, $env:CONDA_EXE)) {
        if ($v -and (Test-Path $v) -and ($v -notmatch '\.(bat|cmd)$')) { return $v }
    }
    $mm = Get-Command "micromamba" -ErrorAction SilentlyContinue
    if ($mm) { return $mm.Source }
    foreach ($base in @("$env:ProgramFiles", "${env:ProgramFiles(x86)}")) {
        if (-not $base) { continue }
        $hit = Get-ChildItem -Path $base -Filter "micromamba.exe" -Recurse -ErrorAction SilentlyContinue `
               | Where-Object { $_.FullName -like "*InVEST*Workbench*" } | Select-Object -First 1
        if ($hit) { return $hit.FullName }
    }
    foreach ($root in @("$env:USERPROFILE\miniforge3", "$env:USERPROFILE\mambaforge",
                        "$env:USERPROFILE\miniconda3", "$env:USERPROFILE\anaconda3",
                        "C:\ProgramData\miniforge3", "C:\ProgramData\miniconda3")) {
        foreach ($rel in @("Scripts\mamba.exe", "Scripts\conda.exe", "condabin\micromamba.exe")) {
            $c = Join-Path $root $rel
            if (Test-Path $c) { return $c }
        }
    }
    foreach ($n in @("mamba", "conda")) {
        $c = Get-Command $n -ErrorAction SilentlyContinue
        if ($c -and ($c.Source -notmatch '\.(bat|cmd)$')) { return $c.Source }
    }
    return $null
}

function Conda-CreateArgs ($conda, $yml) {
    if ((Split-Path $conda -Leaf) -match "micromamba") { return @("create", "-y", "-f", $yml) }
    return @("env", "create", "-y", "-f", $yml)
}

function Conda-EnvPython ($conda, $name) {
    try {
        $p = & $conda "run" "-n" $name "python" "-c" "import sys;print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $p -and (Test-Path $p.Trim())) { return $p.Trim() }
    } catch { }
    return $null
}

# --------------------------------------------------------------------------
# 1. decide + build where the server lives  -> $Py
# --------------------------------------------------------------------------
$Py = $null

$useConda = $CondaServer
if (-not $CondaServer -and -not $Venv) {
    if (Find-Python) {
        Say "Found a system Python >= 3.10 -> using a .venv. (Pass -CondaServer to use micromamba instead.)"
    } else {
        Say "No system Python >= 3.10 -> putting the server in a conda env instead. (No Python install needed.)"
        $useConda = $true
    }
}

if ($useConda) {
    $conda = Find-Conda
    if (-not $conda) {
        Die "No conda/mamba/micromamba found. Install the InVEST Workbench (it bundles micromamba) or Miniforge (https://github.com/conda-forge/miniforge), or pass -Conda <path>."
    }
    Ok "conda tool: $conda"
    $serverYml = Join-Path $RepoRoot "environment-server.yml"
    Say "Creating the 'invest-mcp' env ..."
    & $conda @(Conda-CreateArgs $conda $serverYml)
    $Py = Conda-EnvPython $conda "invest-mcp"
    if (-not $Py) { Die "'invest-mcp' env not found after create -- see the log above." }
    Ok "server python: $Py"
    # so `invest-mcp setup` reuses the same tool for invest-geo / invest-cal
    if (-not $Conda) { $Conda = $conda }
} else {
    if (Test-Path $VenvPy) {
        Ok ".venv already exists"
    } else {
        Say "Looking for Python >= 3.10 ..."
        $py = Find-Python
        if (-not $py) {
            Die "No Python >= 3.10 on PATH. Either install it (https://www.python.org/downloads/windows/, tick 'Add to PATH', new terminal) or re-run with -CondaServer."
        }
        Ok "using $($py -join ' ')"
        Say "Creating .venv ..."
        & $py[0] @($py[1..($py.Length - 1)]) "-m" "venv" $VenvDir
        if (-not (Test-Path $VenvPy)) { Die "venv creation failed" }
        Ok ".venv created"
    }
    $Py = $VenvPy
}

# --------------------------------------------------------------------------
# 2. install invest-mcp
# --------------------------------------------------------------------------
Say "Installing invest-mcp ..."
& $Py "-m" "pip" "install" "--upgrade" "pip" "--quiet"
$target = if ($Http) { "$RepoRoot[http]" } else { $RepoRoot }
& $Py "-m" "pip" "install" "-e" $target
if ($LASTEXITCODE -ne 0) { Die "pip install failed" }
Ok "invest-mcp installed"

# --------------------------------------------------------------------------
# 3. conda envs (invest-geo / invest-cal)
# --------------------------------------------------------------------------
if ($SkipEnvs) {
    Warn "skipping invest-geo / invest-cal (-SkipEnvs): preflight_geo + run_calibration disabled"
} else {
    Say "Building invest-geo / invest-cal. Slow part -- 10-25 min, mostly downloads."
    $setupArgs = @("-m", "invest_mcp", "setup")
    if ($GeoOnly) { $setupArgs += "--geo" }
    if ($CalOnly) { $setupArgs += "--cal" }
    if ($Conda)   { $setupArgs += @("--conda", $Conda) }
    & $Py @setupArgs
    if ($LASTEXITCODE -ne 0) {
        Warn "`invest-mcp setup` reported errors. Model browse/validate/run still work."
        Warn "See docs\INSTALACION-PASO-A-PASO.md section 10, or re-run:  <server-python> -m invest_mcp setup"
    }
}

# --------------------------------------------------------------------------
# 4. doctor
# --------------------------------------------------------------------------
Say "Checking the install ..."
& $Py "-m" "invest_mcp" "doctor"
$doctorRc = $LASTEXITCODE

# --------------------------------------------------------------------------
# 5. client registration
# --------------------------------------------------------------------------
Say "Register the server with your client:"
Write-Host ""
Write-Host "  Claude Code  -- run this once:" -ForegroundColor White
& $Py "-m" "invest_mcp" "mcp-config" "--client" "claude-code"
Write-Host ""
Write-Host "  Claude Desktop / other  -- put this in the client's MCP config:" -ForegroundColor White
& $Py "-m" "invest_mcp" "mcp-config"
Write-Host ""

if ($doctorRc -eq 0) {
    Ok "Done. `doctor` is green."
} else {
    Warn "Done, but `doctor` flagged something. invest itself must be [ok]; invest-geo/-cal [warn] just means those features are off."
}
Write-Host "Full walk-through: docs\INSTALACION-PASO-A-PASO.md" -ForegroundColor Cyan
