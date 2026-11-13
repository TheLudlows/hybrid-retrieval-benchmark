"""Native trace diagnosis of existing hard queries; not a new scored benchmark."""
import argparse
from collections import Counter
import csv
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import time

import psycopg2
from psycopg2 import sql
import bench
from run_scale import save
from verify_contextbase_configs import ExtraWorker


def unique_paragraphs(nodes,mapping):
    return list(dict.fromkeys(mapping[n] for n in nodes if n in mapping))


def rank_of(paragraph,ranked):
    return ranked.index(paragraph)+1 if paragraph in ranked else None


def list_quality(ranked,labels):
    relevant=sum(labels.values());hits=[labels.get(p,0) for p in ranked]
    positions=[i for i,p in enumerate(ranked,1) if labels.get(p,0)]
    return {'first_positive_rank':min(positions) if positions else None,'hit_at_1':int(bool(hits[:1] and hits[0])),'hit_at_10':int(any(hits[:10])),'recall_at_10':sum(hits[:10])/relevant,'ndcg_at_10':sum(v/math.log2(i+2) for i,v in enumerate(hits[:10]))/sum(1/math.log2(i+2) for i in range(min(relevant,10))),'official_depth':len(ranked)}


def explain(response,mapping,labels):
    t=response['trace'];arms={}
    for arm in t['retrieval_results']:
        arms.setdefault(arm['method_name'],[]).extend(arm['results'])
    ordered={name:sorted(rows,key=lambda row:row['score'],reverse=True) for name,rows in arms.items()}
    raw={name:unique_paragraphs([r['node_id'] for r in rows],mapping) for name,rows in ordered.items()}
    fusion=unique_paragraphs([r['node_id'] for r in t['rrf_merged']],mapping)
    candidates=unique_paragraphs([r['node_id'] for r in t['reranked']],mapping)
    final=list(dict.fromkeys(r['document_id'] for r in response['results']))
    phase={r['phase_name']:r['details'] for r in t['summary']['phase_metrics']}
    assert phase['reranking']['reranker_type']=='rrf-passthrough'
    assert not arms.get('graph') and not arms.get('temporal')
    positive=[]
    for p,v in labels.items():
        if not v:continue
        ranks={name:rank_of(p,ranked) for name,ranked in raw.items()};fr=rank_of(p,fusion);cr=rank_of(p,candidates);out=rank_of(p,final)
        if out:category='returned_top1' if out==1 else 'returned_2_to_10' if out<=10 else 'returned_below_top10'
        elif cr:category='return_filter_with_token_truncation' if phase.get('token_filtering',{}).get('truncated') else 'other_final_filter'
        elif fr:category='outside_fusion_top60'
        elif any(r is not None for r in ranks.values()):category='outside_per_source_fusion_cap'
        else:category='not_in_recorded_raw_arms'
        positive.append({'passage_id':p,'raw_arm_official_ranks':ranks,'fusion_official_rank':fr,'post_scoring_candidate_official_rank':cr,'final_official_rank':out,'classification':category})
    qualities={name:list_quality(ranked,labels) for name,ranked in raw.items()};qualities.update(fusion=list_quality(fusion,labels),post_scoring_candidates=list_quality(candidates,labels),final=list_quality(final,labels))
    by_node={r['node_id']:r for r in t['rrf_merged']}
    top=response['results'][0] if response['results'] else None
    def evidence(row):
        if row is None:return None
        p=mapping.get(row['node_id']);src=row['source_ranks'];return {'passage_id':p,'node_id':row['node_id'],'judgment':labels.get(p),'rrf_score':row['rrf_score'],'fusion_unit_rank':row['final_rrf_rank'],'source_unit_ranks':src,'rrf_contributions':{k:1/(60+v) for k,v in src.items()}}
    positive_rows=[r for r in t['rrf_merged'] if labels.get(mapping.get(r['node_id']),0)]
    best=evidence(positive_rows[0]) if positive_rows else None
    first=evidence(by_node.get(top['id'])) if top else None
    return {'raw_arm_unit_depths':{name:len(rows) for name,rows in ordered.items()},'post_scoring_candidate_unit_count':len(t['reranked']),'stage_quality':qualities,'positive_evidence':positive,'first_returned_evidence':first,'best_positive_fusion_evidence':best,'token_filter':phase.get('token_filtering',{}),'trace_reranking_label':'rrf-passthrough (no model); trace.reranked is a native candidate list, not proof of model invocation','final_passage_ids':final}


class DiagnosticWorker(ExtraWorker):
    def render(self):
        (self.out/'100k/README.md').write_text('# 原生阶段诊断\n\n本目录不生成新的正式跑分。见根目录REPORT.md、analysis.json、逐题summary和压缩trace。诊断耗时不计入评测。\n')

    def main(self):
        original=self.plan['restore_case'];case_summaries={};oldcontext=(self.measure/'measurement-context.json').read_bytes()
        try:
            selected_path=self.out/'selected-queries.json'
            if hashlib.sha256(selected_path.read_bytes()).hexdigest()!=self.plan['source_selection_sha256']:raise RuntimeError('Selected query fingerprint mismatch')
            self.code_hashes[str(selected_path.relative_to(self.repo))]=self.plan['source_selection_sha256']
            backup=self.work/'measurement-context-before-fusion-diagnostic.json'
            if not backup.exists():backup.write_bytes(oldcontext)
            data=Path(self.s['data100k']);topics,qrels=bench.topics_and_qrels(data);selection=json.loads((self.out/'selected-queries.json').read_text());qids=[r['qid'] for r in selection]
            with psycopg2.connect(self.dburl('100k')) as conn,conn.cursor() as cur:
                cur.execute(sql.SQL('SELECT id,document_id FROM {}.memory_units').format(sql.Identifier('ctxdb_ws_miracl_scale_100k')));mapping={str(k):v for k,v in cur.fetchall()}
            with (self.out/'node-to-official.csv').open('w') as f:
                w=csv.writer(f);w.writerow(['node_id','official_passage_id']);w.writerows(sorted(mapping.items()))
            for case in self.plan['diagnostic_cases']:
                self.start('contextbase','100k',case);api=bench.prior_api('contextbase');folder=self.out/'traces'/case['name'];folder.mkdir(parents=True,exist_ok=True)
                runid=self.run_prefix+'100k/contextbase/'+case['name'];rows=[]
                archived_path=self.repo/self.plan['comparison_rankings'][case['name']];archived={r['qid']:r['passages'] for r in map(json.loads,archived_path.read_text().splitlines())}
                for qid in qids:
                    self.check_code()
                    self.status(state='diagnosing',case=case['name'],qid=qid,diagnostic_queries_completed=len(rows),diagnostic_queries_target=len(qids),run_id=runid)
                    target=folder/(qid.replace('#','-')+'.json.gz')
                    if target.exists():d=json.load(gzip.open(target,'rt'))
                    else:
                        save(self.measure/'measurement-context.json',{'contextbase':{'phase':'diagnostic','id':qid,'run_id':runid}})
                        d=api.request('/memories/recall',{'query':topics[qid],'budget':'high','types':['world','experience'],'trace':True,'max_tokens':8192,'include':{'entities':None,'chunks':{'max_tokens':16384}}})
                        temp=target.with_suffix('.gz.tmp')
                        with temp.open('wb') as raw:
                            with gzip.GzipFile(fileobj=raw,mode='wb') as zipped:zipped.write(json.dumps(d,ensure_ascii=False).encode())
                            raw.flush();os.fsync(raw.fileno())
                        temp.replace(target)
                    row={'qid':qid,'query':topics[qid],**explain(d,mapping,qrels[qid])};row['matches_archived_full_ranking']=row['final_passage_ids']==archived[qid];rows.append(row)
                    save(self.out/('summary-'+case['name']+'.json'),rows);print('Diagnosed',case['name'],len(rows),qid,flush=True)
                usage=self.archive_calls('contextbase',runid,self.out/('model-calls-'+case['name']+'.jsonl.gz'))
                if any(v.get('calls',0) for k,v in usage.items() if k.endswith('/rerank')):raise RuntimeError('Unexpected model rerank during trace diagnosis')
                if usage.get('diagnostic/embedding',{}).get('calls',0)<len(qids):raise RuntimeError('Missing model usage evidence')
                save(self.out/('model-usage-'+case['name']+'.json'),usage);case_summaries[case['name']]=rows
                self.publish('Archive native stage traces for '+case['name'])
            self.make_report(case_summaries)
        except Exception as e:
            self.failed_error=(type(e).__name__,str(e));self.status(state='failed',error_type=type(e).__name__,error=str(e));raise
        finally:
            self.start('contextbase','100k',original)
            save(self.measure/'measurement-context.json',{'contextbase':{'phase':'idle','id':'none','run_id':'post-fusion-diagnostic-idle'}})
            if hasattr(self,'failed_error'):self.status(state='failed',error_type=self.failed_error[0],error=self.failed_error[1],restored_case=original['name'])
        self.status(state='complete',diagnostic_queries=30,diagnostic_native_requests=60,restored_case=original['name']);self.publish('Complete fusion-stage diagnosis and restore original active configuration')

    def make_report(self,cases):
        baseline=cases['lean_control'];stats={};flat=[]
        for name,rows in cases.items():
            count=Counter(e['classification'] for r in rows for e in r['positive_evidence']);demote1=[];demote10=[]
            for r in rows:
                q=r['stage_quality'];best=min((q[a]['first_positive_rank'] or 10**9 for a in ('semantic','bm25')),default=10**9)
                if best==1 and q['fusion']['first_positive_rank']!=1:demote1.append(r['qid'])
                if best<=10 and (q['fusion']['first_positive_rank'] or 10**9)>10:demote10.append(r['qid'])
                for e in r['positive_evidence']:flat.append({'case':name,'qid':r['qid'],'query':r['query'],'passage_id':e['passage_id'],'semantic_rank':e['raw_arm_official_ranks'].get('semantic'),'bm25_rank':e['raw_arm_official_ranks'].get('bm25'),'fusion_rank':e['fusion_official_rank'],'candidate_rank':e['post_scoring_candidate_official_rank'],'final_rank':e['final_official_rank'],'classification':e['classification']})
            stats[name]={'queries':len(rows),'matches_archived_rankings':sum(r['matches_archived_full_ranking'] for r in rows),'positive_classifications':dict(count),'raw_top1_positive_demoted_by_fusion':demote1,'raw_top10_positive_demoted_by_fusion':demote10,'stage_metrics':{stage:{key:sum(r['stage_quality'][stage][key] for r in rows)/len(rows) for key in ['hit_at_1','hit_at_10','recall_at_10','ndcg_at_10']} for stage in ['semantic','bm25','fusion','post_scoring_candidates','final']}}
        save(self.out/'analysis.json',{'cases':stats,'scope':'30 selected hard queries from fixed tune100; not representative averages or new test scores. Raw-arm metrics are diagnostic projections, not separately executed native single-retriever benchmarks.','trace_semantics':'No model rerank. Native trace.reranked names a passthrough candidate/scoring stage.','classifications_note':'Missing from raw arms means absent from recorded pools, not necessarily absent from index. Candidate-to-final loss is labelled with token truncation evidence, not assumed to be caused solely by token budget.'})
        with (self.out/'positive-stage-ranks.csv').open('w') as f:
            w=csv.DictWriter(f,fieldnames=list(flat[0]));w.writeheader();w.writerows(flat)
        lines=['# ContextBase 向量、BM25与融合阶段诊断','', '选自固定100道调参题的30道错题：先覆盖前十无正例的题，再按相对WeKnora的nDCG缺口补满。这是有意选择的难例，不代表全部393题的平均表现。不改原成绩、不重新选参。','', '两配置仅相差每支路进入融合前是否截到100。原生向量/BM25仍先各按high预算检索，原生trace保存其支路列表、RRF列表、60候选及最终返回。诊断耗时不计入正式性能。','', '| 配置/阶段 | Hit@1 | Hit@10 | Recall@10 | nDCG@10 |','| --- | ---: | ---: | ---: | ---: |']
        for name,s in stats.items():
            for stage,d in s['stage_metrics'].items():lines.append(f'| {name}/{stage} | {d["hit_at_1"]:.2%} | {d["hit_at_10"]:.2%} | {d["recall_at_10"]:.2%} | {d["ndcg_at_10"]:.6f} |')
        lines+=['', 'raw-arm指标仅从trace列表映射官方段落ID、去重后计算，不是修改产品开关后的单路正式跑分。fusion是候选截断前的原生RRF列表。post_scoring_candidates是未调用模型的60候选及原生综合评分后的列表；最终Token过滤和官方段落去重仍会改变深度。','', '## 断点统计','']
        for name,s in stats.items():lines += [f'### {name}', '', f'- 至少一条原始支路正例第1、融合后非第1：{len(s["raw_top1_positive_demoted_by_fusion"])}题。', f'- 至少一条原始支路正例前10、融合后不在前10：{len(s["raw_top10_positive_demoted_by_fusion"])}题。',f'- 与已归档最终完整排名一致：{s["matches_archived_rankings"]}/{s["queries"]}题。',f'- 逐正例分类：`{json.dumps(s["positive_classifications"],ensure_ascii=False)}`。','']
        sample=[r for r in baseline if r['qid'] in stats['lean_control']['raw_top1_positive_demoted_by_fusion']][:5]
        lines+=['## 融合降序实例','', '| 查询 | 正例段落ID | 语义位次 | BM25位次 | 融合位次 | 最终位次 |','| --- | --- | ---: | ---: | ---: | ---: |']
        for row in sample:
            e=min(row['positive_evidence'],key=lambda e:min((v for v in e['raw_arm_official_ranks'].values() if v is not None),default=10**9));lines.append(f'| {row["query"]} | {e["passage_id"]} | {e["raw_arm_official_ranks"].get("semantic")} | {e["raw_arm_official_ranks"].get("bm25")} | {e["fusion_official_rank"]} | {e["final_official_rank"]} |')
        lines+=['', '完整证据见 [analysis.json](analysis.json)、[逐正例位次CSV](positive-stage-ranks.csv) 及各配置summary文件；压缩trace配合 [node-to-official.csv](node-to-official.csv) 可复算。RRF贡献在每题first_returned_evidence及best_positive_fusion_evidence中，使用原生1/(60+rank)计算。未标注为正例不等同证明语义不相关。','', '所有模型记录按diagnostic独立run ID保存，模型重排调用必须为0。最终恢复诊断前的lean_source_cap100配置，保留两端全部原生数据库、索引、配置、检查点及备份。仅依据此次选择难例定位问题，不据此宣称哪种权重或分词方案能在全体查询上提升；没有执行第二项权重扫描。']
        (self.out/'REPORT.md').write_text('\n'.join(lines)+'\n')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--settings',type=Path,required=True);p.add_argument('--results',type=Path,required=True);a=p.parse_args();DiagnosticWorker(a.settings,a.results).main()
