"""Run local services in one process namespace for benchmark requests."""
import atexit
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
processes = []


def start(name, command, cwd, environment):
    log = (ROOT / f'{name}-runtime.log').open('a', encoding='utf-8')
    process = subprocess.Popen(command, cwd=cwd, env=environment, stdout=log, stderr=subprocess.STDOUT)
    processes.append((name, process, log))
    print('START', name, flush=True)
    return process


def cleanup():
    for name, process, log in reversed(processes):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=40)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        log.close()


atexit.register(cleanup)


def request(url, payload=None, token=None, method=None):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(url, body, headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=600) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f'HTTP {error.code} at {urllib.parse.urlsplit(url).path}: {error.read(500).decode(errors="replace")}') from None


def ready(name, process, url, timeout=300):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f'{name} exited with code {process.returncode}. Inspect {name}-runtime.log')
        try:
            request(url)
            print('READY', name, flush=True)
            return
        except (urllib.error.URLError, TimeoutError, RuntimeError):
            time.sleep(2)
    raise TimeoutError(f'{name} readiness deadline exceeded')


def contextbase_environment():
    env = os.environ.copy()
    env.update({
        'PYTHONPATH': str(ROOT / 'contextbase/hindsight-extensions/contextdb-service') + ':' + str(ROOT / 'contextbase/hindsight-api-slim'),
        'CONTEXTDB_DEPLOY_DIR': str(ROOT / 'state/contextbase'),
        'CONTEXTBASE_API_DATABASE_URL': 'pg0',
        'CONTEXTBASE_API_HOST': '127.0.0.1',
        'CONTEXTBASE_API_LLM_PROVIDER': 'openai',
        'CONTEXTBASE_API_LLM_BASE_URL': 'http://127.0.0.1:18090/contextbase/v1',
        'CONTEXTBASE_API_LLM_MODEL': 'qwen2.5-1.5b-local',
        'CONTEXTBASE_API_LLM_API_KEY': 'local-benchmark',
        'CONTEXTBASE_API_LLM_MAX_CONCURRENT': '1',
        'CONTEXTBASE_API_LLM_TIMEOUT': '600',
        'CONTEXTBASE_API_LLM_MAX_RETRIES': '1',
        'CONTEXTBASE_API_RETAIN_MAX_COMPLETION_TOKENS': '4096',
        'CONTEXTBASE_API_ENABLE_AUTO_CONSOLIDATION': 'false',
        'CONTEXTBASE_API_EMBEDDINGS_PROVIDER': 'openai',
        'CONTEXTBASE_API_EMBEDDINGS_OPENAI_BASE_URL': 'http://127.0.0.1:18090/contextbase/v1',
        'CONTEXTBASE_API_EMBEDDINGS_OPENAI_MODEL': 'multilingual-minilm',
        'CONTEXTBASE_API_EMBEDDINGS_OPENAI_API_KEY': 'local-benchmark',
        'CONTEXTBASE_API_EMBEDDINGS_OPENAI_DIMENSIONS': '384',
        'CONTEXTBASE_API_RERANKER_PROVIDER': 'rrf',
        'CONTEXTBASE_API_STORE_DOCUMENT_TEXT': 'true',
        'CONTEXTBASE_API_LLM_TRACE_ENABLED': 'false',
        'CONTEXTBASE_API_AUDIT_LOG_ENABLED': 'false',
        'OPENBLAS_NUM_THREADS': '1', 'OMP_NUM_THREADS': '2',
    })
    return env


def run():
    model_env = os.environ.copy()
    model_env.update(HF_HUB_DISABLE_XET='1', OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='2')
    model = start('models', [str(ROOT / 'model-venv/bin/python'), '-m', 'uvicorn', 'model_server:app', '--host', '127.0.0.1', '--port', '18090'], ROOT, model_env)
    ready('models', model, 'http://127.0.0.1:18090/health')
    api = start('contextbase', [str(ROOT / 'api-venv/bin/python'), '-m', 'hindsight_ext_contextdb.local', '--component', 'api', '--port', '18888'], ROOT / 'contextbase', contextbase_environment())
    ready('contextbase', api, 'http://127.0.0.1:18888/health/ready', timeout=600)
    credentials = json.loads((ROOT / 'state/contextbase/admin-key.json').read_text())
    token = credentials['secret']
    bank = 'recall_v1_smoke'
    result = request('http://127.0.0.1:18888/v1/default/banks/' + bank, {'name':bank}, token, method='PUT')
    print('CREATED_BANK', bank, flush=True)
    result = request('http://127.0.0.1:18888/ext/contextdb/v1/users', {'name':f'recall-smoke-{time.time_ns()}'}, token)
    user_id = result['user_id']
    issued = request(f'http://127.0.0.1:18888/ext/contextdb/v1/users/{user_id}/keys', {
        'name':'benchmark', 'authorization':{'mode':'selected','workspaces':[{'bank_id':bank,'permission':'read_write'}]}
    }, token)
    key = issued['secret']
    start_time = time.monotonic()
    result = request(f'http://127.0.0.1:18888/v1/default/banks/{bank}/memories', {
        'items':[{'document_id':'smoke-source','content':'赤杨生产环境最多自动重试11次。','timestamp':'unset'}], 'async':False
    }, key)
    print('RETAIN', json.dumps(result,ensure_ascii=False), round(time.monotonic()-start_time,2), flush=True)
    result = request(f'http://127.0.0.1:18888/v1/default/banks/{bank}/memories/recall', {
        'query':'赤杨生产环境最多重试几次？','budget':'mid','include':{'chunks':{'max_tokens':1024}}
    }, key)
    (ROOT / 'contextbase-smoke-response.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print('RECALL',len(result['results']),'results','keys',list(result),flush=True)


if __name__ == '__main__':
    run()
