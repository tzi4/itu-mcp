# Pi application snapshot

This fork includes the application used on the maintainer's Raspberry Pi,
captured on **10 October 2026**. The active application release was
`20261003-help-ticket-details`; its core version was `0.7.6.post1.dev18`.
The source manifest records SHA-256 digests for both Python packages and the
PaperBridge HTML resources. These application files were copied without changes.

The snapshot includes the six pending contributions, PaperBridge's actual active
overlay, remote managed-file support and registration draft saving. The private
Pi launcher enables some features through environment settings. Those settings
are operator choices; fresh installations retain each source-defined default.

Private credentials, environment files, service secrets, cookies, PDFs, mail,
archive databases, browser state and the maintainer's networking configuration
are not distributed. Python and browser runtimes are installed on each user's
own machine. Source identity does not mean the machines share settings or data.

PaperBridge is bundled in the same distribution to keep installation independent
of an unpublished `tzipi-paperbridge` PyPI package. Upstream PyPI/registry
publication automation was removed from this fork. The original author and
license remain credited; package and support links target tzi4/itu-mcp.

`scripts/display_launcher.py` preserves the Pi's optional private-display helper
for operator-managed, headed Linux deployments. It requires Xvfb and xauth,
owns one reserved display, and expects an absolute executable application
launcher. Use it only in a service with a private temporary directory and
configure your own paths. It is not needed for default stdio or headless use.

## Validation and security review

Offline tests cover the university tools, PaperBridge permissions, isolated
archives, page rendering, remote file limits and actual MCP stdio transport.
Real-browser fixtures are opt-in and do not run in the default offline suite.
Live Pi health was checked without restarting its service.

Bandit reports six low-severity warnings for `subprocess` imports and fixed,
shell-free worker/browser-install invocations in the captured sources. These
were reviewed rather than changing the Pi application to add suppressions.
The checked-in baseline records the complete source hash and severity for each
warning. CI rejects new findings, scanning errors or changes to a reviewed file
until reviewed again. Both Python packages are scanned.
