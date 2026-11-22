"""Read claim history and apply bot decisions through GitHub GraphQL."""

import io
import json
import os
import re
import sys
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from github_rate_limit import ATTEMPTS, retry_rate_limited, wait_for_rate_limit
from tasks import CLAIM_TITLE_RE

LABELS = ('claim', 'claim-granted', 'claim-conflict')
GATEWAY_ATTEMPTS = 4
GATEWAY_DELAY = 20
COMMENT_FIELDS = 'databaseId body author { login __typename }'


class ClaimsGraphQL:
    def __init__(self, repo, token):
        self.owner, self.repo = repo.split('/')
        self.token = token
        self.issues = {}
        self.comments = []
        self.labels = {}
        self.writes = []

    def query(self, query, **variables):
        request = Request('https://api.github.com/graphql',
                          headers={'Authorization': 'Bearer ' + self.token,
                                   'Content-Type': 'application/json', 'User-Agent': 'replay-claims'},
                          data=json.dumps({'query': query, 'variables': variables}).encode())
        with urlopen(request, timeout=45) as response:
            result = json.load(response)
            if errors := result.get('errors'):
                body = json.dumps(errors).encode()
                if b'rate limit' in body.lower() or b'RATE_LIMITED' in body:
                    raise HTTPError(request.full_url, 429, 'GitHub GraphQL rate limit',
                                    response.headers, io.BytesIO(body))
                raise RuntimeError('; '.join(error['message'] for error in errors))
            return result['data']

    def read_query(self, query, **variables):
        # Retry this page before advancing its cursor. No mutation is replayed here.
        # Large pages can time out at GitHub's gateway; a read is safe to repeat.
        for attempt in range(GATEWAY_ATTEMPTS):
            try:
                return retry_rate_limited(lambda: self.query(query, **variables), 'Retrying GraphQL read')
            except HTTPError as error:
                if error.code not in (502, 503, 504) or attempt == GATEWAY_ATTEMPTS - 1:
                    raise
                error.close()
                print(f'GitHub gateway error (HTTP {error.code}). Retrying GraphQL read.', file=sys.stderr, flush=True)
                time.sleep(GATEWAY_DELAY * (attempt + 1))

    def snapshot(self):
        started = time.monotonic()
        issue_pages = 0
        scanned_issues = 0
        query = '''
        query($owner:String!, $repo:String!, $after:String) {
          repository(owner:$owner, name:$repo) {
            claim: label(name:"claim") { id }
            granted: label(name:"claim-granted") { id }
            conflict: label(name:"claim-conflict") { id }
            issues(states:OPEN, first:100, after:$after) {
              pageInfo { endCursor hasNextPage }
              nodes { id number title body author { login } labels(first:5) { nodes { name } }
                      comments(last:5) { pageInfo { startCursor hasPreviousPage }
                                          nodes { %s } } }
            }
          }
        }''' % COMMENT_FIELDS
        after = None
        while True:
            data = self.read_query(query, owner=self.owner, repo=self.repo, after=after)['repository']
            self.labels = {name: data[key]['id'] for name, key in zip(LABELS, ('claim', 'granted', 'conflict'))}
            issue_page = data['issues']
            issue_pages += 1
            scanned_issues += len(issue_page['nodes'])
            for issue in issue_page['nodes']:
                labels = issue['labels']['nodes']
                if not (CLAIM_TITLE_RE.fullmatch(issue['title'])
                        or any(label['name'] in LABELS for label in labels)):
                    continue
                number = issue['number']
                url = f'https://api.github.com/repos/{self.owner}/{self.repo}/issues/{number}'
                history = list(self.history(issue))
                self.issues[number] = {**issue, 'labels': labels, 'url': url, 'comments': len(history),
                                       'user': {'login': (issue.get('author') or {}).get('login')}}
                self.comments.extend({
                    'id': comment['databaseId'], 'issue_url': url, 'body': comment['body'],
                    'user': {'login': 'github-actions[bot]'}
                } for comment in history
                    if comment.get('author') and comment['author']['__typename'] == 'Bot'
                    and comment['author']['login'] in ('github-actions', 'github-actions[bot]'))
            info = issue_page['pageInfo']
            if not info['hasNextPage']:
                break
            after = info['endCursor']
        elapsed = time.monotonic() - started
        print(f'Claim snapshot read {scanned_issues} open issues across {issue_pages} issue pages '
              f'in {elapsed:.1f}s, retaining {len(self.issues)} claims and {len(self.comments)} bot comments.',
              flush=True)

    def history(self, issue):
        page = issue['comments']
        while True:
            yield from page['nodes']
            info = page['pageInfo']
            if not info['hasPreviousPage']:
                return
            page = self.read_query('''
              query($id:ID!, $before:String) {
                node(id:$id) { ... on Issue {
                  comments(last:100, before:$before) {
                    pageInfo { startCursor hasPreviousPage }
                    nodes { %s }
                  }
                } }
              }''' % COMMENT_FIELDS, id=issue['id'], before=info['startCursor'])['node']['comments']

    def pages(self, route):
        if route.startswith('/issues/comments?'):
            return iter(self.comments)
        return iter(self.issues.values())

    def queue(self, route, method, body=None):
        closing = re.fullmatch(r'/issues/(\d+)', route)
        if closing and method == 'PATCH' and body == {'state': 'closed', 'state_reason': 'not_planned'}:
            node = json.dumps(self.issues[int(closing[1])]['id'])
            self.writes.append('closeIssue(input:{issueId:%s,stateReason:NOT_PLANNED}){clientMutationId}' % node)
            return
        match = re.fullmatch(r'/issues/(\d+)/(labels|comments)(?:/([a-z-]+))?', route)
        number, path, label = match.groups()
        node = json.dumps(self.issues[int(number)]['id'])
        if path == 'comments':
            mutation = 'addComment(input:{subjectId:%s,body:%s}){clientMutationId}' % (node, json.dumps(body['body']))
        elif method == 'DELETE':
            mutation = 'removeLabelsFromLabelable(input:{labelableId:%s,labelIds:[%s]}){clientMutationId}' % (
                node, json.dumps(self.labels[label]))
        else:
            ids = ','.join(json.dumps(self.labels[name]) for name in body['labels'])
            mutation = 'addLabelsToLabelable(input:{labelableId:%s,labelIds:[%s]}){clientMutationId}' % (node, ids)
        self.writes.append(mutation)

    def apply(self):
        started = time.monotonic()
        changes_in_successful_batches = 0
        try:
            for offset in range(0, len(self.writes), 8):
                batch = self.writes[offset:offset+8]
                self.query('mutation{' + '\n'.join(f'm{i}:{item}' for i, item in enumerate(batch)) + '}')
                changes_in_successful_batches += len(batch)
        finally:
            elapsed = time.monotonic() - started
            print(f'Claim mutation batches succeeded for {changes_in_successful_batches} of '
                  f'{len(self.writes)} queued changes in {elapsed:.1f}s.', flush=True)
        print(f'Reconciled {len(self.issues)} claims with {len(self.writes)} changes.', flush=True)


def reconcile(planner):
    for attempt in range(ATTEMPTS):
        transport = ClaimsGraphQL(os.environ['GITHUB_REPOSITORY'], os.environ['GITHUB_TOKEN'])
        transport.snapshot()
        planner(get_pages=transport.pages, mutate=transport.queue)
        try:
            transport.apply()
            return
        except HTTPError as error:
            # A mutation may have succeeded before its response failed. Re-read
            # all grants and replan rather than blindly repeating that mutation.
            if error.code in (502, 503, 504) and attempt < ATTEMPTS - 1:
                error.close()
                print(f'GitHub gateway error (HTTP {error.code}) while applying changes. Rechecking all claims.',
                      file=sys.stderr, flush=True)
                time.sleep(GATEWAY_DELAY * (attempt + 1))
                continue
            wait_for_rate_limit(error, attempt, 'Rechecking all claims')
