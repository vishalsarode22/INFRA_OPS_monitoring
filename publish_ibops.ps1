# InfraBeatOps -- make a clean copy of the project, zip it locally, and push it to GitHub.
#
#   cd "$env:USERPROFILE\ibops\infrabeatops"
#   powershell -ExecutionPolicy Bypass -File publish_ibops.ps1 -RepoUrl https://github.com/<you>/infrabeatops.git
#
# Without -RepoUrl it only builds the clean copy and the zip (no push).
#
# Never copied: .env, real config files with client hosts/tokens (systems.yaml,
# remote_sources.yaml, delivery accounts), tests, logs, reports, screenshots,
# recordings, runtime state (snapshots, history, SQLite DB), .bak files,
# throwaway check_*.py scripts and patch files. The original project folder is
# not changed in any way.
param(
    [string]$RepoUrl = "",
    [string]$Source = (Get-Location).Path,
    [string]$OutDir = (Join-Path $env:USERPROFILE "Desktop")
)
$ErrorActionPreference = "Stop"
$stamp = Get-Date -Format "yyyyMMdd-HHmm"
$clean = Join-Path $OutDir "infrabeatops-clean"
$zip   = Join-Path $OutDir "infrabeatops-clean-$stamp.zip"

if (-not (Test-Path (Join-Path $Source "dashboard\app.py"))) {
    throw "Run this from the project folder (the one with main.py and dashboard\). Current: $Source"
}

# ---------------------------------------------------------------- what to leave out
$skipDirs = @(".venv", "venv", ".git", "tests", "logs", "reports", "recordings", "screenshots",
              "__pycache__", ".pytest_cache", ".mypy_cache", "node_modules",
              "dashboard\snapshots", "dashboard\events", "dashboard\incidents", "dashboard\history",
              "dashboard\intelligence_state", "dashboard\backups", ".github")
$skipFiles = @(".env", "dev_rfc.log",
               "config\systems.yaml", "config\remote_sources.yaml", "config\delivery_accounts.json",
               "config\delivery_settings.json", "config\profile_state.json", "config\schedules.json",
               "dashboard\fm_registry.json", "dashboard\latest_status.json", "dashboard\run_history.json",
               "dashboard\sap_gui_pids.json", "dashboard\reach_state.json", "dashboard\parser_health.json",
               "dashboard\missing_fms.json", "MERGE_NOTES.md", "publish_ibops.ps1")
$skipPatterns = @("*.bak", "*.bak?", "*.bak-*", "*.log", "*.patch", "*.sqlite", "*.sqlite-*", "*.db",
                  "*.lock", "*.tmp", "*.pyc", "check_*.py", "watch_stat.py", "*.zip")
$imageExt = @(".png", ".jpg", ".jpeg", ".bmp", ".gif")
# Images are kept only where the app needs them (logos, UI); elsewhere they are screenshots.
$imageKeep = @("dashboard\static", "packaging", "docs")

function Skip-Path([string]$rel, [bool]$isDir) {
    foreach ($d in $skipDirs) {
        if ($rel -eq $d -or $rel.StartsWith("$d\") -or ($rel -split "\\") -contains $d) { return $true }
    }
    if ($isDir) { return $false }
    if ($skipFiles -contains $rel) { return $true }
    $name = Split-Path $rel -Leaf
    foreach ($p in $skipPatterns) { if ($name -like $p) { return $true } }
    if ($rel -like "config\templates\*_files\*") { return $true }      # Excel "save as web page" debris
    $ext = [IO.Path]::GetExtension($name).ToLower()
    if ($imageExt -contains $ext) {
        foreach ($k in $imageKeep) { if ($rel.StartsWith("$k\")) { return $false } }
        return $true
    }
    return $false
}

# ---------------------------------------------------------------- 1. clean copy
Write-Host "== 1. clean copy -> $clean" -ForegroundColor Cyan
if (Test-Path $clean) { Remove-Item -Recurse -Force $clean }
New-Item -ItemType Directory -Path $clean | Out-Null
$srcFull = (Resolve-Path $Source).Path.TrimEnd("\")
$copied = 0; $skipped = 0
Get-ChildItem -LiteralPath $srcFull -Recurse -Force -File -ErrorAction SilentlyContinue | ForEach-Object {
    $rel = $_.FullName.Substring($srcFull.Length + 1)
    $dirRel = Split-Path $rel -Parent
    if (($dirRel -and (Skip-Path $dirRel $true)) -or (Skip-Path $rel $false)) { $skipped++; return }
    $dest = Join-Path $clean $rel
    New-Item -ItemType Directory -Force -Path (Split-Path $dest -Parent) | Out-Null
    Copy-Item -LiteralPath $_.FullName -Destination $dest
    $copied++
}
Write-Host "   copied $copied files, left out $skipped"

# Example configs so a fresh checkout knows what to fill in.
foreach ($ex in @("config\systems.yaml.example", "config\remote_sources.yaml.example", ".env.example")) {
    if (-not (Test-Path (Join-Path $clean $ex))) { Write-Host "   note: $ex not present" -ForegroundColor Yellow }
}

@"
# --- secrets and machine-specific config: never commit ---
.env
config/systems.yaml
config/remote_sources.yaml
config/delivery_accounts.json
config/delivery_settings.json
config/profile_state.json
config/schedules.json
# --- runtime state, output, logs ---
.venv/
venv/
__pycache__/
*.pyc
.pytest_cache/
logs/
reports/
recordings/
screenshots/
dashboard/snapshots/
dashboard/events/
dashboard/incidents/
dashboard/history/
dashboard/intelligence_state/
dashboard/backups/
dashboard/*.json
dashboard/*.lock
*.sqlite
*.sqlite-*
*.log
*.bak*
*.patch
*.zip
tests/
"@ | Set-Content -Encoding UTF8 (Join-Path $clean ".gitignore")

# ---------------------------------------------------------------- 2. secret scan
Write-Host "== 2. secret scan" -ForegroundColor Cyan
# Two kinds of check:
#  - code files: only a name like password/token/secret/api_key assigned a
#    QUOTED literal of 6+ characters (password set to a quoted word). Code that
#    merely handles a password (password=cfg["password"]) is not flagged.
#  - config files: a key like password/token/... with a written-out value
#    that is not a ${VARIABLE} reference.
#  - plus well-known key formats anywhere (Gemini, xAI, GitHub, private keys).
$codeExt = @(".py", ".js", ".html", ".ps1", ".sh", ".cmd", ".bat", ".css")
$confExt = @(".yaml", ".yml", ".json", ".ini", ".cfg", ".toml", ".conf", ".txt", ".example", ".service", ".md", "")
$names = '\w*(password|passwd|pwd|secret|api_?key|token)\w*'
$codeRule = '(?i)\b' + $names + '\s*[:=]\s*[rbuf]?["'']([^"''\s$%{}<>]{6,})["'']'
$confRule = '(?i)^\s*-?\s*["'']?' + $names + '["'']?\s*[:=]\s*["'']?(?!\$\{)(?!<)([^\s"''#,]{6,})'
$keyRules = @(
    @{ n = "Google/Gemini API key"; r = 'AIza[0-9A-Za-z_\-]{30,}' },
    @{ n = "xAI/Grok API key";      r = 'xai-[0-9A-Za-z]{20,}' },
    @{ n = "private key";           r = '-----BEGIN [A-Z ]*PRIVATE KEY-----' },
    @{ n = "GitHub token";          r = 'gh[pousr]_[0-9A-Za-z]{30,}' }
)
# Values that are clearly not secrets (placeholders, words, env names).
$notSecret = '(?i)^(none|null|true|false|changeme|change_me|example|placeholder|password|secret|token|required|optional|\*+|x+|your\w*|<.*>)$'
# An environment-variable NAME used as a value (case-sensitive: SMTP_PASSWORD).
$envName = '^[A-Z][A-Z0-9]*_[A-Z0-9_]+$'
$findings = @()
Get-ChildItem -LiteralPath $clean -Recurse -File | ForEach-Object {
    $ext = $_.Extension.ToLower()
    $isCode = $codeExt -contains $ext
    $isConf = $confExt -contains $ext
    if (-not ($isCode -or $isConf)) { return }
    $rel = $_.FullName.Substring($clean.Length + 1)
    $n = 0
    foreach ($line in [IO.File]::ReadLines($_.FullName)) {
        $n++
        if ($line.Length -gt 2000) { continue }
        $hit = $null
        foreach ($k in $keyRules) { if ($line -match $k.r) { $hit = $k.n; break } }
        if (-not $hit) {
            $rule = if ($isCode) { $codeRule } else { $confRule }
            if ($line -match $rule) {
                $val = $Matches[$Matches.Count - 1]
                if (($val -notmatch $notSecret) -and ($val -cnotmatch $envName)) { $hit = "password/token with a written-out value" }
            }
        }
        if ($hit) {
            $t = $line.Trim()
            $findings += "$rel`:$n  [$hit]  $($t.Substring(0, [Math]::Min(100, $t.Length)))"
        }
    }
}
if ($findings.Count) {
    Write-Host "   Possible secrets found -- NOT zipping or pushing. Check these lines:" -ForegroundColor Red
    $findings | Select-Object -First 40 | ForEach-Object { Write-Host "   $_" }
    Write-Host "   If a line is a false alarm, tell Claude which one; if it is real, remove it from the source and run again."
    exit 2
}
Write-Host "   no secrets found"

# ---------------------------------------------------------------- 3. local zip
Write-Host "== 3. zip -> $zip" -ForegroundColor Cyan
if (Test-Path $zip) { Remove-Item -Force $zip }
Add-Type -AssemblyName System.IO.Compression.FileSystem
# CreateFromDirectory includes dot-files such as .gitignore (Compress-Archive may not).
[IO.Compression.ZipFile]::CreateFromDirectory($clean, $zip, [IO.Compression.CompressionLevel]::Optimal, $true)
Write-Host ("   {0:N1} MB" -f ((Get-Item $zip).Length / 1MB))

# ---------------------------------------------------------------- 4. GitHub
if (-not $RepoUrl) {
    Write-Host "== 4. no -RepoUrl given: not pushing. Clean copy: $clean" -ForegroundColor Yellow
    exit 0
}
Write-Host "== 4. push -> $RepoUrl" -ForegroundColor Cyan
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw "git is not installed on this machine" }
Push-Location $clean
try {
    git init -q -b main
    git add -A
    $msg = @"
InfraBeatOps: clean snapshot $stamp

SAP Basis monitoring platform (dashboard, RFC/SAP GUI collectors, alerts,
VPN-aware reachability, services). Tests, logs, reports, screenshots,
runtime state and machine-specific config are excluded.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01X7bJnrzbQADx87m6JBziys
"@
    $msg | git commit -q -F -
    git remote add origin $RepoUrl
    git push -u origin main
    if ($LASTEXITCODE -ne 0) { throw "git push failed (see above). Is the repository empty and do you have access?" }
    Write-Host "   pushed." -ForegroundColor Green
} finally { Pop-Location }
