"""Tests for packet remainder (R####) and custom module batch (B<12hex>) claims."""

import gzip
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import activity
import claim_timing
import claims_graphql
import grants
import publish
import submissions
import tasks

ROOT = Path(__file__).resolve().parent.parent
VALIDATOR = ROOT / 'tracker' / 'validate_tracker.py'


def make_issue(number, title, login='alice', labels=(), body='', count=1):
    return {
        'number': number,
        'url': f'https://api.github.com/repos/r/p/issues/{number}',
        'title': title,
        'body': body,
        'user': {'login': login, 'type': 'User', 'id': 100 + number},
        'state': 'open',
        'comments': count,
        'labels': [{'name': name} for name in labels],
    }


def make_comment(issue_obj, cid, body, login='github-actions[bot]', created_at='2026-11-22T12:00:00Z'):
    return {
        'id': cid,
        'issue_url': issue_obj['url'],
        'body': body,
        'user': {'login': login},
        'created_at': created_at,
        'html_url': f"https://github.com/r/p/issues/{issue_obj['number']}#issuecomment-{cid}",
    }


def make_tar(path, members):
    with tarfile.open(path, 'w:gz') as tf:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))


class RemainderAndBatchGrantTests(unittest.TestCase):
    def setUp(self):
        self.rows = [
            {'id': 'M000001', 'packet': 'P0001', 'module': 'A.M1', 'state': 'volunteer-build-reviewed', 'depth': 0, 'imports': []},
            {'id': 'M000002', 'packet': 'P0001', 'module': 'A.M2', 'state': 'open', 'depth': 0, 'imports': []},
            {'id': 'M000003', 'packet': 'P0001', 'module': 'A.M3', 'state': 'open', 'depth': 0, 'imports': []},
            {'id': 'M000004', 'packet': 'P0002', 'module': 'B.M4', 'state': 'open', 'depth': 0, 'imports': []},
            {'id': 'M000005', 'packet': 'P0003', 'module': 'C.M5', 'state': 'assigned', 'depth': 0, 'imports': []},
            {'id': 'M000006', 'packet': 'P0003', 'module': 'C.M6', 'state': 'open', 'depth': 0, 'imports': []},
        ]

    def run_reconcile(self, issues, comments=(), rows=None):
        writes = []
        reads = {
            '/issues?state=open&per_page=100&page=1': list(issues),
            '/' + grants.COMMENT_FEED + '&page=1': list(comments),
        }

        def fake_api(route, method='GET', body=None):
            if method == 'GET':
                return reads.get(route, [])
            writes.append((route, method, body))

        with patch.object(grants, 'load', return_value=self.rows if rows is None else rows), \
             patch.object(grants, 'load_paused', return_value=set()):
            grants.reconcile(mutate=fake_api, get_pages=lambda r: iter(reads.get(r + '&page=1', [])))
        return writes

    def test_remainder_claim_grants_open_modules_and_blocks_overlap(self):
        rem = make_issue(10, 'Claim R0001', login='alice', count=0)
        later_mod = make_issue(11, 'Claim M000002', login='bob', count=0)
        assigned_rem = make_issue(12, 'Claim R0003', login='carol', count=0)
        writes = self.run_reconcile([rem, later_mod, assigned_rem])

        by_route = {(r, m): b for r, m, b in writes}
        self.assertEqual(by_route[('/issues/10/labels', 'POST')]['labels'], ['claim', 'claim-granted'])
        grant_body = by_route[('/issues/10/comments', 'POST')]['body']
        self.assertTrue(grant_body.startswith('Granted `R0001`. Modules (2): M000002 M000003.'))
        self.assertEqual(tasks.parse_granted_modules(grant_body, 'R0001'), ('M000002', 'M000003'))

        # Overlapping M000002 and assigned-packet R0003 are both refused.
        self.assertEqual(by_route[('/issues/11/labels', 'POST')]['labels'], ['claim', 'claim-conflict'])
        self.assertEqual(by_route[('/issues/12/labels', 'POST')]['labels'], ['claim', 'claim-conflict'])

    def test_batch_claim_verifies_digest_and_pins_granted_modules_against_body_edits(self):
        bid = tasks.batch_claim_id(['M000004', 'M000002'])
        valid = make_issue(20, f'Claim {bid}', login='alice', body='Working.\n\nModules: M000004 M000002\n', count=0)
        bad_hash = make_issue(21, 'Claim B000000000000', login='bob', body='Modules: M000003\n', count=0)
        writes = self.run_reconcile([valid, bad_hash])
        by_route = {(r, m): b for r, m, b in writes}
        self.assertEqual(by_route[('/issues/20/labels', 'POST')]['labels'], ['claim', 'claim-granted'])
        grant_body = by_route[('/issues/20/comments', 'POST')]['body']
        self.assertEqual(tasks.parse_granted_modules(grant_body, bid), ('M000002', 'M000004'))
        self.assertEqual(by_route[('/issues/21/labels', 'POST')]['labels'], ['claim', 'claim-conflict'])

        # Simulate hostile issue body edit AFTER grant: bot's pinned comment still governs!
        granted_issue = make_issue(20, f'Claim {bid}', login='alice',
                                   labels=('claim', 'claim-granted'),
                                   body='Modules: M000003\n', count=1)
        bot_comment = make_comment(granted_issue, 99, grant_body)
        overlap_m3 = make_issue(22, 'Claim M000003', login='carol', count=0)
        overlap_m2 = make_issue(23, 'Claim M000002', login='dave', count=0)
        writes2 = self.run_reconcile([granted_issue, overlap_m3, overlap_m2], comments=[bot_comment])
        by_route2 = {(r, m): b for r, m, b in writes2}
        # M000003 was NOT in the pinned grant so carol wins it; M000002 WAS pinned so dave conflicts.
        self.assertEqual(by_route2[('/issues/22/labels', 'POST')]['labels'], ['claim', 'claim-granted'])
        self.assertEqual(by_route2[('/issues/23/labels', 'POST')]['labels'], ['claim', 'claim-conflict'])

    def test_per_user_open_multi_module_claim_cap(self):
        rows = [
            {'id': f'M{i:06d}', 'packet': f'P{i:04d}', 'module': f'Mod.M{i}', 'state': 'open', 'depth': 0, 'imports': []}
            for i in range(1, 12)
        ]
        issues = [make_issue(i, f'Claim R{i:04d}', login='alice', count=0) for i in range(1, 10)]
        writes = self.run_reconcile(issues, rows=rows)
        by_route = {(r, m): b for r, m, b in writes}
        for i in range(1, 9):
            self.assertEqual(by_route[(f'/issues/{i}/labels', 'POST')]['labels'], ['claim', 'claim-granted'])
        self.assertEqual(by_route[('/issues/9/labels', 'POST')]['labels'], ['claim', 'claim-conflict'])
        self.assertIn('already holds 8 open multi-module claims', by_route[('/issues/9/comments', 'POST')]['body'])


class GraphQLAndPipelineIntegrationTests(unittest.TestCase):
    def test_graphql_snapshot_reads_issue_body_for_batch_claims(self):
        bid = tasks.batch_claim_id(['M000001', 'M000002'])
        node = {
            'id': 'I50', 'number': 50, 'title': f'Claim {bid}',
            'body': 'Modules: M000001 M000002',
            'author': {'login': 'alice'},
            'labels': {'nodes': []},
            'comments': {'nodes': [], 'pageInfo': {'startCursor': None, 'hasPreviousPage': False}},
        }
        page = {'repository': {
            'claim': {'id': 'L1'}, 'granted': {'id': 'L2'}, 'conflict': {'id': 'L3'},
            'issues': {'nodes': [node], 'pageInfo': {'endCursor': None, 'hasNextPage': False}},
        }}
        transport = claims_graphql.ClaimsGraphQL('r/p', 'tok')
        with patch.object(transport, 'query', return_value=page):
            transport.snapshot()
        self.assertEqual(transport.issues[50]['body'], 'Modules: M000001 M000002')

    def test_tasks_cli_claim_and_select_for_remainder_and_batch(self):
        rows = [
            {'id': 'M000001', 'packet': 'P0001', 'module': 'A.One', 'state': 'build-observed', 'depth': 0, 'imports': []},
            {'id': 'M000002', 'packet': 'P0001', 'module': 'A.Two', 'state': 'open', 'depth': 0, 'imports': []},
            {'id': 'M000003', 'packet': 'P0002', 'module': 'B.Three', 'state': 'open', 'depth': 0, 'imports': []},
        ]
        with patch.object(tasks, 'completed_ids', return_value=set()):
            rem_sel = tasks.select(rows, 'R0001')
            self.assertEqual([r['id'] for r in rem_sel], ['M000002'])
            batch_sel = tasks.select(rows, 'M000003,M000002')
            self.assertEqual([r['id'] for r in batch_sel], ['M000002', 'M000003'])

        bid = tasks.batch_claim_id(['M000002', 'M000003'])
        with patch.object(tasks, 'load', return_value=rows), \
             patch.object(tasks, 'completed_ids', return_value=set()), \
             patch.object(tasks, 'claims', return_value=set()), \
             patch('sys.argv', ['tasks.py', 'claim', 'M000003,M000002']), \
             patch.object(tasks.subprocess, 'run') as create:
            self.assertEqual(tasks.main(), 0)
            cmd = create.call_args.args[0]
            self.assertEqual(cmd[cmd.index('--title') + 1], f'Claim {bid}')
            self.assertIn('Modules: M000002 M000003', cmd[cmd.index('--body') + 1])

    def test_publish_submissions_activity_and_timing_support_remainder_and_batch(self):
        bid = tasks.batch_claim_id(['M000002', 'M000004'])
        grant_text = tasks.format_grant_comment(bid, ['M000002', 'M000004'])
        iss = make_issue(77, f'Claim {bid}', login='alice', labels=('claim', 'claim-granted'))
        c_grant = make_comment(iss, 1, grant_text)
        sub_body = ('Replay submission\n\n'
                    'Receipt: https://raw.githubusercontent.com/a/b/main/receipt.json\n'
                    'Log: https://raw.githubusercontent.com/a/b/main/build.log\n'
                    'Bundle: https://raw.githubusercontent.com/a/b/main/targets.tar.gz')
        c_sub = make_comment(iss, 2, sub_body, login='alice', created_at='2026-11-22T12:10:00Z')

        # submissions.volunteer_updates
        with patch.object(submissions, 'ROOT') as root:
            root.__truediv__.return_value.glob.return_value = []
            updates = list(submissions.volunteer_updates('owner/repo', {}, ([iss], {77: [c_grant, c_sub]})))
            self.assertEqual(updates, [(77, 'alice', bid, 2)])

        # activity + claim_timing
        ev = activity.activity(activity.activity_issues(([iss], {77: [c_grant, c_sub]})), 'owner', set())
        self.assertEqual([e[1] for e in ev], ['grant', 'volunteer', 'submission'])
        obs = list(claim_timing.observations([iss], {77: [c_grant, c_sub]}, 'owner', '2026-11-22T13:00:00Z'))
        self.assertEqual(len(obs), 1)
        self.assertEqual(obs[0][1], bid)
        self.assertEqual(obs[0][3], 'submitted')

        # publish.publish_candidate + recheck_record
        rows = [
            {'id': 'M000001', 'packet': 'P0001', 'module': 'A.One', 'state': 'open', 'depth': 0, 'imports': []},
            {'id': 'M000002', 'packet': 'P0001', 'module': 'A.Two', 'state': 'open', 'depth': 0, 'imports': []},
            {'id': 'M000004', 'packet': 'P0002', 'module': 'B.Four', 'state': 'open', 'depth': 0, 'imports': []},
        ]
        with tempfile.TemporaryDirectory() as td:
            bundle = Path(td) / 'b.tar.gz'
            make_tar(bundle, [('lib/lean/A/Two.olean', b'olean')])
            receipt_bytes = json.dumps({
                'tasks': ['M000002', 'M000004'], 'exit_code': 0,
                'bundle_sha256': 'ab' * 32, 'toolchain': 'Lean (version 4.33.1)',
            }).encode()
            payloads = {'receipt.json': receipt_bytes, 'build.log': b'ok\n', 'targets.tar.gz': bundle.read_bytes()}

            class Resp:
                def __init__(self, d): self._io = io.BytesIO(d)
                def read1(self, n=-1): return self._io.read(n)
                def __enter__(self): return self
                def __exit__(self, *a): return False

            ctx = {
                'issues': {77: iss},
                'histories': {77: [c_grant, c_sub]},
                'rows': rows,
                'rows_by_module': {r['module']: r for r in rows},
                'credited': {'M000001': ['M000001', 'P0001', 'A.One', 'volunteer-build-reviewed']},
                'run_url': None,
                'verify_budget': {'used': 0.0, 'cap': 5400.0},
                'verify_by_login': {},
            }
            with patch.object(publish, 'ROOT', Path(td)), \
                 patch.object(publish, '_OPENER', type('O', (), {'open': lambda s, req, timeout=None: Resp(payloads[req.full_url.rsplit('/', 1)[-1]])})()), \
                 patch.object(publish.replay_evidence, 'verify', return_value={'kernel_replayed_targets': [], 'built_targets': ['A.Two', 'B.Four']}):
                text, tgts, reason = publish.publish_candidate((77, 'alice', bid, 2), ctx)
                self.assertIsNotNone(text, reason)
                self.assertEqual([r['id'] for r in tgts], ['M000002', 'M000004'])
                self.assertTrue(publish.recheck_record(iss, [c_grant, c_sub], bid, 2, 'alice', sub_body, ('M000002', 'M000004')))
                self.assertFalse(publish.recheck_record(iss, [c_grant, c_sub], bid, 2, 'alice', sub_body, ('M000002',)))


class ValidateTrackerRemainderAndBatchTests(unittest.TestCase):
    def test_validator_accepts_valid_remainder_and_batch_and_rejects_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / 'repo'
            shutil.copytree(ROOT, copy, ignore=shutil.ignore_patterns('.git', '__pycache__'))

            with gzip.open(copy / 'tracker/modules.jsonl.gz', 'rt') as f:
                rows = [json.loads(line) for line in f if line.strip()]
            comp_lines = (copy / 'tracker/completed.tsv').read_text().splitlines()
            comp_header, comp_body = comp_lines[0], comp_lines[1:]
            credited_ids = {ln.split('\t', 1)[0] for ln in comp_body}

            # Find two genuinely open uncredited modules for a B<12hex> receipt test
            open_mods = [r for r in rows if r['state'] == 'open' and r['id'] not in credited_ids][:2]
            self.assertEqual(len(open_mods), 2)
            b_ids = sorted(r['id'] for r in open_mods)
            bid = tasks.batch_claim_id(b_ids)
            summary = json.loads((copy / 'tracker/summary.json').read_text())

            b_receipt = {
                'packet': bid,
                'contributor': 'alice',
                'claim': 'https://github.com/r/p/issues/1#issuecomment-1',
                'tasks': b_ids,
                'exit_code': 0,
                'mathlib': summary['mathlib_commit'],
                'objects': {m: 'a' * 64 for m in b_ids},
            }
            b_path = copy / 'evidence/volunteers' / f'{bid}.json'
            b_path.write_text(json.dumps(b_receipt))

            # Add corresponding rows to completed.tsv sorted by id
            by_id = {r['id']: r for r in rows}
            new_rows = list(comp_body) + [
                f"{m}\t{by_id[m]['packet']}\t{by_id[m]['module']}\tvolunteer-build-automated"
                for m in b_ids
            ]
            new_rows.sort(key=lambda ln: ln.split('\t', 1)[0])
            (copy / 'tracker/completed.tsv').write_text('\n'.join([comp_header, *new_rows]) + '\n')

            ok_res = subprocess.run([sys.executable, str(VALIDATOR), str(copy)], capture_output=True, text=True)
            self.assertEqual(ok_res.returncode, 0, ok_res.stdout + ok_res.stderr)

            # Also find a partial packet with open modules and no assigned modules to test R####
            by_pkt = {}
            for r in rows:
                by_pkt.setdefault(r['packet'], []).append(r)
            cur_credited = credited_ids | set(b_ids)
            rem_pkt, rem_open = None, None
            for pkt, members in by_pkt.items():
                if any(m['state'] == 'assigned' for m in members):
                    continue
                op = [m['id'] for m in members if m['state'] == 'open' and m['id'] not in cur_credited]
                if 1 < len(op) < len(members):
                    rem_pkt, rem_open = pkt, sorted(op)
                    break
            self.assertIsNotNone(rem_pkt)
            rid = 'R' + rem_pkt[1:]
            r_receipt = {
                'packet': rid,
                'contributor': 'bob',
                'claim': 'https://github.com/r/p/issues/2#issuecomment-2',
                'tasks': rem_open,
                'exit_code': 0,
                'mathlib': summary['mathlib_commit'],
                'objects': {m: 'b' * 64 for m in rem_open},
            }
            r_path = copy / 'evidence/volunteers' / f'{rid}.json'
            r_path.write_text(json.dumps(r_receipt))
            new_rows.extend(
                f"{m}\t{by_id[m]['packet']}\t{by_id[m]['module']}\tvolunteer-build-automated"
                for m in rem_open
            )
            new_rows.sort(key=lambda ln: ln.split('\t', 1)[0])
            (copy / 'tracker/completed.tsv').write_text('\n'.join([comp_header, *new_rows]) + '\n')

            ok_res2 = subprocess.run([sys.executable, str(VALIDATOR), str(copy)], capture_output=True, text=True)
            self.assertEqual(ok_res2.returncode, 0, ok_res2.stdout + ok_res2.stderr)

            # Tamper with R#### by dropping one task from R####.json -> must fail completeness check!
            r_receipt['tasks'] = rem_open[:-1]
            r_receipt['objects'] = {m: 'b' * 64 for m in rem_open[:-1]}
            r_path.write_text(json.dumps(r_receipt))
            bad_rem = subprocess.run([sys.executable, str(VALIDATOR), str(copy)], capture_output=True, text=True)
            self.assertNotEqual(bad_rem.returncode, 0)
            self.assertIn('FAIL evidence.reviewed_tasks_complete_claim', bad_rem.stdout)
            r_receipt['tasks'] = rem_open
            r_receipt['objects'] = {m: 'b' * 64 for m in rem_open}
            r_path.write_text(json.dumps(r_receipt))

            # Tamper with B<12hex> by dropping one task -> must fail completeness check!
            b_receipt['tasks'] = [b_ids[0]]
            b_receipt['objects'] = {b_ids[0]: 'a' * 64}
            b_path.write_text(json.dumps(b_receipt))
            bad_res = subprocess.run([sys.executable, str(VALIDATOR), str(copy)], capture_output=True, text=True)
            self.assertNotEqual(bad_res.returncode, 0)
            self.assertIn('FAIL evidence.reviewed_tasks_complete_claim', bad_res.stdout)


if __name__ == '__main__':
    unittest.main()
