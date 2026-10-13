# Distribution notes

PaperBridge is bundled with ITU MCP so installation does not depend on an
unpublished `tzipi-paperbridge` PyPI package. Package and support links point to
tzi4/itu-mcp-paperbridge. The original author, Git history and MIT license remain
credited.

Each user runs the server with their own account, storage and configuration.
Credentials, environment files, service secrets, cookies, PDFs, mail, databases,
browser state and personal networking configuration are not distributed.
Feature switches retain their source-defined defaults on fresh installations.

## Registration draft safety

Draft saves expose `previous_crns`, `added_crns` and `removed_crns`. Removing
courses or replacing an unreadable draft requires `allow_replace=true` as well
as explicit confirmation, unless the operator turned writes off. An unchanged draft
does not trigger a save POST. Rejection with changed or unavailable readback is
uncertain. Saving a draft does not enroll or drop courses.

## Optional Linux display helper

`scripts/display_launcher.py` provides an operator-managed private display for
headed Linux deployments. It requires Xvfb and xauth, owns one reserved display,
and expects an absolute executable application launcher. Use it in a service
with a private temporary directory and configure your own paths. Default stdio
and headless operation do not need it.

## Validation and security review

Offline tests cover the university tools, PaperBridge permissions, isolated
archives, page rendering, remote file limits and actual MCP stdio transport.
CI runs Python 3.11–3.14 on Linux and Python 3.12 on Windows. Real-browser
fixtures are opt-in and do not run in the default offline suite.

Bandit reports six reviewed low-severity warnings for subprocess imports and
fixed, shell-free worker/browser-install invocations. The baseline records each
complete source hash and severity. CI rejects new findings, scanning errors or
changes to a reviewed file until reviewed again. Both packages are scanned.
