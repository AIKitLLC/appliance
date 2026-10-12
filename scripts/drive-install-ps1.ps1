<#
Run install.ps1 against a FAKE machine and assert on what it refused or did.
The Windows counterpart to scripts/drive-install-sh.sh, scenario for scenario.

    powershell -NoProfile -File scripts\drive-install-ps1.ps1 offline
    pwsh -NoProfile -File scripts\drive-install-ps1.ps1 network
    pwsh -NoProfile -File scripts\drive-install-ps1.ps1 no-docker

WHY THIS RUNS WITHOUT DOCKER. A GitHub-hosted windows runner has no Docker
Desktop and cannot run Linux containers, so the appliance cannot start there at
all — but every refusal in install.ps1 is reachable, and "no Docker here" is the
runner's natural state rather than something to simulate. That makes the whole
refusal surface a per-PR gate; the full install on Windows needs a machine with
Docker Desktop and belongs in a nightly lane.

WHY BOTH HOSTS. PS 5.1 ships with Windows and is what a stranger pipes this
into; PS 7 is what a developer has. They differ on web requests, codepages and
$LASTEXITCODE propagation, and install.ps1 carries code for 5.1 specifically
(TLS 1.2, -UseBasicParsing, the ASCII-only rule). Run under each.

EMBABEL_TEST_REF picks the ref the network scenarios download (default: main).
#>
[CmdletBinding()]
param([string]$Which = 'offline')

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$ref = if ($env:EMBABEL_TEST_REF) { $env:EMBABEL_TEST_REF } else { 'main' }
$work = Join-Path $env:TEMP "drive-install-ps1-$([Guid]::NewGuid().ToString('N'))"
$passed = 0
$failed = 0

# A PATH with the real tools and NO docker. system32 carries tar.exe (Windows 10
# 1803+), which install.ps1 needs and checks for; the stub dir carries whatever
# a scenario adds back. Keeping the runner's full PATH would hand us its own
# docker.exe and make "no Docker" untestable.
$stub = Join-Path $work 'stub'
$minPath = "$stub;$env:SystemRoot\system32;$env:SystemRoot"

function New-Stub {
    param([string]$Name, [string]$Body)
    New-Item -ItemType Directory -Force -Path $stub | Out-Null
    # .cmd, not .ps1: Get-Command finds it, & runs it, and no execution policy
    # applies — the same reason install.ps1 writes its own shim as .cmd.
    Set-Content -Path (Join-Path $stub "$Name.cmd") -Value $Body -Encoding ASCII
}

# Answers the three questions install.ps1 asks. Each exit code is overridable so
# one stub covers "working", "no compose" and "not running"; `model status`
# fails by default because no Model Runner is the common case.
function New-DockerStub {
    New-Stub docker @'
@echo off
if "%1 %2"=="compose version" exit /b %STUB_COMPOSE_EXIT%
if "%1"=="info" exit /b %STUB_INFO_EXIT%
if "%1 %2"=="model status" exit /b 1
exit /b 0
'@
}

# Find-Python prefers the py launcher, then python3.13 … python. Stub every name
# it tries: a runner with real Python would otherwise launch the real wizard.
# The log is what lets a scenario assert WHICH mode script was handed off to.
function New-PythonStub {
    foreach ($name in 'py', 'python3.13', 'python3.12', 'python3.11', 'python3.10', 'python3', 'python') {
        New-Stub $name @"
@echo off
rem -3 is the py launcher's version selector; -c is the >=3.9 probe and the shim writer.
if "%1"=="-3" shift
if "%1"=="-c" exit /b 0
echo %* >> "$work\python.log"
exit /b 0
"@
    }
}

function Reset-Machine {
    Remove-Item -Recurse -Force $stub, (Join-Path $work 'home'), (Join-Path $work 'python.log') -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force -Path (Join-Path $work 'home') | Out-Null
    $env:STUB_COMPOSE_EXIT = '0'
    $env:STUB_INFO_EXIT = '0'
}

function Invoke-Install {
    param([hashtable]$Env = @{})
    $saved = @{}
    # EMBABEL_BIN_DIR into the work area ON PURPOSE: install.ps1 writes the shim
    # and prepends it to the User PATH for real, and a test has no business
    # leaving either in a developer's profile. The PATH write still happens —
    # it is what the script does — so run this on a runner or accept the entry.
    # Merged key by key, NOT `@{...} + $Env`: PS 5.1 throws "Item has already
    # been added" on a duplicate key, so a scenario overriding EMBABEL_HOME —
    # which is the whole point of the occupied-directory one — killed the run
    # under the host this script exists to exercise.
    $vars = @{
        PATH            = $minPath
        EMBABEL_HOME    = (Join-Path $work 'home\embabel\worlds')
        EMBABEL_REF     = $ref
        EMBABEL_BIN_DIR = (Join-Path $work 'bin')
    }
    foreach ($k in $Env.Keys) { $vars[$k] = $Env[$k] }
    foreach ($k in $vars.Keys) {
        $saved[$k] = [Environment]::GetEnvironmentVariable($k)
        [Environment]::SetEnvironmentVariable($k, $vars[$k])
    }
    try {
        # A child host, so an `exit 1` inside install.ps1 ends IT and not this
        # harness, and so 5.1 vs 7 is the host under test rather than the caller.
        $exe = (Get-Process -Id $PID).Path
        $out = & $exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $here 'install.ps1') 2>&1 |
            Out-String
        $script:status = $LASTEXITCODE
        $script:output = $out
    } finally {
        foreach ($k in $saved.Keys) { [Environment]::SetEnvironmentVariable($k, $saved[$k]) }
    }
}

function Assert-Scenario {
    param([string]$Scenario, [int]$WantStatus, [string[]]$Expect)
    $bad = @()
    if ($status -ne $WantStatus) { $bad += "exited $status, wanted $WantStatus" }
    foreach ($needle in $Expect) {
        if ($output -notlike "*$needle*") { $bad += "missing: $needle" }
    }
    if ($bad) {
        $script:failed++
        Write-Host ("  x  {0} -- {1}" -f $Scenario, ($bad -join '; '))
        ($output -split "`r?`n" | Select-Object -Last 12) | ForEach-Object { Write-Host "       | $_" }
    } else {
        $script:passed++
        Write-Host "  ok $Scenario"
    }
}

function Test-Wanted {
    param([string]$Name, [string]$Lane)
    switch ($Which) {
        'all' { $true }
        'offline' { $Lane -eq 'offline' }
        'network' { $Lane -eq 'network' }
        default { $Which -eq $Name }
    }
}

New-Item -ItemType Directory -Force -Path $work | Out-Null
Write-Host ("  {0} on PowerShell {1}" -f $Which, $PSVersionTable.PSVersion)

# THE USER PATH IS RESTORED, because a successful install genuinely rewrites it:
# install.ps1 prepends its shim directory to the persisted User PATH, and this
# harness points that at a temp directory it then deletes. On a CI runner the
# litter dies with the machine; on a developer's box it is a permanent entry
# pointing at nothing, added by running the tests. Snapshot, and put it back.
$originalUserPath = [Environment]::GetEnvironmentVariable('Path', 'User')

try {
    # ── no Docker at all ────────────────────────────────────────────────────
    if (Test-Wanted 'no-docker' 'offline') {
        Reset-Machine
        Invoke-Install
        Assert-Scenario 'no-docker' 1 @(
            "No 'docker' on your PATH.",
            'Embabel needs Docker, and it is the only thing you have to install yourself.',
            # The Windows link, not the generic one: this is the copy that
            # differs from copy/docker-required.txt by design.
            'docs.docker.com/desktop/install/windows-install/')
    }

    # ── Rancher Desktop on containerd ───────────────────────────────────────
    if (Test-Wanted 'rancher' 'offline') {
        Reset-Machine
        New-Stub rdctl "@echo off`r`nexit /b 0"
        New-Stub nerdctl "@echo off`r`nexit /b 0"
        Invoke-Install
        Assert-Scenario 'rancher-containerd' 1 @(
            'Rancher Desktop is set to containerd.',
            'rdctl set --container-engine docker')
        if ($output -like '*the only thing you have to install yourself*') {
            $script:failed++
            Write-Host '  x  rancher-containerd -- printed the install-Docker essay at somebody who has a runtime'
        }
    }

    # ── Docker there, Compose v1 or absent ──────────────────────────────────
    if (Test-Wanted 'compose-missing' 'offline') {
        Reset-Machine
        New-DockerStub
        Invoke-Install @{ STUB_COMPOSE_EXIT = '1' }
        Assert-Scenario 'compose-missing' 1 @('Docker Compose v2 is required')
    }

    # ── Docker installed but not started ────────────────────────────────────
    if (Test-Wanted 'not-running' 'offline') {
        Reset-Machine
        New-DockerStub
        Invoke-Install @{ STUB_INFO_EXIT = '1' }
        Assert-Scenario 'not-running' 1 @('Docker is installed but not running.')
    }

    # ── the target directory holds somebody else's files ────────────────────
    if (Test-Wanted 'occupied' 'offline') {
        Reset-Machine
        New-DockerStub
        $occupied = Join-Path $work 'home\occupied'
        New-Item -ItemType Directory -Force -Path $occupied | Out-Null
        Set-Content -Path (Join-Path $occupied 'notes.txt') -Value 'mine' -Encoding ASCII
        Invoke-Install @{ EMBABEL_HOME = $occupied }
        Assert-Scenario 'occupied-dir' 1 @('exists and is not an Embabel install.')
        if (-not (Test-Path (Join-Path $occupied 'notes.txt'))) {
            $script:failed++
            Write-Host '  x  occupied-dir -- it refused AND took the files with it'
        }
    }

    # ── a ref that does not exist ───────────────────────────────────────────
    if (Test-Wanted 'bad-ref' 'network') {
        Reset-Machine
        New-DockerStub
        Invoke-Install @{ EMBABEL_REF = "no-such-ref-$PID" }
        Assert-Scenario 'bad-ref' 1 @('EMBABEL_REF')
    }

    # ── the whole way through ───────────────────────────────────────────────
    # The shim is the Windows-only half of a successful install, and ASCII is
    # not cosmetic: a cmd file written UTF-16 or with a BOM is not executable.
    if (Test-Wanted 'happy' 'network') {
        foreach ($mode in 'worlds', 'me') {
            Reset-Machine
            New-DockerStub
            New-PythonStub
            Invoke-Install @{ EMBABEL_MODE = $mode }
            Assert-Scenario "happy-$mode" 0 @('Done. Starting setup')
            if (-not (Test-Path (Join-Path $work 'home\embabel\worlds\setup.py'))) {
                $script:failed++
                Write-Host "  x  happy-$mode -- no setup.py after a successful install"
            }
            $log = Get-Content (Join-Path $work 'python.log') -ErrorAction SilentlyContinue
            if ($log -notlike "*.\$mode.py*") {
                $script:failed++
                Write-Host "  x  happy-$mode -- handed off to $($log -join ' ')"
            }
        }

        $shim = Join-Path $work 'bin\embabel.cmd'
        if (-not (Test-Path $shim)) {
            $script:failed++
            Write-Host '  x  happy -- no embabel.cmd shim was written'
        } else {
            $bytes = [IO.File]::ReadAllBytes($shim)
            if ($bytes[0] -eq 0xEF -or $bytes[0] -eq 0xFF -or ($bytes | Where-Object { $_ -gt 127 })) {
                $script:failed++
                Write-Host '  x  happy -- embabel.cmd is not plain ASCII; cmd cannot run a BOM or UTF-16 batch file'
            }
            if ((Get-Content $shim -Raw) -notlike '*chcp 65001*') {
                $script:failed++
                Write-Host '  x  happy -- the shim lost chcp 65001; wizard prompts become mojibake'
            }
        }
    }
} finally {
    Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
    $nowUserPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    if ($nowUserPath -ne $originalUserPath) {
        [Environment]::SetEnvironmentVariable('Path', $originalUserPath, 'User')
        Write-Host '  -- restored your User PATH (install.ps1 had prepended the test shim dir)'
    }
}

Write-Host ''
Write-Host ("  {0} passed, {1} failed" -f $passed, $failed)
if ($failed -gt 0) { exit 1 }
