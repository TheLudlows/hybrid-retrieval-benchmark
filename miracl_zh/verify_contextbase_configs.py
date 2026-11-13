"""Post-primary exploratory verification of two configuration-only ContextBase combinations."""
import argparse
import json
import os
from pathlib import Path
import shlex
import sqlite3
import time

from audit_index import audit
from run_scale import save, http
from run_scale_no_rerank import NativeWorker
from argparse import Namespace


class ExtraWorker(NativeWorker):
    def start(self,product,scale,case,cap=60):
        super().start(product,scale,case,cap)
        r=self.runtime/(product+'-'+scale)
        cfg=http(self.connection['CONTEXTBASE_URL']+'/v1/default/banks/'+self.connection['CONTEXTBASE_BANK_ID']+'/config',key=json.loads((r/'admin-key.json').read_text())['secret'])['config']
        for key in ('enable_graph_retrieval','enable_temporal_retrieval','enable_reranking'):
            if cfg.get(key) is not False:raise RuntimeError('Native effective configuration not disabled: '+key)
        expected={'RECALL_STRATEGY_BOOSTS':'','RECALL_MAX_CANDIDATES_PER_SOURCE':'0','RECALL_BUDGET_FIXED_HIGH':'1000',**case['env']}
        env={}
        for line in (r/'api.env').read_text().splitlines():
            if '=' in line:
                key,value=line.split('=',1)
                if key.startswith('CONTEXTBASE_API_') and key.removeprefix('CONTEXTBASE_API_') in expected:
                    tokens=shlex.split(value);env[key.removeprefix('CONTEXTBASE_API_')]=tokens[0] if tokens else ''
        for key,value in expected.items():
            if env.get(key)!=value:raise RuntimeError('Persisted native environment readback mismatch: '+key)
        save(self.out/'100k'/('readback-'+case['name']+'-cap'+str(cap)+'.json'),{'native_bank_config':{key:cfg[key] for key in ('enable_graph_retrieval','enable_temporal_retrieval','enable_reranking')},'native_api_env':env,'source':'Native bank GET and startup-generated api.env; no credentials'})

    def main(self):
        try:
            data=Path(self.s['data100k']);scale='100k';product='contextbase';cases=self.plan['contextbase_trials'];base=cases[0]
            self.start(product,scale,base)
            checkpoint=self.runtime/'contextbase-100k/ingest.sqlite'
            os.environ['AUDIT_POSTGRES_URL']=self.dburl(scale)
            audit(Namespace(data=data,checkpoint=checkpoint,product=product,weknora_db=None,output=self.out/scale/'contextbase-index-audit.json'))
            self.preflight_no_rerank(product,scale,data,checkpoint,base)
            candidates=[]
            for i,case in enumerate(cases):
                if i:self.start(product,scale,case)
                candidates.append((case,self.capture(product,scale,data,checkpoint,case,'tune_dev100')))
                self.publish('Archive extra ContextBase combination '+case['name']+' tune100')
            chosen=max(enumerate(candidates),key=lambda z:(z[1][1]['ndcg_at_10'],z[1][1]['hit_at_1'],-z[0]))[1][0]
            save(self.out/scale/'contextbase-frozen-selection.json',{'case':chosen,'selection_only':'tune_dev100','rule':self.plan['selection'],'note':'Existing dev393 and extra200 results already inspected earlier. All follow-up results are exploratory, not blind validation.'})
            self.publish('Freeze extra ContextBase combination before rerunning assessment queries')
            self.start(product,scale,chosen)
            for qset in ('dev393','fresh_train200'):self.capture(product,scale,data,checkpoint,chosen,qset)
            self.derive_assessment(product,scale,data,chosen)
            self.report(candidates,chosen)
            self.status(state='complete',completed_scales=[100000],million_stage='cancelled_by_user')
            self.publish('Complete additional ContextBase configuration-only verification')
        except Exception as e:
            self.status(state='failed',error_type=type(e).__name__,error=str(e));raise

    def report(self,candidates,chosen):
        metrics={case['name']:result for case,result in candidates}
        selected=json.loads((self.out/'100k/contextbase'/(chosen['name']+'-dev393-cap60')/'metrics.json').read_text())
        primary=self.repo/self.plan['primary_results'];previous=json.loads((primary/'100k/contextbase/lean-dev393-cap60/metrics.json').read_text())
        weknora=json.loads((primary/'100k/weknora-frozen-selection.json').read_text())['case']['name']
        wk=json.loads((primary/'100k/weknora'/(weknora+'-dev393-cap60')/'metrics.json').read_text())
        keys=['hit_at_1','hit_at_5','hit_at_10','ndcg_at_10','recall_at_10','recall_at_100','serial_p50_ms','serial_p95_ms','serial_p99_ms','returned_depth']
        save(self.out/'comparison.json',{'tuning_results':metrics,'chosen':chosen,'selected_dev393':{k:selected[k] for k in keys},'previous_lean_dev393':{k:previous[k] for k in keys},'previous_weknora_selected':weknora,'previous_weknora_dev393':{k:wk[k] for k in keys},'delta_vs_previous_lean':{k:selected[k]-previous[k] for k in keys if k!='returned_depth'},'fairness':'This is an additional ContextBase-only exploration, separate from the primary equal-six-configuration comparison. Existing dev and extra200 queries have already been evaluated.'})
        lines=['# ContextBase 配置组合追加验证结果','', '本轮仅使用原生配置，无模型重排。它是原先每端6组公平预算实验之后追加的探索，不能当成仍然相同选参预算的产品比较。393题和额外200题此前已检查，不作盲测承诺。','', '| 100题配置 | Hit@1 | nDCG@10 | Recall@10 | p95秒 |','| --- | ---: | ---: | ---: | ---: |']
        for case,d in candidates:lines.append(f'| {case["name"]} | {d["hit_at_1"]:.2%} | {d["ndcg_at_10"]:.6f} | {d["recall_at_10"]:.2%} | {d["serial_p95_ms"]/1000:.3f} |')
        lines+=['',f'仅按固定100题选择：**{chosen["name"]}**。','', '| 393题参照 | Hit@1 | nDCG@10 | Recall@10 |','| --- | ---: | ---: | ---: |']
        for name,d in [('此前ContextBase lean',previous),('本轮选定配置',selected),('此前WeKnora '+weknora,wk)]:lines.append(f'| {name} | {d["hit_at_1"]:.2%} | {d["ndcg_at_10"]:.6f} | {d["recall_at_10"]:.2%} |')
        lines+=['', '不同时间窗口的耗时仅作描述，不把此前参照当成同期配对性能实验。完整逐题排名、配置指纹、实际模型用量和原生配置读回均在100k目录。所有正式及预热重排调用必须为零。两端索引、数据库、私有连接配置及备份保留，百万阶段不执行。']
        (self.out/'RESULTS.md').write_text('\n'.join(lines)+'\n')


def wait_primary(settings,primary):
    work=Path(json.loads(settings.read_text())['work'])
    while True:
        state=json.loads((work/'status.json').read_text());pid=state['pid']
        try:
            command=Path('/proc',str(pid),'cmdline').read_bytes()
            running=b'run_scale_no_rerank.py' in command
        except OSError:running=False
        if not running:
            completed=primary/'execution-status.json'
            proof=json.loads(completed.read_text()) if completed.exists() else {}
            if proof.get('state')!='complete':raise RuntimeError('Primary worker did not complete; investigate before running follow-up.')
            if not (primary/'100k/weknora-frozen-selection.json').exists():raise RuntimeError('Missing primary frozen selection')
            return
        time.sleep(15)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--settings',type=Path,required=True);p.add_argument('--results',type=Path,required=True);p.add_argument('--primary',type=Path,required=True);a=p.parse_args()
    wait_primary(a.settings,a.primary)
    ExtraWorker(a.settings,a.results).main()
