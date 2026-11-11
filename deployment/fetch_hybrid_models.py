from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess

root=Path(__file__).resolve().parent/'models'
models={
    'full-embedding':('onnx-community/bge-m3-ONNX','25b9af8e87a38eb120cfe87125383677b9cd309e'),
    'full-reranker':('onnx-community/bge-reranker-v2-m3-ONNX','6f5ff65298512715a1e669753bc754d2bc8f367b'),
}
def fetch(args):
    target, repo, revision, name=args
    path=root/target/Path(name).name
    if path.exists():return
    temp=path.with_suffix(path.suffix+'.part')
    subprocess.run(['curl','-fsSL','--retry','2','--connect-timeout','15','--max-time','900',
        f'https://huggingface.co/{repo}/resolve/{revision}/{name}','-o',str(temp)],check=True)
    temp.replace(path)
    print(target,name,path.stat().st_size,flush=True)
with ThreadPoolExecutor(max_workers=4) as pool:
    list(pool.map(fetch,[(target,*config,name) for target,config in models.items()
        for name in ('onnx/model_quantized.onnx','tokenizer.json','config.json')]))
