"""Collect native stage evidence for selected queries; timings are diagnostic only."""
import argparse
import gzip
import json
import os
from pathlib import Path

from bench import prior_api, topics_and_qrels


def diagnose(data, qids_path, output, measurement_dir):
    import psycopg2
    api=prior_api('contextbase');topics,qrels=topics_and_qrels(data)
    schema='ctxdb_ws_'+api.scope
    if not __import__('re').fullmatch('[a-z0-9_]+',schema):raise ValueError('Invalid scope')
    with psycopg2.connect(os.environ['AUDIT_POSTGRES_URL']) as conn,conn.cursor() as cur:
        cur.execute(f'SELECT id,document_id FROM "{schema}".memory_units')
        mapping={str(i):p for i,p in cur.fetchall()}
    output.mkdir(parents=True,exist_ok=True);summary=[]
    for qid in json.loads(qids_path.read_text()):
        target=output/(qid.replace('#','-')+'.json.gz')
        if target.exists():
            with gzip.open(target,'rt') as f:d=json.load(f)
        else:
            ctx=measurement_dir/'measurement-context.json';tmp=ctx.with_suffix('.tmp');tmp.write_text(json.dumps({'contextbase':{'phase':'diagnostic','id':qid,'run_id':'scale-plan-10k-diagnostic'}}));tmp.replace(ctx)
            d=api.request('/memories/recall',{'query':topics[qid],'budget':'high','types':['world','experience'],'trace':True,'max_tokens':8192,'include':{'entities':None,'chunks':{'max_tokens':16384}}})
            with gzip.open(target,'wt',encoding='utf-8') as f:json.dump(d,f,ensure_ascii=False)
        t=d['trace'];arms={}
        for arm in t['retrieval_results']:
            entries=arms.setdefault(arm['method_name'],{})
            for i,row in enumerate(arm['results'],1):
                p=mapping.get(row['node_id'])
                if p:entries[p]=min(entries.get(p,10**9),i)
        model_ids={mapping[r['node_id']] for r in t['reranked'] if r['node_id'] in mapping}
        final=list(dict.fromkeys(r['document_id'] for r in d['results']))
        positive=[]
        for p,label in qrels[qid].items():
            if not label:continue
            final_rank=final.index(p)+1 if p in final else None
            state=('returned' if final_rank==1 else 'ordering' if final_rank else 'filtered_after_rerank' if p in model_ids else 'outside_model_candidate60' if any(p in x for x in arms.values()) else 'not_in_recorded_recall_arms')
            positive.append({'passage_id':p,'arm_ranks':{k:v[p] for k,v in arms.items() if p in v},'sent_to_model':p in model_ids,'final_rank':final_rank,'classification':state})
        components=[r.get('score_components',{}) for r in t['reranked']]
        summary.append({'qid':qid,'query':topics[qid],'positive_evidence':positive,'recency_values':sorted({r['recency'] for r in components if 'recency' in r}),'temporal_values':sorted({r['temporal'] for r in components if 'temporal' in r}),'model_candidate_chunks':len(t['reranked']),'returned_official_depth':len(final)})
        (output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
        print('Diagnosed',len(summary),qid,flush=True)
    return summary

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('data','qids','output','measurement-dir'):p.add_argument('--'+name,type=Path,required=True)
    a=p.parse_args();diagnose(a.data,a.qids,a.output,a.measurement_dir)
