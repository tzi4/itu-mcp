# Install or update ITU MCP + PaperBridge for the current user on Windows.
#
#   powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/tzi4/itu-mcp-paperbridge/main/scripts/install.ps1 | iex"
#
# It installs uv (https://docs.astral.sh/uv/) if it is missing, installs this
# fork from GitHub as a uv tool (uv fetches a suitable Python when needed), and
# then runs `itu-mcp --setup`, which asks for your ITU login with hidden input
# and adds the server to the MCP clients it finds. Run it again to update.
#
# Optional settings: $env:ITU_MCP_REF (branch, tag or commit; default main) and
# $env:ITU_MCP_SKIP_SETUP = "1" (install only).

function Install-ItuMcp {
    $ErrorActionPreference = 'Stop'
    $Repo = 'tzi4/itu-mcp-paperbridge'
    $Ref = if ($env:ITU_MCP_REF) { $env:ITU_MCP_REF } else { 'main' }
    $PythonRequest = '>=3.11,<3.15'

    if ($Ref -notmatch '^[A-Za-z0-9._/-]+$') {
        throw "ITU_MCP_REF may contain only letters, digits, '.', '_', '/' and '-'."
    }
    # Windows PowerShell 5.1 may not enable TLS 1.2 by default.
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

    $uv = Get-Command uv -ErrorAction SilentlyContinue
    $uvPath = if ($uv) { $uv.Source } else { $null }
    if (-not $uvPath) {
        $candidate = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
        if (Test-Path $candidate) { $uvPath = $candidate }
    }
    if (-not $uvPath) {
        Write-Host 'Installing uv, the Python package manager used to install ITU MCP...'
        powershell -ExecutionPolicy ByPass -NoProfile -Command 'irm https://astral.sh/uv/install.ps1 | iex'
        if ($LASTEXITCODE -ne 0) { throw 'Installing uv failed.' }
        foreach ($candidate in @((Join-Path $env:USERPROFILE '.local\bin\uv.exe'),
                                 (Join-Path $env:USERPROFILE '.cargo\bin\uv.exe'))) {
            if (Test-Path $candidate) { $uvPath = $candidate; break }
        }
        if (-not $uvPath) { throw 'uv was installed but could not be found; open a new terminal and run this again.' }
    }

    # Install the exact commit so the update check can compare it with GitHub.
    $commit = $null
    if ($Ref -match '^[0-9a-f]{40}$') {
        $commit = $Ref
    } else {
        try {
            $response = Invoke-RestMethod -UseBasicParsing -TimeoutSec 20 `
                -Headers @{ 'User-Agent' = 'itu-mcp-installer' } `
                -Uri "https://api.github.com/repos/$Repo/commits/$Ref"
            if ($response.sha -match '^[0-9a-f]{40}$') { $commit = $response.sha }
        } catch {
            $commit = $null
        }
    }
    if ($commit) {
        $source = "https://github.com/$Repo/archive/$commit.zip"
        Write-Host "Installing ITU MCP + PaperBridge from $Repo at commit $($commit.Substring(0, 7))..."
    } else {
        $source = "https://github.com/$Repo/archive/$Ref.zip"
        Write-Host "Could not look up the commit of '$Ref'; installing it without update notices."
    }

    & $uvPath tool install --python $PythonRequest --reinstall-package itu-mcp "itu-mcp @ $source"
    if ($LASTEXITCODE -ne 0) { throw 'uv could not install ITU MCP.' }

    $binDir = (& $uvPath tool dir --bin).Trim()
    $ituMcp = Join-Path $binDir 'itu-mcp.exe'
    if (-not (Test-Path $ituMcp)) { throw "The itu-mcp command was not found in $binDir." }
    & $ituMcp --version

    $pathEntries = ($env:Path -split ';') | ForEach-Object { $_.TrimEnd('\') }
    if ($pathEntries -notcontains $binDir.TrimEnd('\')) {
        Write-Host "Note: $binDir is not on your PATH. Run ``uv tool update-shell`` to add it."
    }

    $savedEnv = Join-Path $env:USERPROFILE '.itu-mcp\.env'
    if ($env:ITU_MCP_SKIP_SETUP -eq '1') {
        Write-Host "Installed. Run `"$ituMcp`" --setup to save your login and configure your MCP clients."
    } elseif ((Test-Path $savedEnv) -and (Select-String -Path $savedEnv -Pattern '^NINOVA_PASSWORD[=].' -Quiet)) {
        # Running the same command again is the update; keep the saved login and client settings.
        Write-Host "Updated. Your saved login and client settings are unchanged; restart your MCP client."
    } else {
        & $ituMcp --setup
    }
}

Install-ItuMcp
