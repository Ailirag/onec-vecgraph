<#
.SYNOPSIS
  onec-vecgraph session preflight (Windows/PowerShell): set up the environment and verify readiness.

.DESCRIPTION
  Eliminates the common START-OF-SESSION errors before index/callgraph/vectorize/ingest:
    - uv not on PATH (fresh shell)      -> prepend D:\tools\uv
    - console codepage (Cyrillic mojibake) -> UTF-8 + PYTHONUTF8
    - HF model cache                    -> HF_HOME
    - empty .venv in a git worktree     -> uv sync --frozen
    - Neo4j not reachable               -> hint / optionally start it
  Non-destructive: it collects issues and prints a final verdict (OK / what to fix).

  NOTE: this script is intentionally ASCII-only. Windows PowerShell 5.1 reads .ps1 files
  in the system ANSI codepage (cp1251) unless they carry a UTF-8 BOM, so Cyrillic source
  would fail to parse. Russian docs live in docs/SESSION_BOOTSTRAP.md (read as UTF-8).

  Run in an INTERACTIVE shell so the env vars persist in the current window:
      . .\scripts\preflight.ps1            # dot-space = dot-source
      . .\scripts\preflight.ps1 -StartNeo4j

  INSIDE an agent's tool calls the environment does NOT persist between calls -- there,
  prepend the one-line prefix from docs/SESSION_BOOTSTRAP.md to EVERY command.

.PARAMETER StartNeo4j
  Bring up Neo4j (docker compose up -d neo4j) before the health check.

.PARAMETER SkipSync
  Skip uv sync (when you are sure .venv is already populated).
#>
[CmdletBinding()]
param(
    [switch]$StartNeo4j,
    [switch]$SkipSync
)

# Paths on this machine (Windows, everything on drive D). Adjust here if the env moves.
$UvDir  = 'D:\tools\uv'
$HfHome = 'D:\tools\hf-cache'

$issues = @()
function Test-Uv { [bool](Get-Command uv -ErrorAction SilentlyContinue) }

# 1) uv on PATH
if (Test-Uv) {
    Write-Host '[ok]  uv on PATH'
} elseif (Test-Path (Join-Path $UvDir 'uv.exe')) {
    $env:Path = "$UvDir;$env:Path"
    Write-Host "[fix] uv prepended to PATH from $UvDir"
} else {
    $issues += "uv not found on PATH or in $UvDir -- install uv or edit `$UvDir in this script."
    Write-Host '[!!]  uv not found'
}

# 2) UTF-8 console (Cyrillic output; data in Neo4j/JSON is correct regardless)
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$OutputEncoding = [Text.Encoding]::UTF8
$env:PYTHONUTF8 = '1'
Write-Host '[ok]  console UTF-8 + PYTHONUTF8=1'

# 3) HF cache for embedding models
if (-not $env:HF_HOME) { $env:HF_HOME = $HfHome }
Write-Host "[ok]  HF_HOME=$env:HF_HOME"

# 4) .venv populated (a fresh git worktree has an empty .venv -> 'program not found')
if (-not $SkipSync -and (Test-Uv)) {
    uv run --no-sync onec-vecgraph version *> $null
    if ($LASTEXITCODE -eq 0) {
        Write-Host '[ok]  .venv ready (package importable)'
    } else {
        Write-Host '[fix] .venv not ready -> uv sync --frozen (from lock, offline)...'
        uv sync --frozen
        if ($LASTEXITCODE -ne 0) {
            $issues += 'uv sync --frozen failed (stale lock?) -- try `uv sync` (needs network); see docs/SESSION_BOOTSTRAP.md.'
            Write-Host '[!!]  uv sync --frozen did not pass'
        }
    }
}

# 5) Neo4j: optionally start + health check
if ($StartNeo4j) {
    Write-Host '[..]  docker compose up -d neo4j'
    docker compose up -d neo4j
}
if (Test-Uv) {
    uv run --no-sync onec-vecgraph health *> $null
    if ($LASTEXITCODE -eq 0) {
        Write-Host '[ok]  Neo4j health'
    } else {
        $issues += 'onec-vecgraph health failed -- Neo4j down? run `docker compose up -d neo4j` (or pass -StartNeo4j).'
        Write-Host '[!!]  Neo4j not reachable'
    }
}

Write-Host ''
if ($issues.Count -eq 0) {
    Write-Host '=== Preflight OK. Environment ready for index / callgraph / vectorize / ingest. ==='
} else {
    Write-Host '=== Preflight: attention needed ==='
    $issues | ForEach-Object { Write-Host "  - $_" }
    Write-Host 'Full symptom -> fix table: docs/SESSION_BOOTSTRAP.md'
}
