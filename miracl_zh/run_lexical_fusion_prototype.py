"""Isolated experimental ContextBase BM25 query policies and true weighted RRF."""
import argparse
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import sys
import time
from unittest.mock import patch

import psycopg2
import bench
from run_scale import save
from verify_contextbase_configs import ExtraWorker


class PrototypeWorker(ExtraWorker):
    def check_code(self):
        super().check_code()
        for name,digest in ({} if getattr(self,'restoring',False) else self.plan['prototype_source_sha256']).items():
            if hashlib.sha256((Path(self.plan['prototype_source'])/name).read_bytes()).hexdigest()!=digest:raise RuntimeError('Prototype source changed: '+name)
        for name,digest in self.plan['original_source_sha256'].items():
            if hashlib.sha256((Path(self.plan['original_source'])/name).read_bytes()).hexdigest()!=digest:raise RuntimeError('Original source changed: '+name)

    def probe(self,case,restored=False):
        topics,qrels=bench.topics_and_qrels(Path(self.s['data100k']));qid=next(q for q,t in topics.items() if t=='哥斯达黎加的国歌是什么？');query=topics[qid]
        runid=self.run_prefix+'probe/'+case['name'];save(self.measure/'measurement-context.json',{'contextbase':{'phase':'prototype_probe','id':qid,'run_id':runid}})
        d=bench.prior_api('contextbase').request('/memories/recall',{'query':query,'budget':'high','types':['world','experience'],'trace':True,'max_tokens':8192,'include':{'entities':None,'chunks':{'max_tokens':16384}}})
        with gzip.open(self.out/('probe-'+case['name']+'.json.gz'),'wt') as f:json.dump(d,f,ensure_ascii=False)
        env=case['env'];weights={'semantic_rank':float(env['EXPERIMENT_RRF_VECTOR_WEIGHT']),'bm25_rank':float(env['EXPERIMENT_RRF_KEYWORD_WEIGHT'])};t=d['trace']
        assert t['query']['query_text']==query
        for row in t['rrf_merged']:
            expected=sum(weights[k]/(60+v) for k,v in row['source_ranks'].items())
            if abs(expected-row['rrf_score'])>1e-12:raise RuntimeError('Native API did not use expected fusion weights')
        vector_hash=hashlib.sha256(json.dumps(t['query']['query_embedding']).encode()).hexdigest();baseline=self.out/'probe-stock_equivalent.json'
        if baseline.exists() and vector_hash!=json.loads(baseline.read_text())['query_embedding_sha256']:raise RuntimeError('BM25 policy changed query embedding')
        if restored:
            expanded=query;stopwords=[]
        else:
            spec=importlib.util.spec_from_file_location('isolated_bm25_probe',Path(self.plan['prototype_source'])/'hindsight-api-slim/hindsight_api/_benchmark_retrieval.py');helper=importlib.util.module_from_spec(spec);sys.modules[spec.name]=helper;spec.loader.exec_module(helper)
            with patch.dict(os.environ,{'CONTEXTBASE_API_'+k:v for k,v in env.items()}):
                expanded=helper.query_text(query);stopwords=list(helper.STOPWORDS) if helper.settings().policy!='native' else []
        with psycopg2.connect(self.dburl('100k')) as c,c.cursor() as q:
            q.execute("SELECT t FROM unnest(%s::text::pdb.jieba::text[]) WITH ORDINALITY u(t,o) WHERE t !~ '^[[:space:][:punct:]]*$' AND t <> ALL(%s::text[]) GROUP BY t ORDER BY char_length(t)>1 DESC,min(o) LIMIT 16",(expanded,stopwords));terms=[v[0] for v in q.fetchall()]
            q.execute('SELECT id,document_id,text::pdb.jieba::text[] FROM ctxdb_ws_miracl_scale_100k.memory_units WHERE bank_id=%s AND document_id=ANY(%s)',('miracl_scale_100k',[p for p,v in qrels[qid].items() if v]));positive_units={str(row[0]):{'official_id':row[1],'document_tokens':row[2]} for row in q.fetchall()}
        bm25={row['node_id']:row['rank'] for arm in t['retrieval_results'] if arm['method_name']=='bm25' for row in arm['results']}
        evidence={'case':case,'experimental_code':not restored,'query_id':qid,'original_query':query,'expected_bm25_query_only':expanded,'expected_selected_query_terms':terms,'query_embedding_sha256':vector_hash,'native_weight_formula_verified_entries':len(t['rrf_merged']),'positive_units':{k:{**v,'native_bm25_rank':bm25.get(k),'matched_selected_terms':sorted(set(v['document_tokens'])&set(terms))} for k,v in positive_units.items()},'final_official_ids':list(dict.fromkeys(x['document_id'] for x in d['results']))}
        if restored:
            reference={r['qid']:r['passages'] for r in map(json.loads,(self.repo/self.plan['stock_control_rankings']).read_text().splitlines())}
            if evidence['final_official_ids']!=reference[qid]:raise RuntimeError('Restored stock probe differs from archived stock ranking')
        save(self.out/('probe-'+case['name']+'.json'),evidence);usage=self.archive_calls('contextbase',runid,self.out/('model-calls-probe-'+case['name']+'.jsonl.gz'))
        if any(v.get('calls',0) for k,v in usage.items() if k.endswith('/rerank')):raise RuntimeError('Probe unexpectedly called rerank model')
        save(self.out/('model-usage-probe-'+case['name']+'.json'),usage)

    def main(self):
        original_source=self.s['contextbase_source'];original=self.plan['restore_case'];failure=None
        try:
            self.s['contextbase_source']=self.plan['prototype_source'];data=Path(self.s['data100k']);checkpoint=self.runtime/'contextbase-100k/ingest.sqlite';candidates=[]
            for i,case in enumerate(self.plan['contextbase_trials']):
                self.start('contextbase','100k',case)
                if i==0:self.preflight_no_rerank('contextbase','100k',data,checkpoint,case)
                self.probe(case)
                result=self.capture('contextbase','100k',data,checkpoint,case,'tune_dev100');candidates.append((case,result))
                if i==0:
                    existing={r['qid']:r['passages'] for r in map(json.loads,(self.repo/self.plan['stock_control_rankings']).read_text().splitlines())};current={r['qid']:r['passages'] for r in map(json.loads,(self.out/'100k/contextbase/stock_equivalent-tune_dev100-cap60/rankings.jsonl').read_text().splitlines())}
                    if existing!=current:raise RuntimeError('Disabled prototype does not reproduce all stock control rankings')
                    save(self.out/'stock-equivalence.json',{'queries':len(current),'identical_full_rankings':len(current),'all_experimental_controls_disabled':True})
                self.publish('Archive experimental BM25/fusion '+case['name']+' tune100')
            chosen=max(enumerate(candidates),key=lambda z:(z[1][1]['ndcg_at_10'],z[1][1]['hit_at_1'],-z[0]))[1][0]
            save(self.out/'100k/contextbase-frozen-selection.json',{'case':chosen,'rule':self.plan['selection'],'scope':'Exploratory experimental fork; not stock ContextBase. No selection on dev393 or extra200.'});self.publish('Freeze experimental BM25/fusion configuration from tune100 only')
            self.start('contextbase','100k',chosen)
            for qset in ('dev393','fresh_train200'):self.capture('contextbase','100k',data,checkpoint,chosen,qset)
            self.derive_assessment('contextbase','100k',data,chosen)
            self.results(candidates,chosen)
        except Exception as e:failure=e
        finally:
            try:
                self.restoring=True;self.s['contextbase_source']=original_source;self.start('contextbase','100k',original);self.probe(original,restored=True)
                envfile=self.runtime/'contextbase-100k/api.env';lines=[line for line in envfile.read_text().splitlines() if not line.startswith(('CONTEXTBASE_API_EXPERIMENT_','HINDSIGHT_API_EXPERIMENT_'))];envfile.write_text('\n'.join(lines)+'\n');envfile.chmod(0o600)
                save(self.measure/'measurement-context.json',{'contextbase':{'phase':'idle','id':'none','run_id':'post-lexical-prototype-idle'}})
                save(self.out/'restored-stock.json',{'original_source':original_source,'configuration':'lean_source_cap100','native_probe_matches_stock_equivalent':True,'experimental_env_keys_removed_from_persistent_api_env':True,'both_service_data_retained':True})
            except Exception as restore_error:
                self.status(state='failed',error_type=type(restore_error).__name__,error=str(restore_error),restoration_failed=True);raise
        if failure:
            self.status(state='failed',error_type=type(failure).__name__,error=str(failure),stock_restored=True);raise failure
        self.status(state='complete',stock_restored=True,experimental_study=True,million_stage='cancelled_by_user');self.publish('Complete experimental lexical/fusion validation and restore stock service')

    def results(self,candidates,chosen):
        old=self.repo/self.plan['primary_results'];follow=self.repo/self.plan['followup_results'];a=json.loads((self.out/'100k/contextbase'/(chosen['name']+'-dev393-cap60')/'metrics.json').read_text());b=json.loads((follow/'100k/contextbase/lean_source_cap100-dev393-cap60/metrics.json').read_text());wkname=json.loads((old/'100k/weknora-frozen-selection.json').read_text())['case']['name'];wk=json.loads((old/'100k/weknora'/(wkname+'-dev393-cap60')/'metrics.json').read_text())
        save(self.out/'comparison.json',{'selected_case':chosen,'tuning':{c['name']:d for c,d in candidates},'experimental_dev393':a,'stock_contextbase_dev393':b,'stock_weknora_dev393':wk,'scope':'Extra code-level experiment; original stock product scores and original equal-budget comparison remain unchanged.'})
        lines=['# BM25中文查询与加权RRF实验版结果','', '**本轮使用独立源码副本中的实验扩展，不能视为原版ContextBase已有配置或原版产品成绩。**原版源代码、索引和此前评分均保留。','', '| 固定100题配置 | Hit@1 | nDCG@10 | Recall@10 | p95秒 |','| --- | ---: | ---: | ---: | ---: |']
        for c,d in candidates:lines.append(f'| {c["name"]} | {d["hit_at_1"]:.2%} | {d["ndcg_at_10"]:.6f} | {d["recall_at_10"]:.2%} | {d["serial_p95_ms"]/1000:.3f} |')
        lines+=['',f'仅按固定100题冻结：**{chosen["name"]}**。以下393题/额外200题已有历史其他配置成绩，仅属探索性复核。','', '| 393题参照 | Hit@1 | nDCG@10 | Recall@10 | Recall@100 |','| --- | ---: | ---: | ---: | ---: |']
        for name,d in [('本轮实验版',a),('原版ContextBase已有配置',b),('原版WeKnora选定配置',wk)]:lines.append(f'| {name} | {d["hit_at_1"]:.2%} | {d["ndcg_at_10"]:.6f} | {d["recall_at_10"]:.2%} | {d["recall_at_100"]:.2%} |')
        lines+=['', '不同时间段、不同额外调参预算及代码版本的结果仅作明确标注的参照，不能据此改写原来原版产品公平对比结论。完整逐题排名、实际模型用量、实验配置、源码补丁及指纹均保留。原始查询继续用于BGE-M3，繁简扩展只修改BM25参数；所有模型重排调用必须为0。最终恢复原版服务和原版配置，不删除两端数据。']
        (self.out/'RESULTS.md').write_text('\n'.join(lines)+'\n')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--settings',type=Path,required=True);p.add_argument('--results',type=Path,required=True);a=p.parse_args();PrototypeWorker(a.settings,a.results).main()
