"""Verify formal RRF bank configuration on a separately restored MIRACL index.

Requires a private runtime cloned from the preserved benchmark deployment and a
separately restored database. Never starts, stops or modifies the original service.
Reuses bench.capture/score, including fsync checkpoints and configuration binding.
"""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys
import time
from argparse import Namespace
from urllib.error import URLError

import bench
from run_scale import save, Worker
from audit_index import audit


def main(args):
    repo = Path(__file__).resolve().parents[1]
    work = args.work.resolve()
    profile = work / 'runtime'
    out = args.results.resolve()
    out.mkdir(parents=True, exist_ok=True)
    settings = json.loads(args.settings.read_text())
    data = Path(settings['data100k'])
    measure = Path(settings['measurement_dir'])
    source = bench.dataset_source(data)
    topics, qrels = bench.topics_and_qrels(data)
    old = repo / 'results/miracl-zh-lexical-fusion-prototype-20261113'
    frozen = json.loads((old / 'query-splits.json').read_text())['tune_dev100']
    run_prefix = 'formal-rrf-config-20261113/'
    bank = json.loads((Path(settings['work']) / 'runtime-v2/contextbase-100k/connection.json').read_text())['CONTEXTBASE_BANK_ID']
    key = json.loads((profile / 'admin-key.json').read_text())['secret']
    api_url = 'http://127.0.0.1:18889'
    os.environ.update(CONTEXTBASE_URL=api_url, CONTEXTBASE_API_KEY=key, CONTEXTBASE_BANK_ID=bank)
    api = bench.prior_api('contextbase')
    env = os.environ.copy()
    for prefix in ('CONTEXTBASE_API_', 'HINDSIGHT_API_'):
        for key_name in list(env):
            if key_name.startswith(prefix):
                env.pop(key_name)
    for line in (profile / 'api.env').read_text().splitlines():
        if not line.strip() or line.startswith('#') or '=' not in line:
            continue
        key_name, value = line.split('=', 1)
        words = shlex.split(value)
        env[key_name] = words[0] if words else ''
    url = (work / 'database-url').read_text()
    values = {
        'DATABASE_URL': url, 'HOST': '127.0.0.1', 'PORT': '18889',
        'EMBEDDINGS_PROVIDER': 'openai', 'EMBEDDINGS_OPENAI_MODEL': 'bge-m3',
        'EMBEDDINGS_OPENAI_DIMENSIONS': '1024', 'EMBEDDINGS_OPENAI_BASE_URL': 'http://127.0.0.1:18090/contextbase/v1',
        'TEXT_SEARCH_EXTENSION': 'pg_search', 'TEXT_SEARCH_EXTENSION_PG_SEARCH_TOKENIZER': 'jieba',
        'ENABLE_TEXT_SEARCH': 'true', 'ENABLE_GRAPH_RETRIEVAL': 'false',
        'ENABLE_TEMPORAL_RETRIEVAL': 'false', 'ENABLE_RERANKING': 'false',
        'RERANKER_PROVIDER': 'tei', 'RERANKER_TEI_URL': 'http://127.0.0.1:18090/contextbase/tei',
        'RERANKER_MAX_CANDIDATES': '60', 'RERANKER_MAX_CANDIDATES_HIGH': '60',
        'RECALL_MAX_CANDIDATES_PER_SOURCE': '100', 'RECALL_STRATEGY_BOOSTS': '',
        'RECALL_BUDGET_FUNCTION': 'fixed', 'RECALL_BUDGET_FIXED_HIGH': '1000',
        'RECALL_RRF_SEMANTIC_WEIGHT': '1', 'RECALL_RRF_BM25_WEIGHT': '1',
        'RECALL_RRF_GRAPH_WEIGHT': '1', 'RECALL_RRF_TEMPORAL_WEIGHT': '1',
    }
    for prefix in ('CONTEXTBASE_API_', 'HINDSIGHT_API_'):
        env.update({prefix + k: v for k, v in values.items()})
    env.update(PYTHONPATH=str(args.source / 'hindsight-extensions/contextdb-service') + ':' + str(args.source / 'hindsight-api-slim'),
               CONTEXTDB_DEPLOY_DIR=str(profile), OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='2')
    config = {'corpus': source['subset'], 'source_base': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=args.source, text=True).strip(),
              'source_patch_sha256': hashlib.sha256(subprocess.check_output(['git', 'diff', 'HEAD'], cwd=args.source)).hexdigest(),
              'model_reranking': False, 'candidate_ceiling': 60, 'max_tokens': 8192, 'concurrency': 1,
              'original_service_untouched': True, 'database': url.rsplit('/', 1)[-1],
              'weights_only': True, 'query_normalization': False,
              'driver_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'cases': {'unit-default-tune100': {'semantic': 1.0, 'bm25': 1.0}, 'configured-dense97-dev393': {'semantic': 0.97, 'bm25': 0.03}},
              'global_environment': {k: v for k, v in values.items() if k != 'DATABASE_URL'}}
    changed = set(subprocess.check_output(['git', 'diff', '--name-only', 'HEAD'], cwd=args.source, text=True).splitlines())
    changed.update(subprocess.check_output(['git', 'ls-files', '--others', '--exclude-standard'], cwd=args.source, text=True).splitlines())
    config['source_files_sha256'] = {name: hashlib.sha256((args.source / name).read_bytes()).hexdigest() for name in sorted(changed) if (args.source / name).is_file()}
    save(out / 'configuration.json', config)
    digest = hashlib.sha256((out / 'configuration.json').read_bytes()).hexdigest()
    checkpoint = profile / 'ingest.sqlite'
    # This is a COPY. Rebind only its endpoint after restoring the same official-ID index.
    with sqlite3.connect(checkpoint) as db:
        db.execute('UPDATE miracl_ingest_run SET binding=? WHERE id=1', (bench.index_binding(api, 'contextbase', source),))
    os.environ['AUDIT_POSTGRES_URL'] = url
    audit(Namespace(data=data, checkpoint=checkpoint, product='contextbase', weknora_db=None, output=out / 'index-audit.json'))
    child = None
    log = (profile / 'service.log').open('a')

    def start():
        nonlocal child
        child = subprocess.Popen([str(args.python), '-m', 'hindsight_ext_contextdb.local', '--component', 'api', '--port', '18889'],
                                 cwd=args.source, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise RuntimeError('Isolated API startup failed; inspect private runtime/service.log')
            try:
                from urllib.request import urlopen
                with urlopen(api_url + '/health/ready', timeout=5) as response:
                    if response.status == 200:
                        return
            except (URLError, TimeoutError):
                pass
            time.sleep(1)
        raise RuntimeError('Isolated API readiness timed out')

    def stop():
        if child and child.poll() is None:
            child.terminate()
            try:
                child.wait(30)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()

    def capture(name, selected, current_source, max_queries=None):
        folder = out / name
        folder.mkdir(exist_ok=True)
        save(out / 'execution-status.json', {'state': 'querying', 'case': name, 'total_queries': len(selected)})
        run = folder / 'rankings.jsonl'
        bench.capture(Namespace(product='contextbase', data=data, run=run, passage_map=checkpoint, limit=60,
                                reranker='benchmark-bge-reranker', resume=run.exists(), warmup=4, max_queries=max_queries,
                                smoke_index=False, config_sha256=digest + '/' + name, measurement_dir=measure,
                                run_id=run_prefix + name, max_tokens=8192, disable_rerank=True), selected, current_source)
        if max_queries is None:
            result = bench.score(run, {q: qrels[q] for q in selected}, None, current_source)
            save(folder / 'metrics.json', result)
            usage = Worker.archive_calls(Namespace(measure=measure), 'contextbase', run_prefix + name, folder / 'model-calls.jsonl.gz')
            if any(v.get('calls', 0) for k, v in usage.items() if k.endswith('/rerank')):
                raise RuntimeError('Unexpected model rerank calls')
            assert usage['query/embedding']['calls'] == len(selected)
            save(folder / 'model-usage.json', usage)
        return folder

    def probe(label, semantic, bm25):
        run_id = run_prefix + 'trace-' + label
        qid = next(q for q, text in topics.items() if text == '哥斯达黎加的国歌是什么？')
        save(measure / 'measurement-context.json', {'contextbase': {'phase': 'probe', 'id': qid, 'run_id': run_id}})
        payload = api.request('/memories/recall', {'query': topics[qid], 'budget': 'high', 'types': ['world', 'experience'], 'trace': True, 'max_tokens': 8192})
        with gzip.open(out / ('trace-' + label + '.json.gz'), 'wt') as stream:
            json.dump(payload, stream, ensure_ascii=False)
        trace = payload['trace']
        weights = {'semantic': semantic, 'bm25': bm25, 'graph': 1.0, 'temporal': 1.0}
        phase = next(p for p in trace['summary']['phase_metrics'] if p['phase_name'] == 'rrf_merge')
        assert phase['details']['rrf_weights'] == weights
        for row in trace['rrf_merged']:
            expected = sum(weights[k.removesuffix('_rank')] / (60 + rank) for k, rank in row['source_ranks'].items())
            assert abs(row['rrf_score'] - expected) < 1e-12
        assert any(m['method_name'] == 'bm25' and m['results'] for m in trace['retrieval_results'])
        save(out / ('probe-' + label + '.json'), {'qid': qid, 'weights': weights, 'all_rrf_scores_checked': len(trace['rrf_merged']),
            'bm25_returned_candidates': True, 'query_embedding_sha256': hashlib.sha256(json.dumps(trace['query']['query_embedding']).encode()).hexdigest()})
        usage = Worker.archive_calls(Namespace(measure=measure), 'contextbase', run_id, out / ('probe-' + label + '-model-calls.jsonl.gz'))
        assert sum(v.get('calls', 0) for k, v in usage.items() if k.endswith('/rerank')) == 0
        save(out / ('probe-' + label + '-model-usage.json'), usage)

    try:
        start()
        flags = {'enable_text_search': True, 'enable_graph_retrieval': False, 'enable_temporal_retrieval': False, 'enable_reranking': False}
        api.request('/config', {'updates': {**flags, 'recall_rrf_semantic_weight': None, 'recall_rrf_bm25_weight': None}}, method='PATCH')
        probe('unit-default', 1.0, 1.0)
        tune = {q: t for q, t in topics.items() if q in set(frozen)}
        tune_source = {**source, 'dataset': 'miracl/zh/tune_dev100/subset-' + source['subset']['file']['sha256'][:16]}
        control = capture('unit-default-tune100', tune, tune_source, 4)
        before = len((control / 'rankings.jsonl').read_text().splitlines())
        stop()
        start()
        control = capture('unit-default-tune100', tune, tune_source)
        save(out / 'restart-proof.json', {'before_restart_completed': before, 'after_restart_completed': 100,
                                       'resume_skips_completed_queries': True, 'same_index_restored': True})
        api.request('/config', {'updates': {'recall_rrf_semantic_weight': 0.97, 'recall_rrf_bm25_weight': 0.03}}, method='PATCH')
        readback = api.request('/config')['config']
        save(out / 'bank-config-readback.json', {k: readback[k] for k in (*flags, 'recall_rrf_semantic_weight', 'recall_rrf_bm25_weight', 'recall_rrf_graph_weight', 'recall_rrf_temporal_weight')})
        probe('configured-dense97', 0.97, 0.03)
        assert json.loads((out / 'probe-unit-default.json').read_text())['query_embedding_sha256'] == json.loads((out / 'probe-configured-dense97.json').read_text())['query_embedding_sha256']
        ranked = capture('configured-dense97-dev393', topics, source)
        concordance = {}
        for label, new, old_path in (
            ('unit_default_tune100', control / 'rankings.jsonl', old / '100k/contextbase/stock_equivalent-tune_dev100-cap60/rankings.jsonl'),
            ('configured_dense97_dev393', ranked / 'rankings.jsonl', old / '100k/contextbase/dense97-dev393-cap60/rankings.jsonl'),
        ):
            existing = {r['qid']: r['passages'] for r in map(json.loads, old_path.read_text().splitlines())}
            rows = list(map(json.loads, new.read_text().splitlines()))
            mismatches = [r['qid'] for r in rows if r['passages'] != existing[r['qid']]]
            concordance[label] = {'queries': len(rows), 'exact_rank_matches': len(rows) - len(mismatches), 'mismatch_qids': mismatches}
        save(out / 'rank-concordance.json', concordance)
        save(out / 'execution-status.json', {'state': 'complete', 'original_service_untouched': True, 'both_product_data_retained': True})
    except Exception as error:
        save(out / 'execution-status.json', {'state': 'failed', 'error_type': type(error).__name__, 'message': str(error), 'resume': 'Rerun the same command; per-query files are durable.'})
        raise
    finally:
        stop()
        log.close()
        save(measure / 'measurement-context.json', {'contextbase': {'phase': 'idle', 'id': 'none', 'run_id': 'post-formal-rrf-config-idle'}})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--settings', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--python', type=Path, required=True)
    parser.add_argument('--results', type=Path, required=True)
    main(parser.parse_args())
