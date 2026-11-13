"""Native retrieval comparison with model reranking disabled; adopts existing ingestion."""
import argparse
from argparse import Namespace
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

import bench
from run_scale import Worker, save


class NativeWorker(Worker):
    def preflight_no_rerank(self,product,scale,data,checkpoint,case):
        folder=self.out/scale/product/'no-rerank-restart-preflight';folder.mkdir(parents=True,exist_ok=True)
        marker=folder/'verified.json'
        if marker.exists():return
        topics,_,source=self.query_set(data,'dev393');run=folder/'rankings.jsonl'
        digest=hashlib.sha256((self.out/'experiment-plan.json').read_bytes()).hexdigest()
        runid=self.run_prefix+scale+'/'+product+'/restart-preflight'
        args=Namespace(product=product,data=data,run=run,passage_map=checkpoint,limit=60,reranker='benchmark-bge-reranker',resume=run.exists(),warmup=0,max_queries=2,smoke_index=True,config_sha256=digest,measurement_dir=self.measure,run_id=runid,max_tokens=8192,disable_rerank=True)
        self.status(state='restart_preflight',run_id=runid)
        bench.capture(args,topics,source);before=run.read_text().splitlines()
        self.start(product,scale,case)
        args.resume=True;args.max_queries=3;bench.capture(args,topics,source)
        after=run.read_text().splitlines()
        if len(after)!=3 or after[:len(before)]!=before:raise RuntimeError('Restart preflight changed completed rows')
        usage=self.archive_calls(product,runid,folder/'model-calls.jsonl.gz')
        if any(v.get('calls',0) for k,v in usage.items() if k.endswith('/rerank')):raise RuntimeError('Rerank observed in preflight')
        if usage.get('smoke_query/embedding',{}).get('calls',0)<3:raise RuntimeError('Missing preflight embedding evidence')
        save(marker,{'restart_verified':True,'completed_queries_preserved':len(before),'final_unique_qids':len({json.loads(l)['qid'] for l in after}),'model_rerank_calls':0,'excluded_from_formal_metrics':True,'model_usage':usage})

    def adopt_existing_ingestion(self):
        handoff=self.work/'mode-change.json'
        if not handoff.exists():return
        old=json.loads(handoff.read_text());pid=old['adopt_ingestion_pid']
        checkpoint=self.runtime/'weknora-100k/ingest.sqlite';bucket=-1
        while True:
            with sqlite3.connect(checkpoint) as db:
                n=db.execute('SELECT COUNT(*) FROM passage_map WHERE indexed=1').fetchone()[0]
                complete=db.execute('SELECT complete FROM miracl_ingest_run WHERE id=1').fetchone()[0]
            if complete:
                self.status(state='adopted_ingestion_complete',product='weknora',scale='100k',indexed=n);return
            try:
                cmd=Path('/proc',str(pid),'cmdline').read_bytes().split(b'\0')
                running=len(cmd)>1 and cmd[1]==str(self.repo/'miracl_zh/ingest.py').encode()
            except OSError:running=False
            if not running:raise RuntimeError('Adopted WeKnora ingestion stopped before completion; preserve checkpoint and reconcile unresolved creates before resuming.')
            self.status(state='waiting_existing_ingestion',product='weknora',scale='100k',indexed=n,adopted_ingest_pid=pid)
            if n//10000>bucket:
                bucket=n//10000;save(self.out/'100k/weknora-ingest-progress.json',dict(self.current))
                self.publish('Checkpoint no-rerank protocol and continuing shared WeKnora index '+str(n))
            time.sleep(15)

    def main(self):
        try:
            if self.plan.get('model_reranking') is not False:raise RuntimeError('This runner requires model_reranking=false')
            self.adopt_existing_ingestion()
            super().main()
        except Exception as e:
            self.status(state='failed',error_type=type(e).__name__,error=str(e));raise


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--settings',type=Path,required=True);p.add_argument('--results',type=Path,required=True);a=p.parse_args();NativeWorker(a.settings,a.results).main()
