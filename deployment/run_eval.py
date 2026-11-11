"""Deploy API-only local test instances and run the fixed V1 benchmark."""
import argparse
from collections import defaultdict
from contextlib import redirect_stdout
from datetime import datetime, timezone
import json
import os
import secrets
import sys
import time
from pathlib import Path

import tiktoken

from run_native import ROOT, contextbase_environment, ready, request, start

sys.path.insert(0, str(ROOT.parent / 'doc_recall_v1'))
import bench

MODEL_URL = 'http://127.0.0.1:18090'
WEKNORA_URL = 'http://127.0.0.1:18080'
CONTEXTBASE_URL = 'http://127.0.0.1:18888'


def private_json(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump(value, stream)


def start_contextbase(run_id):
    env = contextbase_environment()
    env.update(CONTEXTBASE_API_RETAIN_EXTRACTION_MODE='chunks',
               CONTEXTBASE_API_RETAIN_CHUNK_SIZE='512')
    process = start('contextbase', [str(ROOT / 'api-venv/bin/python'), '-m',
        'hindsight_ext_contextdb.local', '--component', 'api', '--port', '18888'],
        ROOT / 'contextbase', env)
    ready('contextbase', process, CONTEXTBASE_URL + '/health/ready', timeout=600)
    admin = json.loads((ROOT / 'state/contextbase/admin-key.json').read_text())['secret']
    bank = 'recall_' + run_id
    request(CONTEXTBASE_URL + '/v1/default/banks/' + bank, {'name': bank}, admin, method='PUT')
    users = CONTEXTBASE_URL + '/ext/contextdb/v1/users'
    user = request(users, {'name':'bench-' + run_id}, admin)['user_id']
    key = request(f'{users}/{user}/keys', {'name':'v1-document-recall', 'authorization':{
        'mode':'selected', 'workspaces':[{'bank_id':bank, 'permission':'read_write'}]}}, admin)['secret']
    os.environ.update(CONTEXTBASE_URL=CONTEXTBASE_URL, CONTEXTBASE_BANK_ID=bank, CONTEXTBASE_API_KEY=key)
    print('SCOPE contextbase', bank, flush=True)


def start_weknora(run_id, existing_knowledge=None):
    state = ROOT / 'state/weknora'
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    credentials = state / 'credentials.json'
    if not credentials.exists():
        private_json(credentials, {'password':'Aa1'+secrets.token_urlsafe(18), 'jwt':secrets.token_hex(32),
                                   'aes':secrets.token_hex(16)})
    auth = json.loads(credentials.read_text())
    if len(auth['password']) > 32:
        auth['password'] = 'Aa1'+secrets.token_urlsafe(18)
        replacement = credentials.with_suffix('.new')
        private_json(replacement, auth)
        replacement.replace(credentials)
    models = state / 'builtin-models.yaml'
    models.write_text(f'''builtin_models:
  - id: benchmark-embedding
    type: Embedding
    name: multilingual-minilm
    source: remote
    is_default: true
    parameters:
      provider: openai
      base_url: {MODEL_URL}/weknora/v1
      api_key: local-benchmark
      embedding_parameters:
        dimension: 384
  - id: benchmark-llm
    type: KnowledgeQA
    name: qwen2.5-1.5b-local
    source: remote
    is_default: true
    parameters:
      provider: openai
      base_url: {MODEL_URL}/weknora/v1
      api_key: local-benchmark
      context_window: 16384
''', encoding='utf-8')
    env = os.environ.copy()
    env.update(DB_DRIVER='sqlite', DB_PATH=str(state / 'weknora.db'), RETRIEVE_DRIVER='sqlite',
        STORAGE_TYPE='local', LOCAL_STORAGE_BASE_DIR=str(state / 'files'), STREAM_MANAGER_TYPE='memory',
        JWT_SECRET=auth['jwt'], SYSTEM_AES_KEY=auth['aes'], BUILTIN_MODELS_CONFIG=str(models),
        WEKNORA_SANDBOX_MODE='disabled', WEKNORA_SANDBOX_DOCKER_ENABLED='false',
        SSRF_WHITELIST='127.0.0.1', SERVER_HOST='127.0.0.1', SERVER_PORT='18080',
        GIN_MODE='release', LOG_LEVEL='warn', AUTO_MIGRATE='true', NEO4J_ENABLE='false',
        ENABLE_GRAPH_RAG='false', DISABLE_REGISTRATION='false')
    env.pop('REDIS_ADDR', None)
    process = start('weknora', [str(ROOT / 'weknora-lite')], ROOT / 'weknora', env)
    ready('weknora', process, WEKNORA_URL + '/health')
    login = {'email':'benchmark@weknora.test','password':auth['password']}
    try:
        session = request(WEKNORA_URL + '/api/v1/auth/login', login)
    except RuntimeError as error:
        if not str(error).startswith('HTTP 401 '):
            raise
        request(WEKNORA_URL + '/api/v1/auth/register', {'username':'Benchmark', **login})
        session = request(WEKNORA_URL + '/api/v1/auth/login', login)
    token = session['token']
    tenant = session['active_tenant']['id']
    knowledge = existing_knowledge or request(WEKNORA_URL + '/api/v1/knowledge-bases', {
        'name':'recall-' + run_id, 'type':'document','embedding_model_id':'benchmark-embedding',
        'chunking_config':{'chunk_size':512},
        'question_generation_config':{'enabled':False},
        'profile_config':{'enabled':False},
    }, token)['data']['id']
    key = request(f'{WEKNORA_URL}/api/v1/tenants/{tenant}/api-keys',
                  {'name':'v1-recall-' + run_id,'full_access':True}, token)['data']['token']
    os.environ.update(WEKNORA_URL=WEKNORA_URL, WEKNORA_KB_ID=knowledge, WEKNORA_API_KEY=key)
    print('SCOPE weknora', knowledge, flush=True)


def model_context(product, run_id, phase, identifier):
    bench.write_json(Path(os.environ.get('BENCH_MEASUREMENT_DIR', ROOT)) / 'measurement-context.json', {
        product: {'phase':phase, 'id':identifier, 'run_id':run_id},
    })


class TrackingAPI(bench.API):
    def __init__(self, product, run_id, query_ids):
        super().__init__(product)
        self.product = product
        self.run_id = run_id
        self.query_ids = query_ids
        self.imported = 0

    def request(self, path, payload=None):
        if path in ('/knowledge-search','/memories/recall'):
            identifier = self.query_ids[payload['query']]
            model_context(self.product, self.run_id, 'retrieve', identifier)
            if self.product == 'weknora':
                payload = {**payload,'rerank':{'enabled':False}}
        data = super().request(path,payload)
        if path == '/memories' or path.endswith('/knowledge/manual'):
            self.imported += 1
            print('INGESTED',self.product,self.imported,flush=True)
        return data


def model_totals(run_dir, product, run_id):
    usage_log = Path(os.environ.get('BENCH_MEASUREMENT_DIR', ROOT)) / 'model-usage.jsonl'
    rows = [row for row in bench.read_rows(usage_log)
            if row.get('run_id') == run_id and row.get('product') == product]
    groups = defaultdict(list)
    for row in rows:
        if row['phase'] in ('ingest','retrieve') and row['id'] != 'warmup':
            groups[(row['phase'],row['id'])].append(row)
    with (run_dir / f'{product}-model-calls.jsonl').open('w',encoding='utf-8') as output:
        for row in rows:
            bench.write_row(output,row)
    path = run_dir / f'{product}-usage.jsonl'
    with path.open('w',encoding='utf-8') as output:
        for (phase,identifier), calls in sorted(groups.items()):
            status = 'observed' if all(row['status']=='observed' for row in calls) else 'estimated'
            fields = {key:sum(row[key] for row in calls) for key in ('input_tokens','output_tokens','calls')}
            bench.write_row(output,{'phase':phase,'id':identifier,'status':status,**fields})
    return path


def run_product(product, run_id, run_dir, questions, encoding, wait_for_weknora=False):
    api = TrackingAPI(product,run_id,{q['query']:q['id'] for q in questions})
    mapping = run_dir / f'{product}-documents.json'
    start_time = time.perf_counter()
    model_context(product,run_id,'ingest','run')
    bench.ingest(api,product,mapping)
    print('INDEXED',product,round(time.perf_counter()-start_time,2),'seconds',flush=True)
    if wait_for_weknora and not (ROOT / 'weknora-lite').exists():
        print('WAIT building WeKnora before timing requests', flush=True)
        deadline = time.monotonic() + 600
        while not (ROOT / 'weknora-lite').exists():
            if time.monotonic() >= deadline:
                raise TimeoutError('WeKnora executable was not built')
            time.sleep(3)
    model_context(product,run_id,'warmup','health')
    # A health check is kept outside the measured query set.
    source_map = {remote: local for local,remote in json.loads(mapping.read_text()).items()}
    payload = '基准健康检查：确认测试库可以执行检索。'
    original_query_ids = api.query_ids
    api.query_ids = {**original_query_ids,payload:'warmup'}
    bench.retrieve(api,product,payload,source_map)
    api.query_ids = original_query_ids
    capture = run_dir / f'{product}-capture.jsonl'
    bench.collect(api,product,mapping,capture,questions,encoding.encode_ordinary,encoding.decode)
    model_context(product,run_id,'unassigned','unassigned')
    usage = model_totals(run_dir,product,run_id)
    with (run_dir / f'{product}-summary-initial.json').open('w',encoding='utf-8') as output, redirect_stdout(output):
        bench.score(capture,{q['id']:q for q in questions},True,usage)
    print('CAPTURED',product,len(questions),flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('products',nargs='*',choices=bench.PRODUCTS)
    args = parser.parse_args()
    products = args.products or list(bench.PRODUCTS)
    run_id = datetime.now(timezone.utc).strftime('v1_%Y%m%d_%H%M%S')
    run_dir = ROOT / 'results' / run_id
    run_dir.mkdir(parents=True)
    print('RUN',run_dir,flush=True)
    os.environ['BENCH_HTTP_TIMEOUT_SECONDS']='600'
    bench.write_json(ROOT / 'measurement-context.json', {})
    model_env = os.environ.copy()
    model_env.update(HF_HUB_DISABLE_XET='1', OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='2')
    model = start('models',[str(ROOT/'model-venv/bin/python'),'-m','uvicorn','model_server:app','--host','127.0.0.1','--port','18090'],ROOT,model_env)
    ready('models',model,MODEL_URL+'/health')
    questions = bench.read_rows(bench.ROOT/'questions.jsonl')
    encoding = tiktoken.get_encoding('o200k_base')
    for product in products:
        starter = start_weknora if product=='weknora' else start_contextbase
        starter(run_id)
        run_product(product,run_id,run_dir,questions,encoding,wait_for_weknora='weknora' in products)


if __name__ == '__main__':
    main()
