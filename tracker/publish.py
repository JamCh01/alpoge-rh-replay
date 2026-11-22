"""Publish verified volunteer replay submissions to the ledger.

This is the automated form of the maintainers' manual publish sweep.  It
finds the same pending submissions as tracker/submissions.py (by importing
it, not by reimplementing its rules), verifies each one with
tracker/replay_evidence.py -- the same checks a reviewer runs by hand --
and, for the submissions that pass every check, writes the artifacts a
manual batch writes:

  evidence/volunteers/<TASK>.json   the receipt plus review metadata
  tracker/completed.tsv             one row per credited module

The completed.tsv rows carry the evidence value
'volunteer-build-automated', not the manual sweep's
'volunteer-build-reviewed': the checks are the same, but no maintainer
reviewed these submissions one by one, and the distinct value (added to
validate_tracker's KNOWN_EVIDENCE in this change) lets anyone filter --
or, if a fabrication ever turns up, revert -- bot-published credit
independently of hand-reviewed credit.

A submission is published only when all of the following hold:

  * submissions.py reports it as a pending submission (a comment whose
    first line is 'Replay submission', written by the claim issue's own
    author, for a task with no reviewed evidence yet), and the claimant
    account still exists (a deleted account's issues and comments are
    reassigned to 'ghost', which is never credited);
  * the claim grant is still in force: the issue is open, carries the
    'claim-granted' label and the bot's 'Granted `<TASK>`.' comment, and
    has no 'claim-conflict' label (the criteria tracker/worker.py's
    inspect_grant composes from the same signals).  Immediately
    before writing, the whole grant state is re-checked against a
    fresh claim snapshot, the way tracker/worker.py re-fetches state
    before it writes;
  * no evidence file for the task exists yet, every module's snapshot
    state is 'open', and none of the task's modules already appear in
    tracker/completed.tsv (a partially credited packet, or one tangled
    with another evidence source, stays with the manual sweep);
  * the comment links a downloadable receipt.json, build.log and bundle
    on an allowlisted host, and replay_evidence.verify() accepts them:
    pinned toolchain and inputs, exact target list, exit status 0, a
    matching successful build transcript, the bundle digest, and every
    bundled .olean digest.  Cached targets additionally need their
    kernel-replay.json.

Anything else is skipped with a recorded reason and left untouched for
the manual sweep -- most notably receipts without a bundle, which the
manual review checks against the prepared checkout (see
replay_evidence.verify's checkout parameter) and which therefore stay
manual.  A candidate that fails, however badly, never fails the run:
its reason is recorded and the sweep moves on.  Failures before the
candidate loop (losing the GitHub snapshot, an unreadable or malformed
completed.tsv) fail the run before anything is written, and all files
are written only after every candidate has been processed and the
grants re-checked, so an interrupted run leaves the tree exactly as it
found it.

Submission comments and artifacts are untrusted input to a job holding
a write token, so the sweep also bounds what a hostile claimant can
make it do: downloads are confined to GitHub's own hosts over https,
each file has a size cap and a wall-clock deadline, each candidate has
a total download budget, verification runs in a subprocess under CPU,
memory and wall-clock limits, the receipt fields the verifier does not
read are shape-checked before they are copied into the ledger, and a
published record larger than a fixed cap is refused.  The run prints a
summary of what was published and what was skipped, and optionally
writes the batch commit message to a file for the calling workflow.
The message adapts the manual batches' subject line --
'Publish N automated volunteer replay submissions' -- so bot batches
are distinguishable in the log as well as in the ledger.
"""

import argparse
import datetime
import gzip
import json
import math
import multiprocessing
import os
from pathlib import Path
import re
import resource
import sys
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request

import replay_evidence
import submissions
import tasks

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_VALUE = 'volunteer-build-automated'
# The manual sweep's wording for a bundle-backed review, minus the
# checkout-based comparisons (source files and installed objects on a
# prepared tree) that this automation does not perform: the sentence
# lists exactly the checks that ran.  The receipt's own pins --
# toolchain, Mathlib and the inputs manifest -- are still verified by
# replay_evidence.verify().  Bot-published records are additionally
# labelled by the 'automation' field below.
REVIEW_TEXT = ('Verified the grant, receipt pins and exact targets, and '
               'matching successful build transcript. Verified the supplied '
               'bundle checksum and every target .olean checksum.')
# The fields tracker/tasks.py's build writes into a receipt, in build
# order.  Anything else a submission's receipt carries is not copied
# into the ledger.
RECEIPT_FIELDS = ('tasks', 'started_utc', 'ended_utc', 'elapsed_seconds',
                  'host', 'toolchain', 'mathlib', 'inputs_manifest_sha256',
                  'command', 'exit_code', 'log_sha256',
                  'rebuilt_project_modules', 'objects', 'bundle_sha256')
# tasks.host_resources() writes exactly these keys, all integers.  The
# verifier never reads 'host', 'started_utc', 'ended_utc' or
# 'elapsed_seconds', so publish.py shape-checks them itself before
# copying: an unchecked nested or non-finite value there could inflate
# or corrupt the published record.
HOST_KEYS = {'cpu_count', 'mem_total_kib', 'peak_child_rss_kib'}
ARTIFACT_CAPS = {
    'receipt.json': 16 * 1024 * 1024,
    'build.log': 256 * 1024 * 1024,
    'targets.tar.gz': 1024 * 1024 * 1024,
    replay_evidence.REPLAY_FILE: 64 * 1024 * 1024,
}
# Downloads are confined to GitHub's own hosts: submission comments are
# untrusted input, and the sweep must not fetch arbitrary URLs from a
# job that holds a write token.  The set is the hosts real submissions
# actually use, checked against the submission census and live redirect
# behaviour: release downloads (the common case) redirect to
# release-assets.githubusercontent.com, and receipts are also posted as
# gists.  Anything hosted elsewhere stays with the manual sweep.
ALLOWED_HOSTS = {'github.com', 'www.github.com', 'raw.githubusercontent.com',
                 'objects.githubusercontent.com', 'media.githubusercontent.com',
                 'release-assets.githubusercontent.com',
                 'gist.githubusercontent.com'}
DOWNLOAD_DEADLINE = 600  # wall-clock seconds per file, however it drips
CANDIDATE_DOWNLOAD_CAP = 2 * 1024 * 1024 * 1024  # per candidate, all artifacts
BUNDLE_EXPANDED_CAP = 8 * 1024 * 1024 * 1024  # decompressed/logical bytes per bundle
BUNDLE_MEMBER_CAP = 200000
RECORD_SIZE_CAP = 1024 * 1024  # a real record is ~30 KB; refuse anything near push limits
VERIFY_CPU_SECONDS = 60
LOGIN_TIME_BUDGET = 900  # wall-clock seconds per claimant per run, downloads + verification
VERIFY_WALL_SECONDS = 180
VERIFY_MEM_BYTES = 10 * 1024 * 1024 * 1024
VERIFY_RUN_BUDGET = 5400  # wall-clock seconds of verification per run, all candidates
RUN_DEADLINE_SECONDS = 14400  # stop taking new candidates after 4h; write what is verified
URL_PATTERN = re.compile(r'https?://[^\s<>\[\]()"\'|`]+')


class _BudgetExceeded(Exception):
    pass


class _DeadlineExceeded(Exception):
    pass


class _AllowlistRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlparse(newurl)
        if parsed.scheme != 'https' or parsed.netloc not in ALLOWED_HOSTS:
            raise ValueError(f'redirect to a URL outside the allowlist: {newurl}')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_AllowlistRedirects)


def artifact_urls(body):
    """Map each downloadable artifact name to candidate URLs in a comment.

    Submission comments link their receipt, log and bundle in free form:
    release assets, raw URLs, or GitHub blob pages on a fork branch.
    Blob pages are rewritten to their raw form.  The rewrite splits the
    ref from the path at the first '/', so a branch name containing '/'
    produces a URL that 404s and the candidate is skipped -- the manual
    sweep remains free to fetch it by hand.  Duplicate URLs are dropped:
    a comment repeating one link a thousand times must not make the
    sweep download the same file a thousand times.
    """
    found = {}
    for match in URL_PATTERN.finditer(body or ''):
        url = match.group(0).rstrip('.,;:')
        name = urllib.parse.urlparse(url).path.rsplit('/', 1)[-1]
        if name not in ARTIFACT_CAPS:
            continue
        parsed = urllib.parse.urlparse(url)
        if parsed.netloc in ('github.com', 'www.github.com') and '/blob/' in parsed.path:
            repo_path, rest = parsed.path.split('/blob/', 1)
            ref, _, file_path = rest.partition('/')
            if not file_path:
                continue
            url = f'https://raw.githubusercontent.com{repo_path}/{ref}/{file_path}'
        candidates = found.setdefault(name, [])
        if url not in candidates:
            candidates.append(url)
    return found


def allowed(url):
    parsed = urllib.parse.urlparse(url)
    return parsed.scheme == 'https' and parsed.netloc in ALLOWED_HOSTS


def download(url, dest, limit, budget):
    """Download url to dest within a size limit and a wall-clock deadline.

    read1() returns after each socket read, so the deadline is checked
    continuously: a peer that drips bytes slowly cannot hold the
    download open past the deadline the way a block-filling read could.
    Every byte read -- including bytes of a download that ultimately
    fails -- counts against the candidate's shared budget, so repeated
    near-limit failures cannot multiply the sweep's bandwidth.
    A partial file is removed on failure.  Returns the byte count.
    """
    request = urllib.request.Request(url, headers={'User-Agent': 'rh-replay-publish'})
    started = time.monotonic()
    total = 0
    try:
        with _OPENER.open(request, timeout=120) as response, dest.open('wb') as out:
            while block := response.read1(256 * 1024):
                if time.monotonic() - started > DOWNLOAD_DEADLINE:
                    raise ValueError(f'{url} exceeded the {DOWNLOAD_DEADLINE}s download deadline')
                total += len(block)
                budget['used'] += len(block)
                if budget['used'] > budget['cap']:
                    raise _BudgetExceeded(f"candidate downloads exceeded the {budget['cap']}-byte budget")
                if total > limit:
                    raise ValueError(f'{url} exceeds the {limit}-byte download limit')
                out.write(block)
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    return total


def fetch_one(urls, directory, name, budget, deadline=None):
    """Download the first working allowlisted candidate for an artifact.

    At most three distinct allowlisted URLs are tried (filtering first,
    so off-allowlist mirrors cannot crowd out a usable link).  The
    run deadline is checked before every attempt, so a candidate
    cannot spend the job's remaining margin on downloads.
    """
    for url in [u for u in urls if allowed(u)][:3]:
        if deadline is not None and deadline():
            raise _DeadlineExceeded('run deadline reached during downloads')
        try:
            download(url, directory / name, ARTIFACT_CAPS[name], budget)
        except _BudgetExceeded:
            raise
        except Exception:
            continue
        return directory / name
    return None


class _CountingReader:
    """A stream wrapper that refuses to yield more than a capped byte count."""

    def __init__(self, stream, cap):
        self.stream = stream
        self.cap = cap
        self.total = 0

    def _count(self, block):
        self.total += len(block)
        if self.total > self.cap:
            raise ValueError(f'bundle expands past the {self.cap}-byte safety cap')
        return block

    def read(self, size=-1):
        return self._count(self.stream.read(size))

    def readline(self, size=-1):
        return self._count(self.stream.readline(size))


def check_bundle(path):
    """Bound the work replay_evidence.verify() will do on a bundle.

    Two different quantities need bounding.  The tar *stream* is walked
    behind a byte-counting reader, which caps the decompressed bytes
    verify() will hash for ordinary members.  But a sparse member's
    logical size (what extractfile() synthesises, mostly zeros) can be
    enormous while its stream footprint is a few hundred bytes, and a
    PAX header is buffered whole by tarfile -- so member sizes are also
    summed and capped, sparse members are refused outright (the build
    never produces them), and the whole verification runs under the
    subprocess resource limits in verify_candidate() regardless.
    """
    logical_total = 0
    with path.open('rb') as raw:
        # The counter wraps the gzip stream, so it measures decompressed
        # bytes -- wrapping the raw file would count compressed bytes and
        # a zip bomb would sail under the cap.
        reader = _CountingReader(gzip.GzipFile(fileobj=raw), BUNDLE_EXPANDED_CAP)
        with tarfile.open(fileobj=reader, mode='r|') as archive:
            members = 0
            for member in archive:
                members += 1
                if members > BUNDLE_MEMBER_CAP:
                    raise ValueError(f'bundle has more than {BUNDLE_MEMBER_CAP} members')
                if member.size < 0:
                    raise ValueError(f'bundle member has a negative size: {member.name[:200]}')
                if member.issparse():
                    raise ValueError(f'sparse bundle member is not accepted: {member.name[:200]}')
                logical_total += member.size
                if logical_total > BUNDLE_EXPANDED_CAP:
                    raise ValueError(f'bundle members exceed the {BUNDLE_EXPANDED_CAP}-byte logical size cap')


def _verify_worker(directory, targets, rows_by_module, conn):
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_AS, (VERIFY_MEM_BYTES, VERIFY_MEM_BYTES))
        resource.setrlimit(resource.RLIMIT_CPU, (VERIFY_CPU_SECONDS, VERIFY_CPU_SECONDS + 60))
        check_bundle(directory / 'targets.tar.gz')
        result = replay_evidence.verify(directory, targets, rows_by_module)
        conn.send(('ok', result))
    except BaseException as error:  # MemoryError and SystemExit included
        try:
            # Truncate before the pipe: an error message can carry an
            # attacker-sized tar member name, and the parent must never
            # receive (or sanitise) an unbounded string.
            conn.send(('error', f'{type(error).__name__}: {error}'[:500]))
        except Exception:
            pass
    finally:
        conn.close()


def verify_candidate(directory, targets, rows_by_module, budgets=()):
    """Run check_bundle + verify() in a resource-limited subprocess.

    Returns (result, None) or (None, reason).  The limits are the hard
    bound the in-process checks cannot give: a bundle that makes
    tarfile buffer a multi-GiB header, or synthesise a sparse file's
    zeros, burns the subprocess's CPU/memory budget and is skipped,
    instead of stalling or OOM-killing the sweep itself.  Elapsed time
    is charged to every budget in `budgets` (here the run-wide one), so
    a queue of hostile candidates cannot consume the whole job.  The
    per-claimant budget is charged separately, on total candidate wall
    time (downloads included), in publish_candidate's wrapper.
    """
    if any(budget['used'] >= budget['cap'] for budget in budgets):
        return None, 'verification time budget exhausted'
    started = time.monotonic()
    context = multiprocessing.get_context('fork')
    parent, child = context.Pipe(duplex=False)
    proc = context.Process(target=_verify_worker,
                           args=(directory, targets, rows_by_module, child))
    proc.start()
    child.close()
    try:
        if parent.poll(VERIFY_WALL_SECONDS):
            try:
                status, payload = parent.recv()
            except EOFError:
                return None, 'verification worker died before reporting (resource limit?)'
            if status == 'ok':
                return payload, None
            return None, f'evidence verification failed: {payload}'
        proc.terminate()
        proc.join(10)
        if proc.is_alive():
            proc.kill()
            proc.join()
        return None, f'verification exceeded the {VERIFY_WALL_SECONDS}s time budget'
    finally:
        parent.close()
        if proc.is_alive():
            proc.terminate()
            proc.join(10)
            if proc.is_alive():
                proc.kill()
        proc.join()
        for budget in budgets:
            budget['used'] += time.monotonic() - started


def _login(obj):
    return (obj.get('user') or {}).get('login')


def granted_modules(comments, target):
    """Extract the authoritative module tuple from the latest bot decision for R#### or B<12hex>."""
    ordered_comments = sorted(enumerate(comments), key=lambda item: (item[1].get('id', item[0]), item[0]))
    pinned = None
    prefix = f'Granted `{target}`.'
    for _, comment in ordered_comments:
        if _login(comment) != 'github-actions[bot]':
            continue
        body = comment.get('body') or ''
        if body.startswith(prefix):
            pinned = tasks.parse_granted_modules(body, target)
        elif body.startswith(('No grant.', 'Granted `')):
            pinned = None
    return pinned


def grant_in_force(issue, comments, target):
    """The worker's grant criteria (tracker/worker.py: inspect_grant)."""
    labels = {label['name'] for label in issue['labels']}
    if issue['state'] != 'open' or 'claim-granted' not in labels or 'claim-conflict' in labels:
        return False
    if target.startswith(('R', 'B')):
        return granted_modules(comments, target) is not None
    prefix = f'Granted `{target}`.'
    return any(_login(comment) == 'github-actions[bot]'
               and comment['body'].startswith(prefix) for comment in comments)


def recheck_record(issue, comments, target, comment_id, login, body=None, expected_ids=None):
    """Re-check one record against a fresh claim snapshot, before writing.

    Mirrors tracker/worker.py's habit of re-fetching live state before
    it writes.  The issue must still be the same open claim, by the
    same live author, with the grant still in force (grant_in_force,
    which includes the bot's 'Granted' comment still being present),
    and the submission comment itself must still exist, still belong
    to the claimant, still open with 'Replay submission', and -- when
    `body` (the text that was actually verified) is given -- be
    byte-identical to it, so an edit to the artifact links after
    verification cannot ride the verified record into the ledger (the
    edited comment is simply re-verified from scratch next run).  An
    issue that no longer appears in the snapshot -- for example
    because its 'claim' label was removed -- fails the re-check.
    """
    if issue is None or login == 'ghost':
        return False
    if (issue['title'] != f'Claim {target}' or _login(issue) != login
            or 'pull_request' in issue):
        return False
    if not grant_in_force(issue, comments, target):
        return False
    if expected_ids is not None and target.startswith(('R', 'B')):
        if granted_modules(comments, target) != tuple(expected_ids):
            return False
    for comment in comments:
        if int(comment['id']) == comment_id:
            lines = comment['body'].splitlines()
            return (_login(comment) == login and bool(lines)
                    and lines[0].strip().casefold() == 'replay submission'
                    and (body is None or comment['body'] == body))
    return False


def receipt_shape_error(receipt):
    """Validate the receipt fields the verifier does not read."""
    host = receipt.get('host')
    if host is not None:
        if not isinstance(host, dict) or not set(host) <= HOST_KEYS:
            return 'receipt host metadata has unexpected keys'
        # os.cpu_count() can return None, so a null value is legitimate.
        if any(not (value is None or type(value) is int) for value in host.values()):
            return 'receipt host metadata has non-integer values'
    for field in ('started_utc', 'ended_utc'):
        value = receipt.get(field)
        if value is not None and (not isinstance(value, str) or len(value) > 64):
            return f'receipt {field} is not a short timestamp string'
    toolchain = receipt.get('toolchain')
    if toolchain is not None and (not isinstance(toolchain, str) or len(toolchain) > 512):
        return 'receipt toolchain is not a short string'
    elapsed = receipt.get('elapsed_seconds')
    if elapsed is not None:
        if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)):
            return 'receipt elapsed_seconds is not a number'
        # Range first (int/float comparisons cannot overflow), then the
        # finiteness check that a huge int would break.
        if elapsed < 0 or elapsed > 31_536_000_000 or not math.isfinite(elapsed):
            return 'receipt elapsed_seconds is not finite and non-negative'
    return None


def _reject_constant(value):
    raise ValueError(f'non-standard JSON constant: {value}')


def load_completed(path):
    credited = {}
    if path.exists():
        raw = path.read_bytes()
        if b'\r' in raw:
            raise ValueError(f'{path} has CRLF line endings; refusing to rewrite it')
        lines = raw.decode().splitlines()
        if not lines or lines[0] != 'id\tpacket\tmodule\tevidence':
            raise ValueError(f'{path} does not start with the expected header')
        ids = []
        for line in lines[1:]:
            if not line:
                continue
            row = line.split('\t')
            if len(row) != 4:
                raise ValueError(f'malformed row in {path}: {line!r}')
            ids.append(row[0])
            credited[row[0]] = row
        if len(set(ids)) != len(ids):
            raise ValueError(f'duplicate ids in {path}')
        if ids != sorted(ids):
            raise ValueError(f'{path} is not sorted by id; refusing to rewrite it')
    return credited


def write_completed(path, credited):
    lines = ['id\tpacket\tmodule\tevidence']
    lines += ['\t'.join(credited[task_id]) for task_id in sorted(credited)]
    path.write_text('\n'.join(lines) + '\n')


def clean(text, limit=300):
    """Make untrusted text safe for logs and the Markdown run summary.

    Verification messages can carry attacker-controlled strings (tar
    member names, URLs); control characters in them could forge Actions
    workflow commands in the log or break the summary's formatting.
    """
    # Slice before filtering: the per-character pass materialises the
    # string, so it must never run over an unbounded input.
    text = str(text)[:4096]
    text = ''.join(c if c == ' ' or c.isprintable() else ' ' for c in text)
    return text[:limit]


def publish_candidate(candidate, context):
    """Verify one pending submission, charging its total wall time --
    downloads included -- to the claimant's per-run budget, so a
    claimant cannot spend the job on downloads that never reach
    verification."""
    number, login, target, comment_id = candidate
    started = time.monotonic()
    try:
        return _publish_candidate(candidate, context)
    finally:
        context['verify_by_login'][login] = (
            context['verify_by_login'].get(login, 0.0) + time.monotonic() - started)


def _publish_candidate(candidate, context):
    """Verify one pending submission.

    Returns (evidence_text, targets, None) to publish, or
    (None, None, reason) to skip.  The evidence record is serialised
    here, inside the per-candidate isolation, so nothing in the later
    write phase can fail on one submission's data.  Verification
    failures of every kind -- including malformed input that makes the
    verifier raise an unexpected exception type -- are reported as
    reasons; anything unexpected that still escapes is caught by the
    caller's per-candidate guard in main().
    """
    number, login, target, comment_id = candidate
    if login == 'ghost':
        return None, None, 'claimant account no longer exists (ghost)'
    issue = context['issues'][number]
    comments = context['histories'][number]
    if not grant_in_force(issue, comments, target):
        return None, None, 'claim grant is not currently in force'
    if (ROOT / 'evidence' / 'volunteers' / f'{target}.json').exists():
        return None, None, 'an evidence file for this task already exists'
    comment = next(c for c in comments if int(c['id']) == comment_id)
    if target.startswith(('R', 'B')):
        pinned_ids = granted_modules(comments, target)
        if not pinned_ids:
            return None, None, 'bot grant comment is missing a valid module list'
        by_id = context.setdefault('rows_by_id', {r['id']: r for r in context['rows']})
        if any(mid not in by_id for mid in pinned_ids):
            return None, None, 'bot grant names an unknown module id'
        if target.startswith('R'):
            base_pkt = 'P' + target[1:]
            if any(by_id[mid]['packet'] != base_pkt for mid in pinned_ids):
                return None, None, f'granted modules do not all belong to {base_pkt}'
        targets = [by_id[mid] for mid in pinned_ids]
    else:
        targets = tasks.select(context['rows'], target)
    if any(row['state'] != 'open' for row in targets):
        return None, None, 'snapshot state is not open for every module'
    if any(row['id'] in context['credited'] for row in targets):
        return None, None, 'some modules are already credited (partial or conflicting evidence)'
    if context['verify_budget']['used'] >= context['verify_budget']['cap']:
        return None, None, 'run verification time budget exhausted'
    if context['verify_by_login'].get(login, 0.0) >= LOGIN_TIME_BUDGET:
        return None, None, 'per-claimant time budget exhausted (downloads + verification)'
    urls = artifact_urls(comment['body'])
    if 'receipt.json' not in urls:
        return None, None, 'no receipt.json linked'
    if not any(allowed(url) for url in urls['receipt.json']):
        return None, None, 'receipt.json is not hosted on an allowlisted host'
    budget = {'used': 0, 'cap': CANDIDATE_DOWNLOAD_CAP}
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        try:
            receipt_path = fetch_one(urls['receipt.json'], directory, 'receipt.json', budget, context.get('past_deadline'))
            if receipt_path is None:
                return None, None, 'no downloadable receipt.json linked'
            try:
                receipt = json.loads(receipt_path.read_text(), parse_constant=_reject_constant)
            except (ValueError, UnicodeDecodeError, RecursionError):
                return None, None, 'linked receipt.json does not parse'
            if not isinstance(receipt, dict):
                return None, None, 'linked receipt.json is not a JSON object'
            if type(receipt.get('exit_code')) is not int or receipt['exit_code'] != 0:
                return None, None, 'receipt does not record a successful build'
            shape_error = receipt_shape_error(receipt)
            if shape_error:
                return None, None, shape_error
            if 'bundle_sha256' not in receipt:
                return None, None, 'receipt has no bundle (manual review checks it against the prepared checkout)'
            if 'build.log' not in urls or fetch_one(urls['build.log'], directory, 'build.log', budget, context.get('past_deadline')) is None:
                return None, None, 'no downloadable build.log linked'
            if 'targets.tar.gz' not in urls or fetch_one(urls['targets.tar.gz'], directory, 'targets.tar.gz', budget, context.get('past_deadline')) is None:
                return None, None, 'receipt names a bundle but none is downloadable'
            if replay_evidence.REPLAY_FILE in urls:
                fetch_one(urls[replay_evidence.REPLAY_FILE], directory, replay_evidence.REPLAY_FILE, budget, context.get('past_deadline'))
        except (_BudgetExceeded, _DeadlineExceeded) as error:
            return None, None, str(error)
        result, reason = verify_candidate(directory, targets, context['rows_by_module'],
                                          (context['verify_budget'],))
        if result is None:
            return None, None, reason
    record = {field: receipt[field] for field in RECEIPT_FIELDS if field in receipt}
    record.update(packet=target, contributor=login, claim=comment['html_url'],
                  reviewed_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  review=REVIEW_TEXT,
                  automation={'tool': 'tracker/publish.py',
                              'run': context['run_url'],
                              'kernel_replayed_targets': result['kernel_replayed_targets'],
                              'built_targets': result['built_targets']})
    try:
        text = json.dumps(record, indent=2, allow_nan=False) + '\n'
    except (ValueError, RecursionError) as error:
        return None, None, f'evidence record cannot be serialised: {error}'
    if len(text.encode()) > RECORD_SIZE_CAP:
        return None, None, f'evidence record exceeds the {RECORD_SIZE_CAP}-byte size cap'
    return text, targets, None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--limit', type=int, default=300,
                        help='maximum submissions to publish (manual batches cap at 300)')
    parser.add_argument('--dry-run', action='store_true',
                        help='verify and report, but write no files')
    parser.add_argument('--message-file', type=Path,
                        help='write the batch commit message here')
    parser.add_argument('--summary-file', type=Path,
                        help='write the run summary (Markdown) here')
    args = parser.parse_args()
    if args.limit < 1:
        parser.error('--limit must be positive')

    repo = os.environ['GITHUB_REPOSITORY']
    snapshot = submissions.claim_snapshot(repo)
    issues, histories = snapshot
    candidates = sorted(
        (c for c in submissions.volunteer_updates(repo, {}, snapshot) if c[3] is not None),
        key=lambda c: c[0])
    completed_path = ROOT / 'tracker' / 'completed.tsv'
    run_id = os.environ.get('GITHUB_RUN_ID')
    context = {
        'issues': {issue['number']: issue for issue in issues},
        'histories': histories,
        'rows': list(tasks.module_rows()),
        'credited': load_completed(completed_path),
        'run_url': (f'https://github.com/{repo}/actions/runs/{run_id}' if run_id else None),
    }
    context['rows_by_module'] = {row['module']: row for row in context['rows']}
    context['verify_budget'] = {'used': 0.0, 'cap': float(VERIFY_RUN_BUDGET)}
    context['verify_by_login'] = {}

    records, published, skipped = [], [], []
    # The deadline is measured from the job's start when the workflow
    # provides it (SWEEP_STARTED), so re-sweeps inside one job share a
    # single clock instead of each restarting it, and time remains for
    # the grant re-check and the push afterwards.
    job_epoch = os.environ.get('SWEEP_STARTED')
    started = time.monotonic()

    def past_deadline():
        if job_epoch:
            return time.time() > int(job_epoch) + RUN_DEADLINE_SECONDS
        return time.monotonic() - started > RUN_DEADLINE_SECONDS

    context['past_deadline'] = past_deadline
    for candidate in candidates:
        number, login, target, comment_id = candidate
        if len(published) >= args.limit:
            skipped.append((target, login, number, f'run limit of {args.limit} reached'))
            continue
        if past_deadline():
            skipped.append((target, login, number, 'run deadline reached'))
            continue
        try:
            text, targets, reason = publish_candidate(candidate, context)
        except (Exception, SystemExit) as error:
            text, reason = None, f'unexpected error: {type(error).__name__}: {error}'
            targets = None
        if text is None:
            skipped.append((target, login, number, clean(reason)))
            continue
        records.append((target, text, number, login, targets, comment_id))
        for row in targets:
            context['credited'][row['id']] = [row['id'], row['packet'], row['module'], EVIDENCE_VALUE]
        published.append((target, login, number))

    # Re-check every record against a fresh claim snapshot before
    # writing, so a claim revoked, closed, retitled or edited during
    # a long run is not credited from the stale snapshot.  A record
    # that fails the re-check is dropped, not credited, and its
    # in-memory ledger rows are rolled back; the rest of the batch
    # proceeds.  If the fresh snapshot itself cannot be fetched, the
    # run fails here, before any write.  Nothing is written unless at
    # least one record survives.
    if records and not args.dry_run:
        fresh_issues, fresh_histories = submissions.claim_snapshot(repo)
        fresh_by_number = {issue['number']: issue for issue in fresh_issues}
        kept = []
        for target, text, number, login, targets, comment_id in records:
            verified_body = next(
                (c['body'] for c in context['histories'].get(number, [])
                 if int(c['id']) == comment_id), None)
            try:
                in_force = recheck_record(fresh_by_number.get(number),
                                          fresh_histories.get(number, []),
                                          target, comment_id, login, verified_body,
                                          tuple(sorted(r['id'] for r in targets)))
            except Exception:
                in_force = False  # malformed fresh data: drop, never credit
            if in_force:
                kept.append((target, text, number, login, targets, comment_id))
            else:
                skipped.append((target, login, number,
                                'grant or submission no longer in force at write time'))
                published = [p for p in published if p[2] != number]
                for row in targets:
                    del context['credited'][row['id']]
        records = kept
        if records:
            for target, text, number, login, targets, comment_id in records:
                evidence_path = ROOT / 'evidence' / 'volunteers' / f'{target}.json'
                evidence_path.write_text(text)
            write_completed(completed_path, context['credited'])
    if not published and len(skipped) >= 10:
        print(f'::warning::nothing published and {len(skipped)} submissions skipped; '
              'check the run summary for a systemic problem', file=sys.stderr)
    if args.message_file:
        # In --dry-run mode nothing is written, so the calling workflow
        # never reaches its commit step and never reads this file.
        args.message_file.write_text(
            f'Publish {len(published)} automated volunteer replay submissions\n')
    heading = f'## Publish sweep: {len(published)} published, {len(skipped)} skipped'
    if args.dry_run:
        heading += ' (dry run)'
    verb = 'Would publish' if args.dry_run else 'Published'
    summary = [heading, '']
    summary += [f'- {verb} `{target}` by @{login} (#{number})'
                for target, login, number in published]
    # Collapse runs of identical skip reasons (a large backlog can
    # otherwise push the summary past the step-summary size limit).
    def esc(reason):
        # The summary is rendered as HTML by GitHub: neutralise the
        # markup-significant characters a hostile artifact name could
        # smuggle into a skip reason, as well as Markdown's.
        return (reason.replace('<', '&lt;').replace('>', '&gt;')
                .replace('`', chr(39)).replace('[', '(').replace(']', ')'))

    reason_counts = {}
    for _, _, _, reason in skipped:
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
    reason_seen = {}
    for target, login, number, reason in skipped:
        reason_seen[reason] = reason_seen.get(reason, 0) + 1
        if reason_counts[reason] > 3 and reason_seen[reason] == 4:
            summary.append(f'- … and {reason_counts[reason] - 3} more skipped: {esc(reason)}')
        if reason_counts[reason] > 3 and reason_seen[reason] > 3:
            continue
        summary.append(f'- Skipped `{target}` by @{login} (#{number}): {esc(reason)}')
    text = '\n'.join(summary) + '\n'
    # Cap each sweep's summary at 300 KB (newline-aware, so no UTF-8
    # codepoint is split): several sweeps in one job must stay under
    # the 1 MiB step-summary limit.
    if len(text.encode()) > 300_000:
        text = text.encode()[:290_000].decode(errors='ignore')
        text = text[:text.rfind('\n')] + '\n- … (summary truncated)\n'
    print(text, end='')
    if args.summary_file:
        args.summary_file.write_text(text)
    return 0


if __name__ == '__main__':
    sys.exit(main())
