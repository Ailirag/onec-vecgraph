<#
.SYNOPSIS
  onec-vecgraph session preflight (Windows/PowerShell): set up the environment and verify readiness.

.DESCRIPTION
  Eliminates the common START-OF-SESSION errors before index/callgraph/vectorize/ingest:
    - uv not on PATH (fresh shell)      -> discover it in PATH / user-local bin
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

.PARAMETER UvDir
  Optional machine-local directory containing uv.exe when it is not on PATH.

.PARAMETER HfHome
  Optional machine-local HuggingFace cache directory. Empty keeps the library default.
#>
[CmdletBinding()]
param(
    [switch]$StartNeo4j,
    [switch]$SkipSync,
    [string]$UvDir = "",
    [string]$HfHome = ""
)

$issues = @()
function Test-Uv { [bool](Get-Command uv -ErrorAction SilentlyContinue) }

# 1) uv on PATH
if (Test-Uv) {
    Write-Host '[ok]  uv on PATH'
} else {
    $uvCandidates = @()
    if ($UvDir) { $uvCandidates += $UvDir }
    $uvCandidates += (Join-Path ([string]$env:USERPROFILE) '.local\bin')
    $foundUvDir = @($uvCandidates | Where-Object {
        $_ -and (Test-Path -LiteralPath (Join-Path $_ 'uv.exe') -PathType Leaf)
    } | Select-Object -First 1)
    if ($foundUvDir.Count -gt 0) {
        $env:Path = "$($foundUvDir[0]);$env:Path"
        Write-Host "[fix] uv prepended to PATH from $($foundUvDir[0])"
    } else {
        $issues += 'uv not found -- install it (`winget install astral-sh.uv`) or pass -UvDir.'
        Write-Host '[!!]  uv not found'
    }
}

# 2) UTF-8 console (Cyrillic output; data in Neo4j/JSON is correct regardless)
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$OutputEncoding = [Text.Encoding]::UTF8
$env:PYTHONUTF8 = '1'
Write-Host '[ok]  console UTF-8 + PYTHONUTF8=1'

# 3) HF cache for embedding models. Empty means the library's user-profile default.
if ($HfHome) { $env:HF_HOME = [System.IO.Path]::GetFullPath($HfHome) }
if ($env:HF_HOME) {
    Write-Host "[ok]  HF_HOME=$env:HF_HOME"
} else {
    Write-Host '[ok]  HF_HOME uses the library default (set env or pass -HfHome to override)'
}

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
