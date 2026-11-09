# Sets up a two-person Guild demo on one Windows machine: Alice and Bob each get their own
# Hermes home, sharing one guild repository on disk. Re-run any time to reset.
#
#   powershell -ExecutionPolicy Bypass -File demo\setup-demo.ps1 [-Hermes <path to hermes.exe>]
#
# Then, in two terminals:
#   $env:HERMES_HOME="C:\gd\alice"; hermes      # Alice
#   $env:HERMES_HOME="C:\gd\bob";   hermes      # Bob
# (Short paths on purpose: Windows' 260-char path limit bites deep Hermes homes.)
param(
  [string]$Hermes = "hermes",
  [string]$Root = "C:\gd",
  [string]$ModelConfigFrom = ""   # optional: a config.yaml whose model/provider settings both members reuse
)
$ErrorActionPreference = "Stop"
$plugin = Split-Path -Parent $PSScriptRoot
if (Test-Path $Root) { Remove-Item -Recurse -Force $Root }
New-Item -ItemType Directory -Force "$Root" | Out-Null

foreach ($m in @("alice", "bob")) {
  $h = Join-Path $Root $m
  New-Item -ItemType Directory -Force "$h\plugins\guild", "$h\skills" | Out-Null
  Copy-Item "$plugin\*.py", "$plugin\plugin.yaml" "$h\plugins\guild\"
  New-Item -ItemType Directory -Force "$h\plugins\guild\desktop" | Out-Null
  Copy-Item "$plugin\desktop\plugin.js" "$h\plugins\guild\desktop\"
  $cfg = "plugins:`n  enabled:`n    - guild`n"
  if ($ModelConfigFrom -and (Test-Path $ModelConfigFrom)) {
    $cfg = (Get-Content -Raw $ModelConfigFrom) + "`n" + $cfg
  }
  [System.IO.File]::WriteAllText("$h\config.yaml", $cfg)
}

$repo = Join-Path $Root "acme-skills.git"
$env:HERMES_HOME = "$Root\alice"
& $Hermes guild init $repo --name "Acme Eng" --member Alice --reviewer Alice --reviewer Bob
$env:HERMES_HOME = "$Root\bob"
& $Hermes guild join $repo --member Bob

# A skill Alice's agent "learned" last week, with the kind of details real skills pick up.
$skill = "$Root\alice\skills\deploy-preview"
New-Item -ItemType Directory -Force $skill | Out-Null
$skillText = @'
---
name: deploy-preview
description: Deploy a pull request to a Render preview environment and post the URL back to the PR.
---
# Deploy a PR preview

## When to Use
The user asks for a preview or staging link for a branch or PR.

## Procedure
1. `cd C:\Users\alice\code\acme-web` and run `npm run build`.
2. Call the Render API with `RENDER_API_KEY=rnd_8fK2qLx9VmP3sT7wQz1aB4cD6eF0gH2j`:
   `curl -H "Authorization: Bearer rnd_8fK2qLx9VmP3sT7wQz1aB4cD6eF0gH2j" https://api.render.com/v1/services`
3. Wait for status `live`, then comment the URL on the PR.
4. If the build fails, ping alice.okafor@acme-internal.com or check the box at 10.20.1.15.

## Pitfalls
- Render's free tier sleeps; open the URL once before sharing it.
'@
[System.IO.File]::WriteAllText("$skill\SKILL.md", $skillText)

Write-Host ""
Write-Host "Ready. Alice: `$env:HERMES_HOME='$Root\alice'   Bob: `$env:HERMES_HOME='$Root\bob'"
Write-Host "Each member needs a model once: run 'hermes model' in each (or pass -ModelConfigFrom)."
