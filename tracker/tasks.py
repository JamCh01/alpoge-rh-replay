#!/usr/bin/env python3
"""Look up module-sized replay tasks, reserve work and produce receipts."""
from collections import Counter
from pathlib import Path
from shutil import which
import argparse,datetime,gzip,hashlib,json,os,re,subprocess,sys,tarfile,time,urllib.request
base=Path(__file__).resolve().parent
repo='rohanarun/alpoge-rh-replay'
MAX_BATCH_MODULES = 64
MAX_OPEN_MULTI_CLAIMS = 8
CLAIM_ID_RE = r'(?:P\d{4}|R\d{4}|M\d{6}|B[0-9a-f]{12})'
CLAIM_TITLE_RE = re.compile(rf'Claim ({CLAIM_ID_RE})')
_BATCH_LINE_RE = re.compile(r'^\s*Modules:\s*(M\d{6}(?:[\s,]+M\d{6})*)\s*$', re.I | re.M)


def batch_claim_id(module_ids):
    """Deterministic 12-hex batch identifier committing to a sorted set of module IDs."""
    items = list(module_ids)
    ordered = sorted(set(items))
    if not ordered or len(ordered) > MAX_BATCH_MODULES or len(ordered) != len(items):
        raise ValueError(f'Batch claims require 1..{MAX_BATCH_MODULES} unique module IDs')
    if any(not isinstance(m, str) or not re.fullmatch(r'M\d{6}', m) for m in ordered):
        raise ValueError('Batch claims only accept M###### module IDs')
    return 'B' + hashlib.sha256(' '.join(ordered).encode()).hexdigest()[:12]


def parse_batch_modules(body):
    """Extract the sorted unique module tuple from a batch claim's `Modules:` line, or None."""
    match = _BATCH_LINE_RE.search((body or '')[:8192])
    if not match:
        return None
    items = re.findall(r'M\d{6}', match.group(1))
    if not items or len(items) > MAX_BATCH_MODULES or len(set(items)) != len(items):
        return None
    return tuple(sorted(items))


def format_grant_comment(target, module_ids=None):
    """Render the bot comment that records an authoritative reservation grant."""
    tail = 'Begin this reservation. Attach your receipt and log to this issue, or close it to release the work.'
    if module_ids is None:
        return f'Granted `{target}`. {tail}'
    ordered = sorted(set(module_ids))
    return f"Granted `{target}`. Modules ({len(ordered)}): {' '.join(ordered)}. {tail}"


def parse_granted_modules(comment_body, target):
    """Read the pinned module tuple from a bot grant comment for R#### or B<12hex>, or None."""
    match = re.match(rf'^Granted `{re.escape(target)}`\.\s+Modules \((\d+)\): (M\d{{6}}(?: M\d{{6}})*)\.',
                     comment_body or '')
    if not match:
        return None
    count = int(match.group(1))
    ids = match.group(2).split(' ')
    if count != len(ids) or len(set(ids)) != len(ids) or ids != sorted(ids):
        return None
    if target.startswith('B'):
        if len(ids) > MAX_BATCH_MODULES or batch_claim_id(ids) != target:
            return None
    return tuple(ids)


def completed_ids(completed_path=None):
    path = completed_path or (base / 'completed.tsv')
    if not path.exists():
        return set()
    import csv
    with path.open() as f:
        return {r['id'] for r in csv.DictReader(f, delimiter='\t')}
def digest(path):
 h=hashlib.sha256()
 with path.open('rb') as f:
  for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
 return h.hexdigest()
BUILT=re.compile(r'^\S+\s+\[\d+/\d+\]\s+Built\s+([^\s:]+)')
def rebuilt_imports(log_text,project,requested):
 """Project modules other than the requested targets that this Lake invocation reports as built."""
 log_text=re.sub(r'\x1b\[[0-9;]*m','',log_text)
 built={m[1] for m in map(BUILT.match,log_text.splitlines()) if m}
 return sorted(built.intersection(project)-set(requested))
def host_resources():
 """CPU count, total memory, and the peak resident set size of the largest finished child process."""
 info={'cpu_count':os.cpu_count()}
 try:
  with open('/proc/meminfo') as f:
   for line in f:
    if line.startswith('MemTotal:'):info['mem_total_kib']=int(line.split()[1]);break
 except OSError:pass
 try:
  import resource
  info['peak_child_rss_kib']=resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
 except ImportError:pass
 return info
def module_rows():
    with gzip.open(base / 'modules.jsonl.gz', 'rt') as stream:
        yield from map(json.loads, stream)

def load():
 rows=list(module_rows())
 completed=base/'completed.tsv'
 if completed.exists():
  import csv
  with completed.open() as f:done={r['id']:r['evidence'] for r in csv.DictReader(f,delimiter='\t')}
  for r in rows:
   if r['id'] in done:r['state']=done[r['id']]
 return rows
def select(rows, target, *, project_modules=None, granted_ids=None):
    """Collect matching targets and, for builds, the names needed to classify the transcript."""
    all_rows = []
    for row in rows:
        if project_modules is not None:
            project_modules.add(row['module'])
        all_rows.append(row)
    if granted_ids is not None:
        wanted = set(granted_ids)
        targets = [r for r in all_rows if r['id'] in wanted]
        if not wanted or len(targets) != len(wanted):
            raise SystemExit('Unknown granted module(s) for: ' + str(target))
        return targets
    if isinstance(target, str) and re.fullmatch(r'R\d{4}', target):
        pkt = 'P' + target[1:]
        pkt_rows = [r for r in all_rows if r['packet'] == pkt]
        if not pkt_rows or any(r['state'] == 'assigned' for r in pkt_rows):
            raise SystemExit('Unavailable packet remainder: ' + target)
        done = completed_ids()
        targets = [r for r in pkt_rows if r['state'] == 'open' and r['id'] not in done]
        if not targets:
            raise SystemExit('No open modules remain in packet: ' + pkt)
        return targets
    if isinstance(target, str) and ',' in target:
        parts = [p.strip() for p in target.split(',') if p.strip()]
        try:
            batch_claim_id(parts)
        except ValueError as exc:
            raise SystemExit(str(exc))
        wanted = set(parts)
        targets = [r for r in all_rows if r['id'] in wanted]
        if len(targets) != len(wanted):
            raise SystemExit('Unknown task in batch: ' + target)
        return targets
    targets = [r for r in all_rows if target in (r['id'], r['packet'], r['module'])]
    if not targets:
        raise SystemExit('Unknown task or packet: ' + str(target))
    return targets
def cold_modules(rows, busy):
    """Rank available tasks with no project imports by their available direct consumers."""
    available = [r for r in rows if r['state'] == 'open'
                 and r['id'] not in busy and r['packet'] not in busy
                 and ('R' + r['packet'][1:]) not in busy]
    leaves = {r['id']: r for r in available if not r['imports']}
    sole = Counter()
    direct = Counter()
    for row in available:
        imports = set(row['imports'])
        for dep in imports:
            if dep in leaves:
                direct[dep] += 1
                if len(imports) == 1:
                    sole[dep] += 1
    ranked = sorted(leaves, key=lambda task: (-sole[task], -direct[task], task))
    return [(leaves[task], sole[task], direct[task]) for task in ranked]

def packet_suggestions(rows, busy, include_partial=False):
    """Split claimable packets into fully open and partially completed ones.

    A packet is fully open when every module is open. A partial packet has
    at least one open module while its remaining modules already carry
    evidence; it qualifies only when no module is assigned and neither the
    packet nor any of its modules is claimed. Packets appear in index order
    when fully open. Partial packets sort by depth, then by most open
    modules first. Each entry is (first open row, open count, total count).
    A partial packet cannot be claimed as a packet: the claim bot reserves
    every module that already has evidence, so its open modules are claimed
    individually instead.
    """
    groups = {}
    for r in rows:
        groups.setdefault(r['packet'], []).append(r)
    full = []
    partial = []
    for name, members in groups.items():
        if name in busy or ('R' + name[1:]) in busy or any(r['id'] in busy for r in members):
            continue
        opened = [r for r in members if r['state'] == 'open']
        if not opened:
            continue
        entry = (opened[0], len(opened), len(members))
        if len(opened) == len(members):
            full.append(entry)
        elif include_partial and all(r['state'] != 'assigned' for r in members):
            partial.append(entry)
    partial.sort(key=lambda e: (e[0]['depth'], -e[1], e[0]['packet']))
    return full, partial

def authenticated_gh():
    cli = which('gh')
    # `auth status` fails if any saved account is invalid, even when the
    # active credential used by `gh api` is valid.
    if cli and subprocess.run([cli, 'auth', 'token', '--hostname', 'github.com'],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        return cli


def open_issues():
    route = f'repos/{repo}/issues?state=open&per_page=100'
    cli = authenticated_gh()
    if cli:
        output = subprocess.check_output(
            [cli, 'api', '--hostname', 'github.com', '--paginate', route],
            text=True,
        )
        decoder = json.JSONDecoder()
        index = 0
        # Older gh versions write one JSON array per page without a separator.
        while index < len(output):
            if output[index].isspace():
                index += 1
                continue
            page, index = decoder.raw_decode(output, index)
            yield from page
        return

    url = 'https://api.github.com/' + route
    while url:
        request = urllib.request.Request(url, headers={'User-Agent': 'rh-replay-tracker'})
        with urllib.request.urlopen(request, timeout=25) as response:
            yield from json.load(response)
            match = re.search(r'<([^>]+)>; rel="next"', response.headers.get('Link', ''))
            url = match[1] if match else None


def claims():
    cli = authenticated_gh()
    if cli:
        owner, name = repo.split('/')
        query = '''
        query($owner: String!, $name: String!, $endCursor: String) {
          repository(owner: $owner, name: $name) {
            issues(states: OPEN, first: 100, after: $endCursor) {
              nodes { title }
              pageInfo { hasNextPage endCursor }
            }
          }
        }
        '''
        pages = json.loads(subprocess.check_output(
            [cli, 'api', 'graphql', '--hostname', 'github.com', '--paginate', '--slurp',
             '-f', f'owner={owner}', '-f', f'name={name}', '-f', f'query={query}'],
            text=True))
        titles = (issue['title'] for page in pages
                  for issue in page['data']['repository']['issues']['nodes'])
    else:
        titles = (issue['title'] for issue in open_issues() if 'pull_request' not in issue)
    return {match[1] for title in titles if (match := CLAIM_TITLE_RE.fullmatch(title))}

def run(targets,checkout,out,bundle,fast_bundle=False,*,project_modules=None):
 project=checkout.resolve();output=out.resolve()
 if output.exists():raise SystemExit('Output directory already exists: '+str(output))
 dirs=project.parent
 env={**os.environ,'ELAN_HOME':str(dirs/'elan'),'XDG_CACHE_HOME':str(dirs/'cache')}
 toolchain = (project / 'lean-toolchain').read_text().strip()
 if toolchain != 'leanprover/lean4:v4.33.1':raise SystemExit('Wrong Lean toolchain')
 launcher = ['elan', 'run', toolchain]
 version=subprocess.check_output([*launcher, 'lean','--version'],env=env,cwd=project,text=True).strip()
 mathlib=subprocess.check_output(['git','-C',str(project/'.lake/packages/mathlib'),'rev-parse','HEAD'],text=True).strip()
 if '4.33.1' not in version or mathlib!=json.loads((base/'summary.json').read_text())['mathlib_commit']:raise SystemExit('Unexpected toolchain or mathlib')
 for r in targets:
  if digest(project/'src'/(r['module'].replace('.','/')+'.lean'))!=r['source_sha256']:raise SystemExit('Source hash mismatch: '+r['module'])
 cmd=['lake','build',*['+'+r['module']+':olean' for r in targets]]
 output.mkdir(parents=True,exist_ok=False)
 log=output/'build.log'
 build=project/'.lake/build'
 started=datetime.datetime.now(datetime.timezone.utc);clock=time.monotonic()
 with log.open('wb') as f:result=subprocess.run([*launcher, *cmd],cwd=project,env=env,stdout=f,stderr=subprocess.STDOUT)
 elapsed=round(time.monotonic()-clock,1)
 if project_modules is None:project_modules={r['module'] for r in module_rows()}
 rebuilt=rebuilt_imports(log.read_text(errors='replace'),project_modules,{r['module'] for r in targets})
 receipt={'tasks':[r['id'] for r in targets],'started_utc':started.isoformat(),'ended_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'elapsed_seconds':elapsed,'host':host_resources(),'toolchain':version,'mathlib':mathlib,'inputs_manifest_sha256':digest(base.parent/'parts.sha256'),'command':cmd,'exit_code':result.returncode,'log_sha256':digest(log),'rebuilt_project_modules':rebuilt,'objects':{}}
 receipt['project_root']=str(project)
 if result.returncode==0:
  for r in targets:receipt['objects'][r['id']]=digest(build/'lib/lean'/(r['module'].replace('.','/')+'.olean'))
  if bundle or fast_bundle:
   artifact=output/'targets.tar.gz'
   options = {'compresslevel': 1} if fast_bundle else {}
   with tarfile.open(artifact,'w:gz',**options) as tar:
    for r in targets:
     stem=r['module'].replace('.','/')
     for prefix in ['lib/lean','ir']:
      path=build/prefix/stem
      for f in path.parent.glob(path.name+'.*'):
       # Lake regenerates this compiler input when building a module; it is
       # not an output artifact needed for reuse or downstream compilation.
       if f.name == path.name+'.setup.json':continue
       if f.is_file() and not f.is_symlink():tar.add(f,arcname=str(f.relative_to(build)),recursive=False)
   receipt['bundle_sha256']=digest(artifact)
 receipt_temp=output/'receipt.json.tmp'
 receipt_temp.write_text(json.dumps(receipt,indent=2)+'\n')
 receipt_temp.replace(output/'receipt.json')
 print('Saved',output,'with exit status',result.returncode)
 return result.returncode
def main():
 p=argparse.ArgumentParser(description=__doc__);s=p.add_subparsers(dest='action',required=True)
 nx=s.add_parser('next');nx.add_argument('--limit',type=int,default=8)
 mode=nx.add_mutually_exclusive_group()
 mode.add_argument('--warm',type=Path,metavar='INVENTORY',help='rank disjoint builds against a worker inventory')
 nx.add_argument('--primary-inventory',type=Path,help='skip targets already on the primary')
 nx.add_argument('--max-builds',type=int,default=3,help='maximum estimated project modules per warm suggestion')
 mode.add_argument('--cold',action='store_true',help='show available individual modules with no project imports')
 nx.add_argument('--include-partial',action='store_true',help='also list partially completed packets (up to --limit more) whose open modules can be claimed individually')
 for n in ['show','claim','build']:
  sub=s.add_parser(n);sub.add_argument('target')
  if n=='build':
   sub.add_argument('checkout',type=Path);sub.add_argument('output',type=Path);sub.add_argument('--bundle',action='store_true')
   sub.add_argument('--fast-bundle',action='store_true',help='write the same tar members with gzip level 1 for faster packaging')
 a=p.parse_args()
 if a.action=='build':
  project_modules=set()
  targets=select(module_rows(),a.target,project_modules=project_modules)
  return run(targets,a.checkout,a.output,a.bundle,a.fast_bundle,project_modules=project_modules)
 rows=load()
 if a.action=='next':
  if a.primary_inventory and not a.warm:p.error('--primary-inventory requires --warm')
  if a.max_builds < 1:p.error('--max-builds must be positive')
  busy=claims()
  if a.warm:
   from replay_evidence import read_inventory
   from warm import suggestions
   cached=set(read_inventory(a.warm)['modules'])
   primary=set(read_inventory(a.primary_inventory)['modules']) if a.primary_inventory else set()
   for row,cost,consumers in suggestions(rows,busy,cached,primary,a.limit,a.max_builds):
    print(row['id'],row['packet'],'estimated project builds',cost,'direct consumers',consumers)
   return 0
  if a.cold:
   for row,sole,direct in cold_modules(rows,busy)[:max(0,a.limit)]:
    print(row['id'],row['packet'],row['module'],'sole-project-import consumers',sole,'direct consumers',direct)
   return 0
  full,partial=packet_suggestions(rows,busy,a.include_partial)
  for r,opened,total in full[:max(0,a.limit)]:
   print(r['packet'],'depth',r['depth'],'begins with',r['module'],f'{opened}/{total} open')
  shown_partial=partial[:max(0,a.limit)]
  for r,opened,total in shown_partial:
   print(r['packet'],'depth',r['depth'],'begins with',r['module'],f'{opened}/{total} open','(partial)')
  if shown_partial:
   print('Partial packets already have evidence for some modules and cannot be claimed as a packet.')
   print('Claim their open modules individually: show <packet> lists them, then claim <module ID>.')
  return 0
 sel=select(rows,a.target)
 if a.action=='show':
  for r in sel:print(json.dumps(r,ensure_ascii=False))
 elif a.action=='claim':
  busy=claims()
  if any(r['id'] in busy or r['packet'] in busy or ('R' + r['packet'][1:]) in busy or r['state']!='open' for r in sel):
   raise SystemExit('One or more modules are already claimed, observed, or assigned.')
  if re.fullmatch(r'R\d{4}', a.target):
   pkt = 'P' + a.target[1:]
   if any(r['id'] in busy for r in rows if r['packet'] == pkt):
    raise SystemExit('One or more modules in this packet already have an open claim.')
   claim_id = a.target
   body = f'I am working on {claim_id} ({len(sel)} open modules in {pkt}). I will attach the input hashes, build transcript and receipt, or close this claim if I stop.'
  elif ',' in a.target:
   ordered = sorted(r['id'] for r in sel)
   claim_id = batch_claim_id(ordered)
   if claim_id in busy:
    raise SystemExit('This batch claim is already open.')
   body = (f'I am working on {claim_id}.\n\nModules: {" ".join(ordered)}\n\n'
           'I will attach the input hashes, build transcript and receipt, or close this claim if I stop.')
  else:
   claim_id = a.target if a.target == sel[0]['packet'] else sel[0]['id']
   body = f'I am working on {claim_id}. I will attach the input hashes, build transcript and receipt, or close this claim if I stop.'
  subprocess.run(['gh', 'issue', 'create', '--repo', repo, '--title', 'Claim ' + claim_id,
                  '--body', body], check=True)
 return 0
if __name__=='__main__':sys.exit(main())
