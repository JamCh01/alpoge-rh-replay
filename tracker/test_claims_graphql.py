import io
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import claims_graphql
import grants


def comment(number, body, login='github-actions', kind='Bot'):
    author = {'login': login, '__typename': kind} if login else None
    return {'databaseId': number, 'body': body, 'author': author}


def comments(nodes=(), before=None):
    return {'nodes': list(nodes), 'pageInfo': {
        'startCursor': before, 'hasPreviousPage': before is not None,
    }}


def issue(number, title, labels=(), history=None, body=''):
    return {'id': f'I{number}', 'number': number, 'title': title, 'body': body,
            'labels': {'nodes': [{'name': name} for name in labels]},
            'comments': comments() if history is None else history}


def page(nodes, after=None):
    return {'repository': {
        'claim': {'id': 'L1'}, 'granted': {'id': 'L2'}, 'conflict': {'id': 'L3'},
        'issues': {'nodes': nodes, 'pageInfo': {
            'endCursor': after, 'hasNextPage': after is not None,
        }},
    }}


class ClaimsGraphQLTests(unittest.TestCase):
    def limited(self, code=429, body=b'', headers=None):
        error = HTTPError('https://api.github.com/graphql', code, 'rejected',
                          {'Retry-After': '2'} if headers is None else headers, io.BytesIO(body))
        self.addCleanup(error.close)
        return error

    def test_rate_limited_issue_page_retries_without_restarting_snapshot(self):
        transport = claims_graphql.ClaimsGraphQL('r/p', 'unused-test-token')
        with patch.object(transport, 'query', side_effect=[
                page([issue(1, 'Claim M000001')], after='issue-1'),
                self.limited(), page([issue(2, 'Claim M000002')]),
        ]) as query, patch('time.sleep') as sleep:
            transport.snapshot()
        self.assertEqual([call.kwargs['after'] for call in query.call_args_list],
                         [None, 'issue-1', 'issue-1'])
        self.assertEqual(set(transport.issues), {1, 2})
        sleep.assert_called_once_with(2)

    def test_rate_limited_history_page_keeps_cursor_and_grant(self):
        transport = claims_graphql.ClaimsGraphQL('r/p', 'unused-test-token')
        claim = issue(1, 'Claim M000001', ('claim', 'claim-granted'),
                      comments([comment(2, 'Recent comment', 'owner', 'User')], before='comment-2'))
        with patch.object(transport, 'query', side_effect=[
                page([claim]), self.limited(),
                {'node': {'comments': comments([comment(1, 'Granted `M000001`.')])}},
        ]) as query, patch('time.sleep') as sleep:
            transport.snapshot()
        self.assertEqual([call.kwargs['before'] for call in query.call_args_list[1:]],
                         ['comment-2', 'comment-2'])
        self.assertEqual([c['id'] for c in transport.comments], [1])
        self.assertEqual(transport.issues[1]['comments'], 2)
        sleep.assert_called_once_with(2)

    def test_read_permission_failure_does_not_retry(self):
        transport = claims_graphql.ClaimsGraphQL('r/p', 'unused-test-token')
        with patch.object(transport, 'query', side_effect=self.limited(
                403, b'permission denied', {})) as query, patch('time.sleep') as sleep:
            with self.assertRaises(HTTPError):
                transport.snapshot()
        self.assertEqual(query.call_count, 1)
        sleep.assert_not_called()

    def test_exhausted_reads_do_not_restart_snapshot_or_apply_mutations(self):
        errors = [self.limited() for _ in range(4)]
        planner = Mock()
        with patch.dict('os.environ', GITHUB_REPOSITORY='r/p', GITHUB_TOKEN='unused-test-token'), \
                patch('sys.argv', ['grants.py', '--graphql']), \
                patch.object(claims_graphql.ClaimsGraphQL, 'query', side_effect=errors) as query, \
                patch.object(claims_graphql.ClaimsGraphQL, 'apply') as apply, \
                patch.object(grants, 'reconcile', planner), patch('time.sleep') as sleep:
            with self.assertRaises(HTTPError):
                grants.main()
        self.assertEqual(query.call_count, 4)
        self.assertEqual(sleep.call_count, 3)
        planner.assert_not_called()
        apply.assert_not_called()

    def test_rate_limited_mutation_rechecks_snapshot_before_replanning(self):
        events = []
        writes = [self.limited(), None]

        def snapshot():
            events.append('snapshot')

        def planner(**kwargs):
            events.append('plan')

        def apply():
            events.append('apply')
            error = writes.pop(0)
            if error:
                raise error

        with patch.dict('os.environ', GITHUB_REPOSITORY='r/p', GITHUB_TOKEN='unused-test-token'), \
                patch.object(claims_graphql.ClaimsGraphQL, 'snapshot', side_effect=snapshot), \
                patch.object(claims_graphql.ClaimsGraphQL, 'apply', side_effect=apply), \
                patch('time.sleep') as sleep:
            claims_graphql.reconcile(planner)
        self.assertEqual(events, ['snapshot', 'plan', 'apply', 'snapshot', 'plan', 'apply'])
        sleep.assert_called_once_with(2)

    def test_hundred_issue_pages_preserve_grants_from_full_comment_history(self):
        # The grant is at the end of a full issue page, followed by an
        # overlapping new request on the next page. Its trusted bot history
        # spans two older comment pages; recent comments cannot forge a grant.
        packet = issue(100, 'Claim P0001', ('claim', 'claim-granted'), comments([
            comment(300, 'Granted `P9999`.', 'owner', 'User'),
            comment(301, 'Granted `P9999`.', 'dependabot', 'Bot'),
            comment(302, 'Granted `P9999`.', None),
        ], before='comment-200'))
        first = [issue(n, 'Unrelated issue') for n in range(1, 100)] + [packet]
        second = [issue(101, 'Claim M000001')]
        transport = claims_graphql.ClaimsGraphQL('r/p', 'unused-test-token')
        requests = []

        def query(text, **variables):
            requests.append((text, variables))
            if 'issues(states:OPEN' in text:
                self.assertIn('issues(states:OPEN, first:100, after:$after)', text)
                self.assertIn('comments(last:5)', text)
                if variables['after'] is None:
                    return page(first, after='issue-100')
                self.assertEqual(variables['after'], 'issue-100')
                return page(second)
            self.assertIn('comments(last:100, before:$before)', text)
            self.assertEqual(variables['id'], 'I100')
            if variables['before'] == 'comment-200':
                history = comments([
                    comment(200, 'Granted `P0001`.', 'github-actions[bot]'),
                    comment(250, 'No grant.', 'owner', 'User'),
                ], before='comment-100')
            else:
                self.assertEqual(variables['before'], 'comment-100')
                history = comments([comment(100, 'Granted `P9999`.')])
            return {'node': {'comments': history}}

        with patch.object(transport, 'query', side_effect=query):
            transport.snapshot()

        self.assertEqual(set(transport.issues), {100, 101})
        self.assertEqual(transport.issues[100]['comments'], 6)
        self.assertEqual([c['id'] for c in transport.comments], [200, 100])
        self.assertTrue(all(c['user']['login'] == 'github-actions[bot]'
                            for c in transport.comments))
        self.assertEqual([v['after'] for q, v in requests if 'issues(states:OPEN' in q],
                         [None, 'issue-100'])
        self.assertEqual([v['before'] for q, v in requests if 'comments(last:100' in q],
                         ['comment-200', 'comment-100'])

        writes = []
        # Already-recorded build evidence must not revoke an existing grant.
        rows = [{'id': 'M000001', 'packet': 'P0001', 'state': 'build-observed'}]
        with patch.object(grants, 'load', return_value=rows):
            grants.reconcile(get_pages=transport.pages,
                             mutate=lambda *args: writes.append(args))
        # A new claim on completed work is refused and closed.
        self.assertEqual([w[0] for w in writes],
                         ['/issues/101/labels', '/issues/101/comments', '/issues/101'])
        self.assertEqual(writes[0][2], {'labels': ['claim', 'claim-conflict']})

    def test_short_final_page_keeps_renamed_labeled_claims(self):
        renamed = issue(101, 'Edited claim title', ('claim', 'claim-granted'))
        transport = claims_graphql.ClaimsGraphQL('r/p', 'unused-test-token')
        with patch.object(transport, 'query', side_effect=[
                page([issue(n, 'Unrelated issue') for n in range(1, 101)], after='issue-100'),
                page([renamed]),
        ]) as query:
            transport.snapshot()
        self.assertEqual(query.call_count, 2)
        self.assertEqual(query.call_args.kwargs['after'], 'issue-100')
        self.assertEqual(list(transport.issues), [101])
        self.assertEqual(transport.comments, [])


if __name__ == '__main__':
    unittest.main()
