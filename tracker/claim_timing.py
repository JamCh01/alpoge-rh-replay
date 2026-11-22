"""Report volunteer reservation timing from GitHub issue and comment timestamps."""

import csv
import json
from datetime import datetime, timezone
import os
import re
import sys

from submissions import ROOT, claim_snapshot
from tasks import CLAIM_TITLE_RE


def seconds(start, end):
    def parse(stamp):
        return datetime.fromisoformat(stamp.replace('Z', '+00:00'))
    return int((parse(end) - parse(start)).total_seconds())


def observations(issues, histories, owner, observed_at, reviewed_comments=()):
    for issue in issues:
        author = issue.get('user') or {}
        login = author.get('login', '')
        match = CLAIM_TITLE_RE.fullmatch(issue['title'])
        if not match or author.get('type') != 'User' or not login or login.casefold() == owner.casefold():
            continue
        target = match[1]
        granted = submitted = latest = None
        longest_gap = 0
        for comment in sorted(histories.get(issue['number'], []), key=lambda c: c['id']):
            user = comment.get('user') or {}
            body = comment['body']
            when = comment['created_at']
            if user.get('login') == 'github-actions[bot]':
                if body.startswith(f'Granted `{target}`.'):
                    if granted is None:
                        granted = latest = when
                elif body.startswith(('No grant.', 'Granted `')):
                    granted = submitted = latest = None
                    longest_gap = 0
            elif granted and not submitted and user.get('login', '').casefold() == login.casefold():
                longest_gap = max(longest_gap, seconds(latest, when))
                latest = when
                first_line = body.lstrip('\ufeff \t\r\n').partition('\n')[0].strip().casefold()
                if first_line == 'replay submission' or comment.get('html_url') in reviewed_comments:
                    submitted = when
                    break  # Later bot decisions cannot erase a completed reservation.
        if not granted:
            continue
        if submitted:
            state, end = 'submitted', submitted
        elif issue['state'] == 'closed':
            state, end = 'closed_without_submission', issue['closed_at']
        elif any(label['name'] == 'claim-granted' for label in issue['labels']):
            state, end = 'open', observed_at
        else:
            continue
        # Issue state and comments are paginated separately. Defer inconsistent
        # rows to the next snapshot rather than emitting a negative interval.
        if latest > end:
            continue
        tail = seconds(latest, end)
        yield (issue['number'], target, login, state, granted, end,
               seconds(granted, end), max(longest_gap, tail))


def report(repo, output):
    issues, histories = claim_snapshot(repo)
    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    writer = csv.writer(output, delimiter='\t', lineterminator='\n')
    writer.writerow(('issue', 'target', 'claimant', 'state', 'granted_at', 'observed_until',
                     'elapsed_seconds', 'longest_claim_thread_silence_seconds'))
    reviewed = {json.loads(path.read_text())['claim'] for path in (ROOT / 'evidence/volunteers').glob('*.json')}
    writer.writerows(observations(issues, histories, repo.split('/')[0], now, reviewed))


if __name__ == '__main__':
    report(os.environ['GITHUB_REPOSITORY'], sys.stdout)
