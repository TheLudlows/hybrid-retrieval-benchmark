"""Durable native MIRACL scale/tuning stages. Private runtime settings stay outside Git."""
import argparse
from argparse import Namespace
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

import bench
from audit_index import audit


def save(path, value):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.tmp')
    with tmp.open('w') as f:json.dump(value,f,ensure_ascii=False,indent=2);f.write('\n');f.flush();os.fsync(f.fileno())
    tmp.replace(path)


def http(url,payload=None,key=None,method=None,extra=None):
    headers={'Content-Type':'application/json',**(extra or {})}
    if key:headers['Authorization']='Bearer '+key
    req=Request(url,None if payload is None else json.dumps(payload).encode(),headers,method=method)
    with urlopen(req,timeout=600) as r:return json.load(r)


class Worker:
    def __init__(self,settings,results):
        self.settings_path=settings;self.s=json.loads(settings.read_text());self.out=results.resolve()
        self.repo=Path(__file__).resolve().parent.parent;self.work=Path(self.s['work']);self.runtime=self.work/('runtime-'+self.s.get('runtime_revision','v1'));self.runtime.mkdir(parents=True,exist_ok=True)
        self.plan=json.loads((self.out/'experiment-plan.json').read_text());self.splits=json.loads((self.out/'query-splits.json').read_text())
        for name,digest in self.plan.get('code_sha256',{}).items():
            if hashlib.sha256((self.repo/name).read_bytes()).hexdigest()!=digest:raise RuntimeError('Frozen plan code fingerprint mismatch: '+name)
        self.run_prefix=self.plan.get('experiment_revision','v1')+'/'
        self.measure=Path(self.s['measurement_dir']);self.py=self.s['python'];self.children=[];self.current={}
        guarded=list((self.repo/'miracl_zh').glob('*.py'))+[self.repo/'doc_recall_v1/bench.py',self.repo/'deployment/model_server.py',self.repo/'deployment/model_server_hybrid.py',self.out/'experiment-plan.json',self.out/'query-splits.json',self.out/'fresh-topics.tsv',self.out/'fresh-qrels.tsv']
        self.code_hashes={str(p.relative_to(self.repo)):hashlib.sha256(p.read_bytes()).hexdigest() for p in guarded}
        self.lock=(self.work/'worker.lock').open('w');fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        (self.work/'worker.pid').write_text(str(os.getpid())+'\n')
        signal.signal(signal.SIGTERM,self.terminate)

    def status(self,**kw):
        self.current.update(kw,updated_unix=time.time(),pid=os.getpid());save(self.work/'status.json',self.current)
        with (self.work/'events.jsonl').open('a') as f:f.write(json.dumps(self.current)+'\n');f.flush();os.fsync(f.fileno())

    def check_code(self):
        for name,digest in self.code_hashes.items():
            if hashlib.sha256((self.repo/name).read_bytes()).hexdigest()!=digest:raise RuntimeError('Experiment code changed during execution: '+name)

    def terminate(self,*_):
        self.status(state='interrupted')
        for p in reversed(self.children):
            if p.poll() is None:
                try:os.killpg(p.pid,signal.SIGTERM)
                except ProcessLookupError:pass
        raise SystemExit(0)

    def stop_products(self):
        owned=[]
        for p in Path('/proc').iterdir():
            if not p.name.isdigit():continue
            try:c=(p/'cmdline').read_bytes().split(b'\0')
            except OSError:continue
            if (len(c)>2 and c[0]==self.py.encode() and c[2]==b'hindsight_ext_contextdb.local') or (len(c)>1 and c[1]==b'/workspace/work/start-weknora.py') or c[0]==str(self.repo/'deployment/weknora-lite').encode():owned.append(int(p.name))
        for pid in owned:
            try:os.kill(pid,signal.SIGTERM)
            except ProcessLookupError:pass
        for _ in range(60):
            if not any(Path('/proc',str(p)).exists() for p in owned):break
            time.sleep(.5)
        self.children=[p for p in self.children if p.poll() is None]

    def launch(self,command,cwd,env,health,log):
        f=log.open('a');p=subprocess.Popen(command,cwd=cwd,env=env,stdout=f,stderr=subprocess.STDOUT,start_new_session=True);self.children.append(p)
        for _ in range(600):
            if p.poll() is not None:raise RuntimeError('Native service exited; see '+str(log))
            try:http(health);return p
            except (URLError,OSError,HTTPError):time.sleep(1)
        raise TimeoutError('Service not ready: '+health)

    def dburl(self,scale):
        u=urlsplit(self.s['postgres_url']);return urlunsplit((u.scheme,u.netloc,'/'+self.dbname(scale),'',''))

    def dbname(self,scale):
        return 'miracl_scale_'+scale+'_'+self.s.get('runtime_revision','v1')

    def start(self,product,scale,case,cap=60):
        self.check_code()
        self.status(state='starting',scale=scale,product=product,case=case['name'],candidate_ceiling=cap,query_set=None,queries=None,indexed=None,elapsed_s=None)
        self.stop_products();r=self.runtime/(product+'-'+scale);r.mkdir(mode=0o700,parents=True,exist_ok=True);conn=r/'connection.json';env=os.environ.copy()
        if product=='contextbase':
            import psycopg2
            from psycopg2 import sql
            subprocess.run(['docker','start','miracl-pg'],check=True,stdout=subprocess.DEVNULL)
            for _ in range(60):
                try:c=psycopg2.connect(self.s['postgres_url']);break
                except psycopg2.OperationalError:time.sleep(1)
            else:raise RuntimeError('PostgreSQL unavailable')
            c.autocommit=True
            with c.cursor() as q:
                name=self.dbname(scale);q.execute('SELECT 1 FROM pg_database WHERE datname=%s',(name,))
                if not q.fetchone():q.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
            c.close()
            with psycopg2.connect(self.dburl(scale)) as c,c.cursor() as q:
                for ext in ('vector','pg_trgm','btree_gin','pg_search'):q.execute(sql.SQL('CREATE EXTENSION IF NOT EXISTS {}').format(sql.Identifier(ext)))
            sys.path.insert(0,str(self.repo/'deployment'));from run_native import contextbase_environment
            env=contextbase_environment();env.update(PYTHONPATH=self.s['contextbase_source']+'/hindsight-extensions/contextdb-service:'+self.s['contextbase_source']+'/hindsight-api-slim',CONTEXTDB_DEPLOY_DIR=str(r))
            values={'DATABASE_URL':self.dburl(scale),'EMBEDDINGS_OPENAI_MODEL':'bge-m3','EMBEDDINGS_OPENAI_DIMENSIONS':'1024','TEXT_SEARCH_EXTENSION':'pg_search','TEXT_SEARCH_EXTENSION_PG_SEARCH_TOKENIZER':'jieba','ENABLE_TEXT_SEARCH':'true','ENABLE_GRAPH_RETRIEVAL':'true','ENABLE_TEMPORAL_RETRIEVAL':'true','ENABLE_RERANKING':'true','RERANKER_PROVIDER':'tei','RERANKER_TEI_URL':'http://127.0.0.1:18090/contextbase/tei','RERANKER_TEI_HTTP_TIMEOUT':'300','RERANKER_MAX_CANDIDATES':str(cap),'RERANKER_MAX_CANDIDATES_HIGH':str(cap),'RETAIN_EXTRACTION_MODE':'chunks','RETAIN_CHUNK_SIZE':'512',**case.get('env',{})}
            # Reset every tuned knob explicitly: local startup also retains api.env.
            for key,value in {'RECALL_STRATEGY_BOOSTS':'','RECALL_MAX_CANDIDATES_PER_SOURCE':'0','RECALL_BUDGET_FUNCTION':'fixed','RECALL_BUDGET_FIXED_HIGH':'1000'}.items():
                values[key]=case.get('env',{}).get(key,value)
            for prefix in ('CONTEXTBASE_API_','HINDSIGHT_API_'):env.update({prefix+k:v for k,v in values.items()})
            self.launch([self.py,'-m','hindsight_ext_contextdb.local','--component','api','--port','18888'],self.s['contextbase_source'],env,'http://127.0.0.1:18888/health/ready',r/'service.log')
            if not conn.exists():
                base='http://127.0.0.1:18888';admin=json.loads((r/'admin-key.json').read_text())['secret'];bank='miracl_scale_'+scale
                try:http(base+'/v1/default/banks/'+bank,{'name':bank},admin,'PUT',{'If-None-Match':'*'})
                except HTTPError as e:
                    if e.code!=412:raise
                user=http(base+'/ext/contextdb/v1/users',{'name':'benchmark-'+scale+'-'+str(time.time_ns())},admin)['user_id']
                key=http(base+f'/ext/contextdb/v1/users/{user}/keys',{'name':'scale-benchmark','authorization':{'mode':'selected','workspaces':[{'bank_id':bank,'permission':'read_write'}]}},admin)['secret']
                save(conn,{'CONTEXTBASE_URL':base,'CONTEXTBASE_BANK_ID':bank,'CONTEXTBASE_API_KEY':key});conn.chmod(0o600)
        else:
            subprocess.run(['docker','stop','miracl-pg'],stdout=subprocess.DEVNULL,check=True)
            cred=r/'credentials.json'
            if not cred.exists():
                import secrets
                save(cred,{'password':'Aa1'+secrets.token_urlsafe(18),'jwt':secrets.token_hex(32),'aes':secrets.token_hex(16)});cred.chmod(0o600)
            auth=json.loads(cred.read_text());models=r/'builtin-models.yaml';shutil.copyfile(self.s['weknora_models'],models)
            env.update(DB_DRIVER='sqlite',DB_PATH=str(r/'weknora.db'),RETRIEVE_DRIVER='sqlite',STORAGE_TYPE='local',LOCAL_STORAGE_BASE_DIR=str(r/'files'),STREAM_MANAGER_TYPE='memory',JWT_SECRET=auth['jwt'],SYSTEM_AES_KEY=auth['aes'],BUILTIN_MODELS_CONFIG=str(models),WEKNORA_SANDBOX_MODE='disabled',WEKNORA_SANDBOX_DOCKER_ENABLED='false',SSRF_WHITELIST='127.0.0.1',SERVER_HOST='127.0.0.1',SERVER_PORT='18080',GIN_MODE='release',LOG_LEVEL='warn',AUTO_MIGRATE='true',NEO4J_ENABLE='false',ENABLE_GRAPH_RAG='false',DISABLE_REGISTRATION='false');env.pop('REDIS_ADDR',None)
            self.launch([str(self.repo/'deployment/weknora-lite')],self.repo/'deployment/weknora',env,'http://127.0.0.1:18080/health',r/'service.log')
            base='http://127.0.0.1:18080';login={'email':'benchmark@weknora.test','password':auth['password']}
            try:session=http(base+'/api/v1/auth/login',login)
            except HTTPError as e:
                if e.code!=401:raise
                http(base+'/api/v1/auth/register',{'username':'Benchmark',**login});session=http(base+'/api/v1/auth/login',login)
            self.wtoken=session['token'];tenant=session['active_tenant']['id']
            if not conn.exists():
                kb=http(base+'/api/v1/knowledge-bases',{'name':'miracl-scale-'+scale,'type':'document','embedding_model_id':'benchmark-bge-m3','chunking_config':{'chunk_size':512,'chunk_overlap':0},'question_generation_config':{'enabled':False},'profile_config':{'enabled':False}},self.wtoken)['data']['id']
                key=http(base+f'/api/v1/tenants/{tenant}/api-keys',{'name':'scale-benchmark','full_access':True},self.wtoken)['data']['token']
                save(conn,{'WEKNORA_URL':base,'WEKNORA_KB_ID':kb,'WEKNORA_API_KEY':key});conn.chmod(0o600)
            self.set_wconfig(case,cap)
        self.connection=json.loads(conn.read_text());os.environ.update(self.connection);os.environ['BENCH_HTTP_TIMEOUT_SECONDS']='600'

    def set_wconfig(self,case,cap):
        cfg={'embedding_top_k':cap,'rerank_top_k':cap,'vector_threshold':0,'keyword_threshold':0,'rerank_threshold':0,'rerank_model_id':'benchmark-bge-reranker',**case['rrf']}
        result=http('http://127.0.0.1:18080/api/v1/tenants/kv/retrieval-config',cfg,self.wtoken,'PUT')
        if result.get('data')!=cfg:raise RuntimeError('Native retrieval config readback mismatch')

    def context(self,product,phase,runid):
        save(self.measure/'measurement-context.json',{product:{'phase':phase,'id':'corpus','run_id':runid}})

    def archive_calls(self,product,runid,target):
        stats={};per_query={}
        with (self.measure/'model-usage.jsonl').open() as src,gzip.open(target,'wt') as out:
            for line in src:
                r=json.loads(line)
                if r['product']!=product or r.get('run_id')!=runid:continue
                out.write(line);s=stats.setdefault(r['phase']+'/'+r['kind'],{'input_tokens':0,'calls':0,'pairs':0,'max_pairs':0});s['input_tokens']+=r['input_tokens'];s['calls']+=r['calls'];s['pairs']+=r.get('pairs',0);s['max_pairs']=max(s['max_pairs'],r.get('pairs',0))
                if r['phase']=='query':
                    q=per_query.setdefault(r['id'],{}).setdefault(r['kind'],{'calls':0,'input_tokens':0,'pairs':0});q['calls']+=r['calls'];q['input_tokens']+=r['input_tokens'];q['pairs']+=r.get('pairs',0)
        if per_query:stats['per_query']=per_query
        return stats

    def ingest(self,product,scale,data):
        target=self.out/scale;target.mkdir(exist_ok=True);checkpoint=self.runtime/(product+'-'+scale)/'ingest.sqlite';marker=target/(product+'-index-audit.json')
        if marker.exists():
            os.environ['AUDIT_POSTGRES_URL']=self.dburl(scale)
            audit(Namespace(data=data,checkpoint=checkpoint,product=product,weknora_db=self.runtime/(product+'-'+scale)/'weknora.db',output=marker))
            return checkpoint
        runid=self.run_prefix+scale+'/'+product+'/ingest';self.context(product,'ingest',runid);self.status(state='ingesting',indexed=0)
        before=0
        if checkpoint.exists():
            with sqlite3.connect(checkpoint) as db:before=db.execute('SELECT COUNT(*) FROM passage_map WHERE indexed=1').fetchone()[0]
        env=os.environ.copy();log=self.runtime/(product+'-'+scale)/'ingest.log';started=time.time()
        with log.open('a') as f:
            p=subprocess.Popen([self.py,str(self.repo/'miracl_zh/ingest.py'),'--data',str(data),product,str(checkpoint)],cwd=self.repo,env=env,stdout=f,stderr=subprocess.STDOUT,start_new_session=True);self.children.append(p)
            published_bucket=before//10000
            while p.poll() is None:
                if checkpoint.exists():
                    with sqlite3.connect(checkpoint) as db:
                        try:n=db.execute('SELECT COUNT(*) FROM passage_map WHERE indexed=1').fetchone()[0]
                        except sqlite3.OperationalError:n=0
                    self.status(indexed=n,elapsed_s=time.time()-started)
                    if n//10000>published_bucket:
                        published_bucket=n//10000;save(target/(product+'-ingest-progress.json'),dict(self.current));self.publish('Checkpoint '+scale+' '+product+' indexed '+str(n)+' passages')
                time.sleep(15)
            if p.returncode:raise RuntimeError('Ingestion stopped; reconcile pending native creates with original ingest.py recovery options: '+str(log))
        os.environ['AUDIT_POSTGRES_URL']=self.dburl(scale)
        audit(Namespace(data=data,checkpoint=checkpoint,product=product,weknora_db=self.runtime/(product+'-'+scale)/'weknora.db',output=marker))
        timing=checkpoint.with_suffix('.timing.jsonl');shutil.copyfile(timing,target/(product+'-ingest-timing.jsonl'))
        save(target/(product+'-ingest-usage.json'),{'driver_attempt_wall_s':time.time()-started,'indexed_before_attempt':before,'cumulative_recorded_batch_wall_s':sum(json.loads(line)['elapsed_s'] for line in timing.read_text().splitlines()),'models':self.archive_calls(product,runid,target/(product+'-ingest-model-calls.jsonl.gz'))})
        self.backup(product,scale,'index-ready');return checkpoint

    def query_set(self,data,name):
        topics,qrels=bench.topics_and_qrels(data);source=bench.dataset_source(data)
        if name=='fresh_train200':
            topics=dict(x.split('\t',1) for x in (self.out/'fresh-topics.tsv').read_text().splitlines());qrels={q:{} for q in topics}
            for line in (self.out/'fresh-qrels.tsv').read_text().splitlines():
                q,_,p,v=line.split();qrels[q][p]=int(v)
        elif name!='dev393':
            selected=set(self.splits[name]);topics={q:t for q,t in topics.items() if q in selected};qrels={q:qrels[q] for q in topics}
        if name!='dev393':source={**source,'dataset':'miracl/zh/'+name+'/subset-'+source['subset']['file']['sha256'][:16]}
        return topics,qrels,source

    def capture(self,product,scale,data,checkpoint,case,qset,cap=60,max_tokens=8192):
        self.check_code()
        label=case['name']+'-'+qset+'-cap'+str(cap)+('-tokens'+str(max_tokens) if max_tokens!=8192 else '')
        folder=self.out/scale/product/label;folder.mkdir(parents=True,exist_ok=True)
        metrics=folder/'metrics.json'
        if metrics.exists():return json.loads(metrics.read_text())
        topics,qrels,source=self.query_set(data,qset)
        config={'product':product,'scale':scale,'corpus':bench.dataset_source(data)['subset']['file'],'query_set':qset,'query_ids_sha256':hashlib.sha256(json.dumps(list(topics)).encode()).hexdigest(),'case':case,'candidate_ceiling':cap,'max_tokens':max_tokens,'plan_sha256':hashlib.sha256((self.out/'experiment-plan.json').read_bytes()).hexdigest()}
        save(folder/'config.json',config);digest=hashlib.sha256((folder/'config.json').read_bytes()).hexdigest();runid=self.run_prefix+scale+'/'+product+'/'+label
        args=Namespace(product=product,data=data,run=folder/'rankings.jsonl',passage_map=checkpoint,limit=cap,reranker='benchmark-bge-reranker',resume=(folder/'rankings.jsonl').exists(),warmup=4,max_queries=None,smoke_index=False,config_sha256=digest,measurement_dir=self.measure,run_id=runid,max_tokens=max_tokens)
        self.status(state='querying',query_set=qset,run_id=runid,queries=len(topics));bench.capture(args,topics,source)
        result=bench.score(args.run,qrels,None,source);usage=self.archive_calls(product,runid,folder/'model-calls.jsonl.gz');query=usage.get('query/rerank',{})
        if query.get('max_pairs',0)>cap:raise RuntimeError('Observed model input exceeded candidate ceiling')
        rows=[json.loads(line) for line in args.run.read_text().splitlines()]
        if any(r['passages'] and usage.get('per_query',{}).get(r['qid'],{}).get('rerank',{}).get('calls',0)<1 for r in rows):raise RuntimeError('Nonempty ranking without an observed model rerank')
        save(folder/'model-usage.json',usage);save(metrics,result);return result

    def derive_assessment(self,product,scale,data,case):
        folder=self.out/scale/product/(case['name']+'-assessment_dev293-derived');folder.mkdir(parents=True,exist_ok=True)
        original=self.out/scale/product/(case['name']+'-dev393-cap60')/'rankings.jsonl';topics,qrels,source=self.query_set(data,'assessment_dev293')
        rows=[json.loads(line) for line in original.read_text().splitlines()];target=folder/'rankings.jsonl'
        target.write_text(''.join(json.dumps({**r,'dataset':source['dataset']})+'\n' for r in rows if r['qid'] in topics))
        save(folder/'metrics.json',bench.score(target,qrels,None,source));save(folder/'provenance.json',{'derived_from':str(original.relative_to(self.out)),'additional_native_requests':0,'assessment_already_inspected':True})

    def backup(self,product,scale,label):
        r=self.runtime/(product+'-'+scale);target=self.work/'backups';target.mkdir(exist_ok=True)
        if product=='contextbase':
            with (target/(product+'-'+scale+'-'+label+'.dump')).open('wb') as f:subprocess.run(['docker','exec','miracl-pg','pg_dump','-U','postgres','-d',self.dbname(scale),'-Fc'],stdout=f,check=True)
        else:
            with sqlite3.connect(r/'weknora.db') as src,sqlite3.connect(target/(product+'-'+scale+'-'+label+'.db')) as dst:src.backup(dst)

    def publish(self,message):
        # Only this experiment's outputs may be staged by the worker.
        staged=subprocess.check_output(['git','diff','--cached','--name-only'],cwd=self.repo,text=True).splitlines()
        relative=str(self.out.relative_to(self.repo))
        if any(not n.startswith(relative+'/') for n in staged):self.status(publish_state='blocked_external_staging');return
        save(self.out/'execution-status.json',self.current)
        self.render()
        subprocess.run(['git','add',relative],cwd=self.repo,check=True)
        if subprocess.run(['git','diff','--cached','--quiet'],cwd=self.repo).returncode==0:return
        subprocess.run(['git','commit','-m',message],cwd=self.repo,check=True)
        p=subprocess.run(['git','push','origin','miracl-zh-benchmark'],cwd=self.repo)
        self.status(publish_state='pushed' if p.returncode==0 else 'push_pending')

    def render(self):
        for scale in ('100k','1m'):
            root=self.out/scale
            lines=['# '+scale+' 阶段结果','', '所有数值来自已完成并落盘的阶段；缺失阶段仍在运行或尚未开始。开发集结果已被检查，不能声称盲测。新增200题来自官方 train，仅未用于本轮调参。','', '| 产品 / 配置 / 查询集 | 题数 | Hit@1 | nDCG@10 | Recall@100 | p95 秒 |','| --- | ---: | ---: | ---: | ---: | ---: |']
            for p in sorted(root.glob('*/*/metrics.json')):
                d=json.loads(p.read_text());label=str(p.parent.relative_to(root));lines.append(f'| [{label}]({label}/metrics.json) | {d["queries"]} | {d["hit_at_1"]:.4%} | {d["ndcg_at_10"]:.6f} | {d["recall_at_100"]:.4%} | {d["serial_p95_ms"]/1000:.3f} |')
            lines+=['','每个已完成配置目录保留配置指纹、逐题排名、实际模型调用（gzip）和用量。`assessment_dev293-derived` 是完整393题中的固定子集，不另发查询；其余记录标明实际查询集。40/100候选和ContextBase 16384返回预算仅为独立预算实验，不参与60候选主配置选择。','', '入库进度在挂载目录 `/workspace/work/miracl-scale/status.json`，事件在 `events.jsonl`。各产品入库用量文件标明本次续跑前已完成数量、尝试墙钟和累计已记录批次墙钟；原生索引与认证配置保存在该目录 runtime/backups，不进入 Git。']
            (root/'README.md').write_text('\n'.join(lines)+'\n')

    def size(self,product,scale):
        if product=='weknora':return (self.runtime/(product+'-'+scale)/'weknora.db').stat().st_size
        import psycopg2
        with psycopg2.connect(self.s['postgres_url']) as c,c.cursor() as q:q.execute('SELECT pg_database_size(%s)',(self.dbname(scale),));return q.fetchone()[0]

    def scale(self,scale,data,frozen=None):
        self.status(scale=scale,state='baseline')
        selected={};sizes={};checkpoints={}
        # Complete both default baselines before looking at any tuning result.
        for product in ('contextbase','weknora'):
            trials=self.plan[product+'_trials'];default=trials[0]
            self.start(product,scale,default);checkpoint=self.ingest(product,scale,data)
            checkpoints[product]=checkpoint
            self.capture(product,scale,data,checkpoint,default,'dev393')
            self.derive_assessment(product,scale,data,default)
            self.publish('Archive '+scale+' '+product+' default 393-query baseline')
        for product in ('contextbase','weknora'):
            trials=self.plan[product+'_trials'];default=trials[0];checkpoint=checkpoints[product]
            self.start(product,scale,default)
            if frozen is None:
                candidates=[]
                for case in trials:
                    if case!=default:
                        if product=='contextbase':self.start(product,scale,case)
                        else:self.set_wconfig(case,60)
                    result=self.capture(product,scale,data,checkpoint,case,'tune_dev100');candidates.append((case,result))
                chosen=max(enumerate(candidates),key=lambda z:(z[1][1]['ndcg_at_10'],z[1][1]['hit_at_1'],-z[0]))[1][0]
                selected[product]=chosen;save(self.out/scale/(product+'-frozen-selection.json'),{'case':chosen,'rule':self.plan['selection'],'frozen_before_assessment_and_fresh':True})
            else:chosen=frozen[product];selected[product]=chosen
            if product=='contextbase':self.start(product,scale,chosen)
            else:self.set_wconfig(chosen,60)
            for qset in ('dev393','fresh_train200'):
                self.capture(product,scale,data,checkpoint,chosen,qset)
            self.derive_assessment(product,scale,data,chosen)
            if frozen is None:
                for cap in self.plan['frontier_candidate_ceilings']:
                    if product=='contextbase':self.start(product,scale,chosen,cap)
                    else:self.set_wconfig(chosen,cap)
                    self.capture(product,scale,data,checkpoint,chosen,'tune_dev100',cap)
                if product=='contextbase':
                    self.start(product,scale,chosen,60)
                    self.capture(product,scale,data,checkpoint,chosen,'tune_dev100',60,16384)
            sizes[product]=self.size(product,scale);save(self.out/scale/'native-index-sizes.json',sizes)
            self.publish('Archive '+scale+' '+product+' tuning and frozen validation')
        return selected,sizes

    def main(self):
        try:
            frozen,sizes=self.scale('100k',Path(self.s['data100k']))
            free=shutil.disk_usage(self.work).free
            # Tenfold live indices, one additional backup allowance, and 8 GiB reserve.
            needed=sum(sizes.values())*19+8*1024**3
            gate={'free_bytes':free,'projected_increment_and_backup_bytes':needed,'native_100k_sizes':sizes,'pass':free>needed}
            save(self.out/'million-capacity-gate.json',gate)
            self.publish('Record measured capacity gate before million-passage run')
            if free<=needed:
                self.status(state='blocked_capacity',capacity_gate=gate);self.publish('Pause million-passage stage at measured capacity gate');return
            self.scale('1m',Path(self.s['data1m']),frozen)
            self.status(state='complete');self.publish('Complete nested million-passage validation')
        except BaseException as e:
            if isinstance(e,SystemExit):raise
            self.status(state='failed',error_type=type(e).__name__,error=str(e));raise

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--settings',type=Path,required=True);p.add_argument('--results',type=Path,required=True);a=p.parse_args();Worker(a.settings,a.results).main()
