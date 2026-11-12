"""Explain an archived paired MIRACL run without inventing pre-rerank evidence."""
import argparse
from collections import Counter
import csv
import json
import math
from pathlib import Path
import random
import statistics

from bench import dataset_source, score, topics_and_qrels


def analyze(data, results, output):
    source = dataset_source(data)
    topics, qrels = topics_and_qrels(data)
    output.mkdir(parents=True, exist_ok=True)
    runs = {p: {r['qid']: r for r in map(json.loads, (results / f'{p}-60.jsonl').read_text().splitlines())}
            for p in ('contextbase', 'weknora')}
    metrics = {p: score(results / f'{p}-60.jsonl', qrels, None, source) for p in runs}
    details = []
    for qid, query in topics.items():
        positives = {p for p, label in qrels[qid].items() if label}
        row = {'qid': qid, 'query': query, 'positive_count': len(positives)}
        for product in runs:
            ranked = runs[product][qid]['passages']
            positions = [i for i, p in enumerate(ranked, 1) if p in positives]
            missing = sorted(positives - set(ranked))
            row[product] = {'first_positive_rank': min(positions) if positions else None,
                            'positive_positions': positions, 'missing_positive_ids': missing,
                            'returned_depth': len(ranked), **metrics[product]['per_topic'][qid]}
        row['delta_ndcg_contextbase_minus_weknora'] = row['contextbase']['ndcg_at_10'] - row['weknora']['ndcg_at_10']
        details.append(row)
    summary = {}
    for p in runs:
        summary[p] = {'queries': len(details),
                      'first_rank_misses_with_positive_in_top10': sum(1 < r[p]['first_positive_rank'] <= 10 for r in details),
                      'queries_with_no_positive_in_top10': sum(r[p]['first_positive_rank'] is None or r[p]['first_positive_rank'] > 10 for r in details),
                      'queries_missing_any_judged_positive': sum(bool(r[p]['missing_positive_ids']) for r in details),
                      'missing_positive_judgments': sum(len(r[p]['missing_positive_ids']) for r in details)}
    diffs = [r['delta_ndcg_contextbase_minus_weknora'] for r in details]
    rng = random.Random(20261112)
    samples = sorted(statistics.mean(rng.choices(diffs, k=len(diffs))) for _ in range(10000))
    b = sum(r['contextbase']['hit_at_1'] > r['weknora']['hit_at_1'] for r in details)
    c = sum(r['contextbase']['hit_at_1'] < r['weknora']['hit_at_1'] for r in details)
    pvalue = min(1., 2 * sum(math.comb(b + c, k) for k in range(min(b, c) + 1)) / 2 ** (b + c))
    summary['exploratory_statistics'] = {'ndcg_paired_difference': statistics.mean(diffs),
        'paired_query_bootstrap_95_percentile_ci': [samples[249], samples[9749]],
        'bootstrap_draws':10000,'seed':20261112,'hit1_discordant_c_only':b,'hit1_discordant_w_only':c,
        'mcnemar_exact_two_sided_unadjusted_p':pvalue,
        'scope':'Exploratory on the already inspected 393 dev queries; query bootstrap assumes independent queries; no multiplicity adjustment or unseen-test claim.'}
    summary['attribution_limit'] = 'Returned rankings show ordering opportunities and missing positives, but cannot alone distinguish pre-rerank recall loss from rerank cutoff or final filtering. Native traces are required.'
    (output / 'ranking-analysis.json').write_text(json.dumps({'dataset':source['dataset'],'summary':summary,'per_query':details},ensure_ascii=False,indent=2)+'\n')
    with (output / 'ranking-cases.csv').open('w',newline='') as f:
        w = csv.writer(f);w.writerow(['qid','query','c_first_positive','w_first_positive','c_ndcg','w_ndcg','c_missing_positives','w_missing_positives'])
        for r in sorted(details,key=lambda r:abs(r['delta_ndcg_contextbase_minus_weknora']),reverse=True):
            w.writerow([r['qid'],r['query'],r['contextbase']['first_positive_rank'],r['weknora']['first_positive_rank'],r['contextbase']['ndcg_at_10'],r['weknora']['ndcg_at_10'],' '.join(r['contextbase']['missing_positive_ids']),' '.join(r['weknora']['missing_positive_ids'])])
    selected = sorted(details,key=lambda r:abs(r['delta_ndcg_contextbase_minus_weknora']),reverse=True)[:4]
    selected += [r for r in details if r['contextbase']['missing_positive_ids'] or r['weknora']['missing_positive_ids']]
    (output / 'diagnostic-qids.json').write_text(json.dumps(list(dict.fromkeys(r['qid'] for r in selected))[:12],indent=2)+'\n')
    return summary

if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data',type=Path,required=True);p.add_argument('--results',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();print(json.dumps(analyze(a.data,a.results,a.output),ensure_ascii=False,indent=2))
