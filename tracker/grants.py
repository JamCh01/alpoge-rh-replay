"""Reconcile mutually exclusive volunteer reservations from trusted main-branch code."""
import json,os,re,sys,urllib.request
from claim_comments import COMMENT_FEED, comments_by_issue, repository_comments
from tasks import (CLAIM_ID_RE, CLAIM_TITLE_RE, MAX_OPEN_MULTI_CLAIMS, batch_claim_id,
                   format_grant_comment, load, parse_batch_modules, parse_granted_modules)
from github_rate_limit import rate_limit_delay, retry_rate_limited
from collections import defaultdict
from pathlib import Path
PAUSED=Path(__file__).with_name('paused-claimants.txt')
def load_paused(path=None):
 """GitHub logins whose new claims receive no grant; '#' starts a comment."""
 path=path or PAUSED
 if not path.is_file():return set()
 return {line.split('#',1)[0].strip().lower() for line in path.read_text().splitlines()}-{''}
def api(route,method='GET',body=None):
 base='https://api.github.com/repos/'+os.environ['GITHUB_REPOSITORY']
 request=urllib.request.Request(base+route,method=method,headers={'Authorization':'Bearer '+os.environ['GITHUB_TOKEN'],'Accept':'application/vnd.github+json','User-Agent':'replay-claims'},data=None if body is None else json.dumps(body).encode())
 with urllib.request.urlopen(request,timeout=30) as response:return json.loads(response.read() or 'null')
def pages(route):
    if route == '/' + COMMENT_FEED:
        return repository_comments(api_pages, route)
    return api_pages(route)

def api_pages(route):
    page = 1
    while True:
        batch = api(f'{route}&page={page}')
        yield from batch
        if len(batch) < 100:
            return
        page += 1


def _prior_state(issues, get_pages=None):
    get_pages = get_pages or pages
    labels = {i['number']: {label['name'] for label in i['labels']} for i in issues}
    decided = [i for i in issues if labels[i['number']] & {'claim-granted', 'claim-conflict'}]
    histories = comments_by_issue(decided, get_pages('/' + COMMENT_FEED))
    prior = {}
    prior_modules = {}
    conflicts = set()
    for number, comments in histories.items():
        for comment in sorted(comments, key=lambda c: c['id']):
            if comment['user']['login'] != 'github-actions[bot]':
                continue
            grant = re.match(rf'Granted `({CLAIM_ID_RE})`\.', comment['body'])
            if grant:
                if 'claim-granted' in labels[number]:
                    prior[number] = grant[1]
                    if grant[1].startswith(('R', 'B')):
                        prior_modules[number] = parse_granted_modules(comment['body'], grant[1])
                conflicts.discard(number)
            elif comment['body'].startswith('No grant.'):
                prior.pop(number, None)
                prior_modules.pop(number, None)
                conflicts.add(number)
    return prior, conflicts, prior_modules


def prior_decisions(issues, get_pages=None):
    prior, conflicts, _ = _prior_state(issues, get_pages)
    return prior, conflicts


def reconcile(get_pages=None, mutate=None):
 get_pages = get_pages or pages
 mutate = mutate or api
 rows=load();groups=defaultdict(set);reserved=set();completed=set();assigned=set();all_ids=set()
 for r in rows:
  all_ids.add(r['id'])
  groups[r['id']].add(r['id']);groups[r['packet']].add(r['id'])
  if r['state']!='open':reserved.add(r['id'])
  if r['state']=='assigned':assigned.add(r['id'])
  if r['state'] not in ('open','assigned'):completed.add(r['id'])
 issues=[i for i in get_pages('/issues?state=open&per_page=100') if 'pull_request' not in i]
 paused=load_paused();planned=[];taken=set();multi_counts=defaultdict(int)
 # Preserve valid grants first, then use issue number as a deterministic tie-breaker.
 prior,conflicts,prior_modules=_prior_state(issues,get_pages)
 for i in sorted(issues,key=lambda z:(prior.get(z['number'])!=z['title'].removeprefix('Claim '),z['number'])):
  labels={l['name'] for l in i['labels']};m=CLAIM_TITLE_RE.fullmatch(i['title'])
  if not m and not labels.intersection({'claim-granted','claim-conflict','claim'}):continue
  target=m[1] if m else ''
  raw_login=((i.get('user') or {}).get('login') or '')
  login=raw_login.lower()
  existing_grant=prior.get(i['number'])==target
  explicit_mods=None
  if target.startswith('R'):
   pkt_mods=groups.get('P'+target[1:],set())
   if existing_grant:
    pinned=prior_modules.get(i['number'])
    mods=set(pinned) if pinned else set()
    existing_grant=bool(mods)
   else:
    mods=(pkt_mods-reserved) if (pkt_mods and not pkt_mods.intersection(assigned)) else set()
   explicit_mods=mods
   finished=bool(pkt_mods) and not existing_grant and pkt_mods<=completed
   avail=bool(mods) and (existing_grant or not mods.intersection(reserved))
  elif target.startswith('B'):
   if existing_grant:
    pinned=prior_modules.get(i['number'])
    mods=set(pinned) if pinned else set()
    existing_grant=bool(mods)
    declared_mods=mods
   else:
    declared=parse_batch_modules(i.get('body') or '')
    valid_decl=bool(declared) and batch_claim_id(declared)==target and set(declared)<=all_ids
    declared_mods=set(declared) if valid_decl else set()
    mods=declared_mods
   explicit_mods=mods
   finished=bool(declared_mods) and not existing_grant and declared_mods<=completed
   avail=bool(mods) and (existing_grant or not mods.intersection(reserved))
  else:
   mods=groups.get(target,set())
   finished=bool(mods) and not existing_grant and mods<=completed
   avail=bool(mods) and (existing_grant or not mods.intersection(reserved))
  is_multi=target.startswith(('P','R','B'))
  # A paused claimant keeps existing grants so earlier submissions can be fixed,
  # but receives no new grants.
  held=not existing_grant and login in paused
  over_cap=is_multi and not existing_grant and bool(login) and multi_counts[login]>=MAX_OPEN_MULTI_CLAIMS
  # New build evidence should not revoke work already granted to a volunteer.
  win=avail and not held and not over_cap and not mods.intersection(taken)
  if win:
   taken.update(mods)
   if is_multi and login:
    multi_counts[login]+=1
  desired='claim-granted' if win else 'claim-conflict'
  planned.append((i['number'],labels,target,desired,held or finished,over_cap,raw_login,explicit_mods))
 for number,labels,target,desired,held,_over_cap,_raw_login,_explicit_mods in planned:
  other='claim-conflict' if desired=='claim-granted' else 'claim-granted'
  if other in labels:mutate(f'/issues/{number}/labels/{other}','DELETE')
 for number,labels,target,desired,held,over_cap,raw_login,explicit_mods in planned:
  if desired not in labels or 'claim' not in labels:mutate(f'/issues/{number}/labels','POST',{'labels':['claim',desired]})
  if desired=='claim-granted' and (desired not in labels or prior.get(number)!=target):
   mutate(f'/issues/{number}/comments','POST',{'body':format_grant_comment(target,explicit_mods)})
  elif desired=='claim-conflict' and (desired not in labels or number not in conflicts):
   paused_owner=raw_login.lower() in paused
   body=('No grant. New claims from this account are paused until the pending submissions on your granted claims are fixed. '
         'See the review comments there and the submission self-check in CONTRIBUTING.md. This issue is closed.') if held and paused_owner else \
        ('No grant. This work is already completed. This issue is closed. Run `git pull` before `python3 tracker/tasks.py next` '
         'so your queue skips completed modules.') if held else \
        (f'No grant. @{raw_login} already holds {MAX_OPEN_MULTI_CLAIMS} open multi-module claims; '
         'finish or close existing multi-module claims before requesting more.') if over_cap else \
        'No grant. This selection is unavailable or overlaps existing work. Close this issue and choose a new open module or packet with `python3 tracker/tasks.py next`.'
   mutate(f'/issues/{number}/comments','POST',{'body':body})
  # Close refused claims from paused accounts so they don't grow the open set every run reads.
  if held and desired=='claim-conflict':
   mutate(f'/issues/{number}','PATCH',{'state':'closed','state_reason':'not_planned'})


def main():
    if '--graphql' in sys.argv:
        import claims_graphql
        return claims_graphql.reconcile(reconcile)
    return retry_rate_limited(reconcile, 'Rechecking all claims')


if __name__=='__main__':main()
