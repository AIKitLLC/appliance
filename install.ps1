# Embabel Worlds -- one-command install (Windows / PowerShell).
#
#   irm https://worlds.embabel.com/install.ps1 | iex
#
# The Windows counterpart to install.sh. Same shape, same three steps:
#
#   1. check Docker is installed and running
#   2. download this repo into $HOME\embabel\worlds
#   3. hand off to worlds.py / me.py, which owns the actual setup
#
# It installs nothing globally, needs no admin, and writes only inside that one
# directory plus a two-line shim in $env:LOCALAPPDATA\Programs\embabel\bin.
# To undo it: `docker compose down -v` in there, delete the directory, and
# remove the bin dir from PATH.
#
# Environment variables (identical semantics to install.sh):
#   EMBABEL_HOME     where to install         (default: $HOME\embabel\worlds)
#   EMBABEL_REF      branch or tag to fetch   (default: main)
#   EMBABEL_MODE     worlds | me              (default: worlds)
#   EMBABEL_REPO     org/repo of the source   (default: embabel-worlds/appliance)
#   EMBABEL_TOKEN    GitHub token, private forks only
#   EMBABEL_BIN_DIR  where to put the shim    (default: $env:LOCALAPPDATA\Programs\embabel\bin)
#
# ASCII-only on purpose. PS 5.1 reads .ps1 files as the system ANSI codepage
# unless the file carries a UTF-8 BOM; box-drawing chars and em-dashes then
# come back as Mojibake and the parser dies. Piping over the wire (irm | iex)
# would be safe -- the site serves this as text/plain; charset=utf-8 -- but
# the file also has to work when someone saves and runs it locally.

#Requires -Version 5.1
$ErrorActionPreference = 'Stop'

# Force UTF-8 on the console so em-dashes, box borders, and other non-ASCII
# characters in wizard prompts (from setup.py, the appliance's own logs) render
# as intended. The Windows console codepage defaults to OEM 437/850 in most
# shells, and Python 3 emits UTF-8 by default -- displayed in cp437, an em dash
# appears as a stray accented letter, box rules turn into scrambled shapes, and
# typed input can lose characters at codepage boundaries. Setting both channels
# fixes this
# session; the .cmd shim written below does the same via `chcp 65001` for
# later runs. Silent if the current host does not surface Console (e.g. a
# service context) -- setup will just look the way it did before.
try {
  [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
  [Console]::InputEncoding  = [System.Text.Encoding]::UTF8
} catch { }

# PYTHONUTF8=1 forces Python's default text encoding to UTF-8 for FILES and
# SUBPROCESS pipes -- a separate context from console I/O. Without it, on
# Windows Python 3 defaults to locale.getpreferredencoding() (cp1252 in en-US)
# for open() and subprocess.run(text=True), which crashes with UnicodeDecodeError
# on any UTF-8 byte in a docker log, an .env file, or a sibling on PATH written
# by another tool. Individual encoding="utf-8" arguments on hot call sites remain
# as backstops for callers that bypass this launcher, but this covers the whole
# process at once.
$env:PYTHONUTF8 = "1"

# ---- Config ------------------------------------------------------------------
$Repo = if ($env:EMBABEL_REPO) { $env:EMBABEL_REPO } else { 'embabel-worlds/appliance' }
$Ref  = if ($env:EMBABEL_REF)  { $env:EMBABEL_REF  } else { 'main' }
# EMBABEL_DOOR: the old name, still honoured -- same reasoning as install.sh.
$Mode = if ($env:EMBABEL_MODE) { $env:EMBABEL_MODE } elseif ($env:EMBABEL_DOOR) { $env:EMBABEL_DOOR } else { 'worlds' }

# Default install dir: $HOME\embabel\worlds -- see install.sh for why a single
# directory serves both doors. An existing install is used where it is found,
# whichever name it has, so a rename does not strand anyone's data.
$DefaultHome = Join-Path $HOME 'embabel\worlds'
if (-not $env:EMBABEL_HOME) {
  foreach ($cand in @((Join-Path $HOME 'embabel-worlds'), (Join-Path $HOME 'embabel-me'))) {
    if (Test-Path (Join-Path $cand 'setup.py')) { $DefaultHome = $cand; break }
  }
}
$HomeDir = if ($env:EMBABEL_HOME) { $env:EMBABEL_HOME } else { $DefaultHome }

# ---- Output helpers ----------------------------------------------------------
# Write-Host with -ForegroundColor works in every Windows console -- legacy
# conhost, Windows Terminal, VS Code integrated terminal, PS 5.1 and PS 7 --
# without the VT-processing dance ANSI escapes need on PS 5.1. Losing "bold"
# and "dim" is the price; we fake dim with DarkGray.
function Say  { param($m) Write-Host "  $m" }
function Step { param($m) Write-Host "  ::" -NoNewline -ForegroundColor Cyan; Write-Host " $m" }
function OK   { param($m) Write-Host "  OK" -NoNewline -ForegroundColor Green; Write-Host " $m" }
function Note { param($m) Write-Host "  $m" -ForegroundColor DarkGray }
function Warn { param($m) Write-Host "  !!" -NoNewline -ForegroundColor Yellow; Write-Host " $m" }
function Die  { param($m) Write-Host ""; Write-Host "  !!" -NoNewline -ForegroundColor Yellow; Write-Host " $m" -ForegroundColor Red; Stop-Install 1 }

# ---- Stopping without closing the window -------------------------------------
# `irm ... | iex` runs this script INSIDE the user's own PowerShell session, so an
# `exit` there does not end the script -- it closes their window, and takes with it
# whatever setup or Die had just printed: a failed install looked like the shell
# crashing, with no reason left on screen. So under iex: record the code in
# $LASTEXITCODE and `break`, which ends the script and leaves the session alone
# (the same choice Scoop's installer makes). Run as a FILE (`-File install.ps1`,
# which is how scripts\drive-install-ps1.ps1 drives it) `exit` ends only this
# script's host, and its exit code is what that harness asserts on -- so keep it.
#
# The same goes for the working directory: setup has to run from the install
# directory, and under iex a Set-Location there moved the user's own prompt into it
# and left it there. So the move is a Push-Location, and stopping pops it.
$RunAsFile = [bool]$PSCommandPath
$MovedIntoHome = $false
function Stop-Install {
  param([int]$Code)
  $global:LASTEXITCODE = $Code
  if ($MovedIntoHome) { Pop-Location }
  if ($RunAsFile) { exit $Code }
  break
}

# ---- Banner ------------------------------------------------------------------
# The door you are actually installing, described in the SITE'S words. Same as
# install.sh: an installer that pitches the product differently from the page
# that sent them reads as a different product.
try { $cols = $Host.UI.RawUI.WindowSize.Width } catch { $cols = 80 }

if ($Mode -eq 'worlds') {
  if ($cols -ge 102) {
    Write-Host ""
    $banner = @'
   ___  ___    _______ .___  ___. .______        ___      .______    _______  __         ___  ___
  /  / /  /   |   ____||   \/   | |   _  \      /   \     |   _  \  |   ____||  |        \  \ \  \
 /  / /  /    |  |__   |  \  /  | |  |_)  |    /  ^  \    |  |_)  | |  |__   |  |         \  \ \  \
<  < <  <     |   __|  |  |\/|  | |   _  <    /  /_\  \   |   _  <  |   __|  |  |          >  > >  >
 \  \ \  \    |  |____ |  |  |  | |  |_)  |  /  _____  \  |  |_)  | |  |____ |  `----.    /  / /  /
  \__\ \__\   |_______||__|  |__| |______/  /__/     \__\ |______/  |_______||_______|   /__/ /__/
'@
    Write-Host $banner -ForegroundColor Cyan
  } else {
    Write-Host ""
    Write-Host "  <<  E M B A B E L  >>" -ForegroundColor Cyan
  }
  Write-Host ""
  Write-Host "  Embabel Worlds " -NoNewline
  Write-Host "-- the world your AI acts in" -ForegroundColor DarkGray
  Write-Host "  A governed, living knowledge graph of your business, derived from the" -ForegroundColor DarkGray
  Write-Host "  systems you already run and owned by you. Insight across the whole" -ForegroundColor DarkGray
  Write-Host "  business, in days." -ForegroundColor DarkGray
  Write-Host ""
} else {
  Write-Host ""
  Write-Host "  Embabel Me " -NoNewline
  Write-Host "-- your own assistant, on your own machine" -ForegroundColor DarkGray
  Write-Host ""
}

# ---- 1. Docker ---------------------------------------------------------------
# The one prerequisite we cannot install for you, and the one worth failing
# early and clearly on: everything below is pointless without it.
function Show-DockerRequired {
  Write-Host ""
  # NOTE: the copy below is DUPLICATED from copy/docker-required.txt in the
  # appliance repo, byte-for-byte, and check-copy.py fails the build if it
  # drifts. Same rule as install.sh -- this script runs BEFORE there is a
  # checkout to read the canonical file from.
  @"
  Embabel needs Docker, and it is the only thing you have to install yourself.

  Embabel is not one program. It is a knowledge graph, a server, a console, a
  document converter, a metrics stack and a sandbox that runs code your agents
  write -- six or seven pieces that have to find each other, come up in the right
  order and agree on their versions. Docker is how they arrive together, already
  wired, in about a command.

  It is also what keeps your world YOURS. Every one of those pieces runs on this
  machine: your documents are converted here, turned into vectors here by a model
  that runs here, and stored in a graph here. Nothing is uploaded to us, and
  there is no account to make. The only traffic that leaves is your model
  provider's, when you ask a question and your own key pays for the answer.

  And it is what makes this reversible. There is no installer scattering files
  across your system, no Java, no Node, no database to configure and no service
  left running when you are done. `embabel down` stops it; `embabel uninstall`
  removes it. What is left behind is the directory you chose.

  Install Docker Desktop for Windows, start it, and run this again:

      https://docs.docker.com/desktop/install/windows-install/
"@ | Write-Host
}

# Rancher Desktop on containerd -- see install.sh for the reasoning. The ASCII
# arrows and dashes below are this file's convention, not a different text.
function Show-RancherContainerd {
  @"

  Rancher Desktop is installed here and set to containerd, which ships nerdctl and
  no docker command at all. That is a setting, not a missing product, and it is
  the setting Rancher Desktop installs with.

  Switch it to the Docker engine:

      rdctl set --container-engine docker

  or open Preferences -> Container Engine and choose dockerd (moby). Then run this
  again.

  Nothing is deleted by switching. The two engines keep separate image stores, so
  images you pulled with nerdctl go quiet until you switch back -- they are still on
  disk, and nothing outside those stores is touched.

  Embabel needs the Docker API itself, not only a command that looks like Docker.
  The appliance starts the sandbox your agents' code runs in as a sibling
  container, through the docker socket, and containerd serves no such socket. On
  containerd this would install, start, and then fail at the exact moment it first
  ran code -- which is a worse outcome than stopping here.
"@ | Write-Host
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
  # BOTH tools, not either: nerdctl alone is also plain containerd, where every
  # sentence about Preferences is wrong. rdctl beside it is Rancher Desktop.
  if ((Get-Command rdctl -ErrorAction SilentlyContinue) -and (Get-Command nerdctl -ErrorAction SilentlyContinue)) {
    Show-RancherContainerd
    Die "No 'docker' on your PATH -- Rancher Desktop is set to containerd."
  }
  Show-DockerRequired
  Die "No 'docker' on your PATH."
}

# `docker compose version` -- Compose v2. Suppress output; exit code is the answer.
& docker compose version 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { Die "Docker Compose v2 is required -- update Docker Desktop, or install the compose plugin." }

# Installed but not started is a DIFFERENT problem from missing.
& docker info 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { Die "Docker is installed but not running. Start Docker Desktop, then run this again." }

# Model Runner -- an OPTION, not a prerequisite. Same reasoning, and the same
# rewrite, as install.sh: an appliance ships with no embedding model, and the
# people who cannot enable this at all are the ones the old wording alarmed.
& docker model status 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
  Write-Host ""
  @"
  No Docker Model Runner here, which decides one thing only: if you turn document
  search on, the embedding model is a hosted one, set up from the key you give the
  appliance and downloading nothing.

      embabel embeddings use hosted

  (On Docker Desktop you can enable Model Runner instead -- Settings -> AI -- and run
  that model on this machine with `embabel embeddings use local`.)

  Nothing is waiting on this. An appliance ships with no embedding model at all,
  and documents are the only feature that uses one.
"@ | Write-Host
  Write-Host ""
}

# ---- 2. Download -------------------------------------------------------------
if ((Test-Path $HomeDir) -and (-not (Test-Path (Join-Path $HomeDir 'setup.py'))) -and (Get-ChildItem $HomeDir -Force -ErrorAction SilentlyContinue)) {
  Die "$HomeDir exists and is not an Embabel install. Move it, or set `$env:EMBABEL_HOME."
}

if (Test-Path (Join-Path $HomeDir 'setup.py')) {
  Step "Updating $HomeDir (your .env and data are untouched)..."
} else {
  Step "Installing into $HomeDir..."
}

$null = New-Item -ItemType Directory -Force -Path $HomeDir

# Same two sources as install.sh, same order rule: codeload first (avoids the
# API's redirect hop), API is the fallback and goes first when a token is set.
$Tarball = Join-Path $env:TEMP "embabel-appliance-$([Guid]::NewGuid().ToString('N')).tar.gz"

try {
  $CodeLoad = "https://codeload.github.com/$Repo/tar.gz/$Ref"
  $Api      = "https://api.github.com/repos/$Repo/tarball/$Ref"
  $Sources  = if ($env:EMBABEL_TOKEN) { @($Api, $CodeLoad) } else { @($CodeLoad, $Api) }
  $Headers  = if ($env:EMBABEL_TOKEN) { @{ Authorization = "Bearer $($env:EMBABEL_TOKEN)" } } else { @{} }

  # TLS 1.2 -- PS 5.1 on stock Win 10 defaults to TLS 1.0 for .NET web requests,
  # which GitHub has been retiring. Setting it here is a no-op on PS 7 and on
  # already-modernised 5.1 hosts; on old ones it is the whole difference
  # between a 200 and an unhelpful handshake error.
  [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

  $downloaded = $false
  $code = 0
  foreach ($url in $Sources) {
    for ($t = 0; $t -lt 3; $t++) {
      try {
        # -UseBasicParsing: forbids the IE COM engine (which needs a
        # first-run profile and does not exist on Server Core).
        Invoke-WebRequest -Uri $url -OutFile $Tarball -Headers $Headers -UseBasicParsing
        $downloaded = $true
        break
      } catch {
        $resp = $_.Exception.Response
        if ($resp -and $resp.StatusCode) {
          $code = [int]$resp.StatusCode
        } else {
          $code = 0
        }
        # A missing ref is not a blip; retrying it just makes the person wait.
        if ($code -eq 404) { break }
        Start-Sleep -Seconds 2
      }
    }
    if ($downloaded) { break }
  }

  if (-not $downloaded) {
    switch ($code) {
      404 { Die "No '$Ref' in $Repo -- check `$env:EMBABEL_REF, or `$env:EMBABEL_REPO if you are using a fork." }
      { $_ -in 401,403 } { Die "GitHub refused the download ($code). Check `$env:EMBABEL_TOKEN if $Repo is private, or wait out a rate limit." }
      0 { Die "Could not reach GitHub. Check your connection or proxy." }
      default { Die "GitHub could not serve the download ($code) -- its problem, not yours. Try again in a minute." }
    }
  }

  # tar.exe ships with Windows 10 1803+ and Windows Server 2019+. On anything
  # older this fails clearly enough that the user knows what happened.
  if (-not (Get-Command tar -ErrorAction SilentlyContinue)) {
    Die "'tar' is not on your PATH. This script needs the tar bundled with Windows 10 (1803+) or newer."
  }
  & tar -xzf $Tarball -C $HomeDir --strip-components=1
  if ($LASTEXITCODE -ne 0) { Die "Could not unpack the download." }

  # Post-condition: a download that returns the wrong thing is worse than one
  # that fails, so prove the install is actually here before promising setup.
  if (-not (Test-Path (Join-Path $HomeDir 'setup.py'))) {
    Die "The download did not contain an Embabel appliance. Nothing was installed."
  }
} finally {
  Remove-Item $Tarball -ErrorAction SilentlyContinue
}

# ---- 2b. Put 'embabel' on PATH -----------------------------------------------
# A cmd shim rather than a ps1 -- .cmd is executable from PowerShell, cmd,
# Windows Terminal, VS Code, Git Bash, and every other shell a Windows user
# might launch it from. .ps1 needs an execution policy dance in cmd.
$BinDir = if ($env:EMBABEL_BIN_DIR) { $env:EMBABEL_BIN_DIR } else { Join-Path $env:LOCALAPPDATA 'Programs\embabel\bin' }
$null = New-Item -ItemType Directory -Force -Path $BinDir

$Shim = @"
@echo off
rem Forwards to the Embabel appliance in $HomeDir. Written by install.ps1.
rem Prefers the Python launcher; falls back to python.exe on PATH.
rem chcp 65001: force the console to UTF-8 so em-dashes and box borders in
rem wizard prompts render as intended -- default OEM 437/850 turns them into
rem stray accented letters and other garbage, and setup becomes unreadable.
rem >nul silences the "Active code page: 65001" line every invocation prints.
rem PYTHONUTF8=1: force Python's default text encoding to UTF-8 for files
rem and subprocess pipes (a separate context from the console codepage).
rem Without it, docker log reads and .env file reads default to cp1252 and
rem die on any UTF-8 byte -- see install.ps1's own comment for the whole
rem story. Set as a session env var so it applies only to this invocation.
chcp 65001 >nul
set PYTHONUTF8=1
where py >nul 2>nul
if not errorlevel 1 (
  py -3 "$HomeDir\embabel" %*
  exit /b %errorlevel%
)
where python >nul 2>nul
if not errorlevel 1 (
  python "$HomeDir\embabel" %*
  exit /b %errorlevel%
)
echo embabel: Python 3.9+ not found. Install from https://python.org 1>&2
exit /b 1
"@
Set-Content -Path (Join-Path $BinDir 'embabel.cmd') -Value $Shim -Encoding ASCII

# ANOTHER 'embabel' MAY ALREADY WIN -- same warning as install.sh, same reasoning.
$existing = (Get-Command embabel -ErrorAction SilentlyContinue | Select-Object -First 1)
$shimPath = Join-Path $BinDir 'embabel.cmd'
if ($existing -and $existing.Source -ne $shimPath) {
  Warn "another 'embabel' already comes first on your PATH:"
  Note "        $($existing.Source)"
  Note "      To use this one, put $BinDir ahead of it,"
  Note "      or run it by path: $shimPath"
  Write-Host ""
}

# Persist to User PATH if not already there -- takes effect in new shells.
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if ($null -eq $userPath) { $userPath = '' }
if (($userPath -split ';') -notcontains $BinDir) {
  [Environment]::SetEnvironmentVariable('Path', "$BinDir;$userPath", 'User')
  Say "Installed the 'embabel' command to $BinDir and added it to your User PATH."
  Note "Open a new terminal to use it there -- this one has been updated for the rest of this session."
} else {
  Say "Installed the 'embabel' command to $BinDir."
}
# Make it usable in THIS session immediately, regardless of PATH-update path.
if (($env:Path -split ';') -notcontains $BinDir) { $env:Path = "$BinDir;$env:Path" }
Write-Host ""

# ---- 3. Hand off -------------------------------------------------------------
# setup.py owns the real flow. Same rule as install.sh: no second
# implementation of any of it here.
function Find-Python {
  # Prefer the py launcher's `-3` (newest 3.x it can find). Fall back to raw
  # binaries in the order install.sh uses.
  if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)" 2>$null
    if ($LASTEXITCODE -eq 0) { return @('py', '-3') }
  }
  foreach ($cand in 'python3.13','python3.12','python3.11','python3.10','python3','python') {
    if (Get-Command $cand -ErrorAction SilentlyContinue) {
      & $cand -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)" 2>$null
      if ($LASTEXITCODE -eq 0) { return @($cand) }
    }
  }
  return $null
}

$Py = Find-Python
if (-not $Py) {
  Die "Embabel needs Python 3.9 or newer -- install from https://python.org (check 'Add python.exe to PATH')."
}

OK "Done. Starting setup -- after this, use the 'embabel' command."
Write-Host ""

Push-Location $HomeDir
$MovedIntoHome = $true
$Script = if ($Mode -eq 'worlds') { '.\worlds.py' } else { '.\me.py' }

# Note on stdin: install.sh reopens /dev/tty because `curl ... | sh` has already
# consumed the pipe's stdin. In PowerShell, `iex` consumes the pipeline once
# to evaluate the script; subprocesses launched from within run in the host
# console and inherit its real stdin/stdout -- no reopening needed. If a future
# host ever changes that assumption, the failure mode is a wizard that cannot
# read the first prompt, which matches the sh symptom and is easy to spot.
if ($Py.Length -eq 1) {
  & $Py[0] $Script @args
} else {
  & $Py[0] $Py[1] $Script @args
}
$SetupExit = $LASTEXITCODE
if ($SetupExit -ne 0) {
  Write-Host ""
  Warn "Setup did not finish (exit $SetupExit). Read the message above, then resume with: embabel up"
}
Stop-Install $SetupExit
