"""Reject unreviewed Bandit findings against the reviewed source baseline."""
from pathlib import Path
import hashlib
import json
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    result = subprocess.run([sys.executable, '-m', 'bandit', '-r', 'src', '-f', 'json', '-q'],
                            cwd=ROOT, capture_output=True, text=True, check=False)
    if result.returncode not in (0, 1):
        raise SystemExit('Bandit could not complete the scan.')
    try:
        report = json.loads(result.stdout)
    except ValueError:
        raise SystemExit('Bandit returned no valid report.') from None
    if report.get('errors'):
        raise SystemExit('Bandit reported source scanning errors.')
    reviewed = json.loads((ROOT / '.bandit-baseline.json').read_text())
    allowed = {(item['path'], item['test_id'], item['source_sha256'], item['severity'])
               for item in reviewed['findings']}
    unknown = []
    for finding in report['results']:
        path = Path(finding['filename'])
        if path.is_absolute():
            path = path.relative_to(ROOT)
        digest = hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
        key = (path.as_posix(), finding['test_id'], digest, finding['issue_severity'])
        if key not in allowed:
            unknown.append(f"{path}:{finding['line_number']} {finding['test_id']} {finding['issue_severity']}")
    if unknown:
        raise SystemExit('Unreviewed security findings:\n' + '\n'.join(unknown))
    print(f"Bandit: no unreviewed findings; {len(report['results'])} reviewed low-severity warnings.")


if __name__ == '__main__':
    main()
