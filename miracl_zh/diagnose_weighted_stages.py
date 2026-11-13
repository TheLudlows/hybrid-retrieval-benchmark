"""Diagnose all frozen 0.97/0.03 queries without changing native retrieval policy.

Uses the independently restored index from verify_rrf_configuration.py. Complete
responses remain in private persistent storage; public compact traces retain the
IDs, source scores, ranks and phase details needed to reproduce the diagnosis.
"""
import argparse
from argparse import Namespace
from collections import Counter
import gzip
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import time
from urllib.error import URLError
from urllib.request import urlopen

import psycopg2
from psycopg2 import sql

import bench
from diagnose_fusion_stages import explain
from run_scale import Worker, save

WEIGHTS = {'semantic': 0.97, 'bm25': 0.03, 'graph': 1.0, 'temporal': 1.0}


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def atomic_gzip(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('wb') as raw:
        with gzip.GzipFile(fileobj=raw, mode='wb', mtime=0) as zipped:
            zipped.write(json.dumps(value, ensure_ascii=False).encode())
        raw.flush()
        os.fsync(raw.fileno())
    temporary.replace(path)


def explain_weighted(payload, mapping, labels):
    trace = payload['trace']
    phase = next(p for p in trace['summary']['phase_metrics'] if p['phase_name'] == 'rrf_merge')
    assert phase['details']['rrf_weights'] == WEIGHTS
    for arm in trace['retrieval_results']:
        assert all(row['node_id'] in mapping for row in arm['results'])
    for row in trace['rrf_merged']:
        assert row['node_id'] in mapping
        expected = sum(WEIGHTS[key.removesuffix('_rank')] / (60 + rank) for key, rank in row['source_ranks'].items())
        assert abs(expected - row['rrf_score']) < 1e-12
    summary = explain(payload, mapping, labels)
    # Reuse the existing stage analyzer but replace its equal-weight explanation
    # with the actual resolved contributions checked above.
    for name in ('first_returned_evidence', 'best_positive_fusion_evidence'):
        evidence = summary[name]
        if evidence:
            evidence['rrf_contributions'] = {key: WEIGHTS[key.removesuffix('_rank')] / (60 + rank) for key, rank in evidence['source_unit_ranks'].items()}
            assert abs(sum(evidence['rrf_contributions'].values()) - evidence['rrf_score']) < 1e-12
    summary['rrf_entries_verified'] = len(trace['rrf_merged'])
    return summary


def compact(payload, mapping, raw_sha256):
    trace = payload['trace']
    return {
        'schema': 'compact-native-trace-v1', 'raw_response_gzip_sha256': raw_sha256,
        'query': {k: v for k, v in trace['query'].items() if k != 'query_embedding'},
        'query_embedding_sha256': hashlib.sha256(json.dumps(trace['query']['query_embedding']).encode()).hexdigest(),
        'retrieval_results': [{'method_name': arm['method_name'], 'fact_type': arm.get('fact_type'),
            'results': [{'node_id': row['node_id'], 'official_id': mapping[row['node_id']], 'rank': row['rank'], 'score': row['score'], 'score_name': row['score_name']} for row in arm['results']]} for arm in trace['retrieval_results']],
        'rrf_merged': [{k: v for k, v in row.items() if k != 'text'} | {'official_id': mapping[row['node_id']]} for row in trace['rrf_merged']],
        'reranked': [{k: v for k, v in row.items() if k != 'text'} | {'official_id': mapping[row['node_id']]} for row in trace['reranked']],
        'phase_metrics': trace['summary']['phase_metrics'],
        'results': [{'node_id': row['id'], 'official_id': row['document_id'], 'scores': row['scores']} for row in payload['results']],
    }


def write_report(out, rows, archived_metrics, usage):
    counts = Counter()
    stages = ('semantic', 'bm25', 'fusion', 'post_scoring_candidates', 'final')
    metrics = {name: {key: sum(row['stage_quality'].get(name, {}).get(key, 0) for row in rows) / len(rows)
                      for key in ('hit_at_1', 'hit_at_10', 'ndcg_at_10', 'recall_at_10')} for name in stages}
    for row in rows:
        quality = row['stage_quality']
        semantic = quality['semantic']['hit_at_1']
        bm25 = quality.get('bm25', {}).get('hit_at_1', 0)
        fusion = quality['fusion']['hit_at_1']
        final = quality['final']['hit_at_1']
        counts['queries'] += 1
        counts['final_top1_correct' if final else 'final_top1_miss'] += 1
        if semantic and not final:
            counts['semantic_top1_lost_by_final'] += 1
        if not semantic and final:
            counts['semantic_top1_rescued_by_final'] += 1
        if (semantic or bm25) and not fusion:
            counts['some_arm_top1_positive_fusion_top1_not_positive'] += 1
        if semantic and not fusion:
            counts['semantic_top1_positive_fusion_top1_not_positive'] += 1
        if fusion and not final:
            counts['fusion_top1_positive_final_top1_not_positive'] += 1
        if not fusion and final:
            counts['fusion_top1_not_positive_final_top1_positive'] += 1
        if not final and not semantic and not bm25:
            counts['final_miss_neither_arm_top1_positive'] += 1
        if final and not semantic and bm25:
            counts['final_hit_with_bm25_positive_semantic_not_positive_head'] += 1
        if row['matches_archived_full_ranking']:
            counts['full_ranking_matches_archive'] += 1
        if row['matches_archived_top10']:
            counts['top10_matches_archive'] += 1
        if row['matches_archived_quality']:
            counts['per_query_quality_matches_archive'] += 1
        if row['final_passage_ids'] != row['post_scoring_passage_ids']:
            counts['final_list_differs_from_60_candidates'] += 1
    positive_counts = Counter(p['classification'] for row in rows for p in row['positive_evidence'])
    result = {'scope': 'All 393 frozen dev queries; raw-arm readouts are diagnostic, not standalone single-arm native benchmarks.',
              'weights': WEIGHTS, 'queries': len(rows), 'stage_metrics': metrics, 'counts': dict(counts),
              'positive_judgment_classification': dict(positive_counts),
              'verified_rrf_entries': sum(row['rrf_entries_verified'] for row in rows),
              'model_usage': usage,
              'baseline_quality': {k: v for k, v in archived_metrics.items() if k in ('hit_at_1', 'hit_at_10', 'ndcg_at_10', 'recall_at_10')}}
    save(out / 'analysis.json', result)
    lines = ['# 当前加权配置的原生阶段诊断', '',
        '本轮覆盖同一10万段语料、全部393道开发集查询。配置冻结为正式版0.97/0.03，图/时间支路关闭，模型重排关闭；没有调参、改索引或重新入库。诊断请求耗时不计入此前正式性能。', '',
        '原始语义/BM25指标来自同一次混合请求的trace支路，不是切换产品开关后的单路正式跑分。模型未调用：trace.reranked表示原生候选与综合评分阶段。', '',
        '| 阶段 | Hit@1 | Hit@10 | nDCG@10 | Recall@10 |', '| --- | ---: | ---: | ---: | ---: |']
    for name in stages:
        values = metrics[name]
        lines.append(f'| {name} | {values["hit_at_1"]:.2%} | {values["hit_at_10"]:.2%} | {values["ndcg_at_10"]:.6f} | {values["recall_at_10"]:.2%} |')
    lines += ['', '## 首位断点', '',
        f'- 原始语义首位为正例、最终首位不是正例：{counts["semantic_top1_lost_by_final"]}题。',
        f'- 原始语义首位不是正例、最终首位为正例：{counts["semantic_top1_rescued_by_final"]}题。',
        f'- 语义首位为正例、融合首位不是正例：{counts["semantic_top1_positive_fusion_top1_not_positive"]}题。',
        f'- 任一路首位为正例、融合首位不是正例：{counts["some_arm_top1_positive_fusion_top1_not_positive"]}题。',
        f'- 融合首位为正例、最终首位不是正例：{counts["fusion_top1_positive_final_top1_not_positive"]}题。',
        f'- 最终首位未命中且两路原始首位都未命中：{counts["final_miss_neither_arm_top1_positive"]}题。', '',
        f'本次完整返回排名与正式归档一致{counts["full_ranking_matches_archive"]}/393题；Top10一致{counts["top10_matches_archive"]}/393题；逐题四项质量一致{counts["per_query_quality_matches_archive"]}/393题。逐题差异保留，不覆盖既有正式结果。', '',
        f'实际模型记录：{usage.get("diagnostic/embedding", {}).get("calls", 0)}次诊断Embedding、输入{usage.get("diagnostic/embedding", {}).get("input_tokens", 0)} Token；模型重排调用为0。预热单独记录。全部{result["verified_rrf_entries"]}条RRF记录逐项核验了加权公式。', '',
        '逐题证据见summary.json、top1-breakpoints.json及traces中的紧凑轨迹。紧凑轨迹保留官方ID、原生单位ID、原始得分/排名、融合/候选/最终排名、查询向量哈希和阶段详情；完整原始响应保留在私有持久目录，并以SHA-256绑定。所有官方标注为0与未标注的首位结果均区分记录，未标注不等同真实不相关。', '',
        '本轮未搜索新参数，不能根据诊断表把原始语义支路成绩当作已验证的纯向量产品成绩。后续选择方案必须保留现有配置作为对照；已使用过的查询不称为盲测。两端原库、索引、检查点和备份保留，独立诊断API完成后停止。', '']
    (out / 'REPORT.md').write_text('\n'.join(lines))
    return result


def main(args):
    repo = Path(__file__).resolve().parents[1]
    source = args.source.resolve()
    work = args.work.resolve()
    profile = work / 'runtime'
    out = args.results.resolve()
    out.mkdir(parents=True, exist_ok=True)
    public_traces = out / 'traces'
    public_traces.mkdir(exist_ok=True)
    raw_folder = work / 'weighted-stage-diagnosis-raw'
    raw_folder.mkdir(mode=0o700, exist_ok=True)
    settings = json.loads(args.settings.read_text())
    data = Path(settings['data100k'])
    topics, qrels = bench.topics_and_qrels(data)
    dataset = bench.dataset_source(data)
    baseline = repo / 'results/miracl-zh-rrf-config-formal-20261113'
    archived_path = baseline / 'configured-dense97-dev393/rankings.jsonl'
    archived = {row['qid']: row['passages'] for row in map(json.loads, archived_path.read_text().splitlines())}
    archived_metrics = json.loads((baseline / 'configured-dense97-dev393/metrics.json').read_text())
    version = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=source, text=True).strip()
    assert version == 'cdf6430e028ff456d490436ff99e50fcf4aba59c'
    assert not subprocess.check_output(['git', 'status', '--porcelain'], cwd=source).strip()
    clone_proof = json.loads((work / 'restored.json').read_text())
    assert clone_proof['database'] == 'miracl_rrf_config_validation_20261113'
    url = (work / 'database-url').read_text()
    assert url.split('?', 1)[0].endswith('/' + clone_proof['database'])
    bank = json.loads((Path(settings['work']) / 'runtime-v2/contextbase-100k/connection.json').read_text())['CONTEXTBASE_BANK_ID']
    key = json.loads((profile / 'admin-key.json').read_text())['secret']
    os.environ.update(CONTEXTBASE_URL='http://127.0.0.1:18889', CONTEXTBASE_API_KEY=key, CONTEXTBASE_BANK_ID=bank)
    api = bench.prior_api('contextbase')
    env = {key: value for key, value in os.environ.items() if not key.startswith(('CONTEXTBASE_API_', 'HINDSIGHT_API_'))}
    for line in (profile / 'api.env').read_text().splitlines():
        if line and not line.startswith('#') and '=' in line:
            name, value = line.split('=', 1)
            words = shlex.split(value)
            env[name] = words[0] if words else ''
    for prefix in ('CONTEXTBASE_API_', 'HINDSIGHT_API_'):
        env[prefix + 'DATABASE_URL'] = url
        env[prefix + 'PORT'] = '18889'
    env.update(PYTHONPATH=str(source / 'hindsight-extensions/contextdb-service') + ':' + str(source / 'hindsight-api-slim'),
               CONTEXTDB_DEPLOY_DIR=str(profile), OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='2')
    config = {'scope': 'All 393 queries; stage diagnosis only, no new tuning', 'source_commit': version,
              'corpus_sha256': dataset['subset']['file']['sha256'], 'baseline_rankings_sha256': digest(archived_path),
              'query_order_sha256': hashlib.sha256(json.dumps(list(topics)).encode()).hexdigest(),
              'driver_sha256': digest(Path(__file__)), 'weights': WEIGHTS, 'concurrency': 1,
              'full_response_directory': str(raw_folder), 'compact_trace_schema': 'compact-native-trace-v1',
              'max_candidates': 60, 'max_tokens': 8192, 'source_cap': 100, 'recall_high': 1000}
    config_path = out / 'configuration.json'
    if config_path.exists():
        assert json.loads(config_path.read_text()) == config, 'Resume config changed'
    else:
        save(config_path, config)
    with psycopg2.connect(url) as connection, connection.cursor() as cursor:
        cursor.execute(sql.SQL('SELECT id,document_id FROM {}.memory_units WHERE bank_id=%s').format(sql.Identifier('ctxdb_ws_' + bank)), (bank,))
        mapping = {str(node): passage for node, passage in cursor.fetchall()}
    assert len(set(mapping.values())) == 100000
    measurement = Path(settings['measurement_dir'])
    old_measurement = (measurement / 'measurement-context.json').read_bytes()
    run_id = 'weighted-stage-diagnosis-20261113/dev393'
    child = None
    rows = []
    log = (profile / 'weighted-stage-diagnosis.log').open('a')
    try:
        try:
            with urlopen(api.base + '/health/ready', timeout=2):
                raise RuntimeError('The diagnostic port is already occupied; do not stop another process')
        except URLError:
            pass
        child = subprocess.Popen([str(args.python), '-m', 'hindsight_ext_contextdb.local', '--component', 'api', '--port', '18889'],
                                 cwd=source, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + 180
        while True:
            if child.poll() is not None:
                raise RuntimeError('Diagnostic API startup failed; see private log')
            try:
                with urlopen(api.base + '/health/ready', timeout=5) as response:
                    if response.status == 200:
                        break
            except (URLError, TimeoutError):
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError('Diagnostic API readiness timed out')
            time.sleep(1)
        expected = {'enable_text_search': True, 'enable_graph_retrieval': False, 'enable_temporal_retrieval': False, 'enable_reranking': False,
                    **{'recall_rrf_' + name + '_weight': weight for name, weight in WEIGHTS.items()}}
        readback = api.request('/config')['config']
        assert all(readback[name] == value for name, value in expected.items()), 'Do not modify frozen bank config'
        save(out / 'bank-config-readback.json', {name: readback[name] for name in expected})
        for i in range(4):
            save(measurement / 'measurement-context.json', {'contextbase': {'phase': 'diagnostic_warmup', 'id': str(i), 'run_id': run_id}})
            bench.fetch_ranked(api, 'contextbase', f'检索预热：第{i + 1}次中文文档检索', 60, 'benchmark-bge-reranker', 8192, True)
        for qid, query in topics.items():
            raw_path = raw_folder / (qid.replace('#', '-') + '.json.gz')
            trace_path = public_traces / raw_path.name
            if raw_path.exists():
                payload = json.load(gzip.open(raw_path, 'rt'))
            else:
                save(measurement / 'measurement-context.json', {'contextbase': {'phase': 'diagnostic', 'id': qid, 'run_id': run_id}})
                payload = api.request('/memories/recall', {'query': query, 'budget': 'high', 'types': ['world', 'experience'],
                    'trace': True, 'max_tokens': 8192, 'include': {'entities': None, 'chunks': {'max_tokens': 16384}}})
                atomic_gzip(raw_path, payload)
            assert payload['trace']['query']['query_text'] == query
            row = {'qid': qid, 'query': query, **explain_weighted(payload, mapping, qrels[qid])}
            row['post_scoring_passage_ids'] = list(dict.fromkeys(mapping[x['node_id']] for x in payload['trace']['reranked']))
            row['matches_archived_full_ranking'] = row['final_passage_ids'] == archived[qid]
            row['matches_archived_top10'] = row['final_passage_ids'][:10] == archived[qid][:10]
            old = archived_metrics['per_topic'][qid]
            row['matches_archived_quality'] = all(abs(row['stage_quality']['final'][name] - old[name]) < 1e-12 for name in ('hit_at_1', 'hit_at_10', 'ndcg_at_10', 'recall_at_10'))
            row['first_positive_semantic_rank'] = row['stage_quality']['semantic']['first_positive_rank']
            rows.append(row)
            atomic_gzip(trace_path, compact(payload, mapping, digest(raw_path)))
            save(out / 'summary.json', rows)
            save(out / 'execution-status.json', {'state': 'diagnosing', 'completed': len(rows), 'target': len(topics), 'last_qid': qid})
            print('Diagnosed', len(rows), '/', len(topics), qid, flush=True)
        usage = Worker.archive_calls(Namespace(measure=measurement), 'contextbase', run_id, out / 'model-calls.jsonl.gz')
        assert not any(values.get('calls', 0) for name, values in usage.items() if name.endswith('/rerank'))
        raw_calls = list(map(json.loads, gzip.open(out / 'model-calls.jsonl.gz', 'rt').read().splitlines()))
        called = Counter(row['id'] for row in raw_calls if row['phase'] == 'diagnostic' and row['kind'] == 'embedding')
        assert set(called) == set(topics), 'Missing model call provenance'
        save(out / 'model-usage.json', usage)
        save(out / 'top1-breakpoints.json', [row for row in rows if not row['stage_quality']['final']['hit_at_1']])
        result = write_report(out, rows, archived_metrics, usage)
        save(out / 'execution-status.json', {'state': 'complete', 'completed': len(rows), 'target': len(topics),
            'no_parameters_changed': True, 'original_service_untouched': True, 'full_raw_responses_retained': True})
        print(json.dumps(result['counts'], ensure_ascii=False), flush=True)
    except Exception as error:
        save(out / 'execution-status.json', {'state': 'failed', 'completed': len(rows), 'target': len(topics),
            'error_type': type(error).__name__, 'message': str(error), 'recovery': 'Rerun the identical command; durable raw responses are reused.'})
        raise
    finally:
        if child and child.poll() is None:
            child.terminate()
            try:
                child.wait(30)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        log.close()
        (measurement / 'measurement-context.json').write_bytes(old_measurement)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--settings', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--python', type=Path, required=True)
    parser.add_argument('--results', type=Path, required=True)
    main(parser.parse_args())
