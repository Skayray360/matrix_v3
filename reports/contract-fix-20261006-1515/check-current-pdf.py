"""Read current PDF with production extractor; no inference, indexing or database."""
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import sys
OUT=Path(__file__).resolve().parent
ROOT=OUT.parents[1]
sys.path.insert(0,str(ROOT/'backend'))
os.chdir(ROOT)
from app.config import get_settings
from app.ingestion.loaders import extract_pdf
from app.rag.chunking import chunk_blocks
from app.rag.claim_context import claim_context
from app.rag.schemas import Evidence
s=get_settings()
path=next((ROOT/'data/prestaciones').glob('*PENSIONES*2022.pdf'))
raw=path.read_bytes()
chunks=chunk_blocks(extract_pdf(raw).blocks,chunk_size_tokens=s.rag_chunk_size_tokens,overlap_tokens=s.rag_chunk_overlap_tokens)
chunk=next(c for c in chunks if c.index==8)
item=Evidence(source_id='data-alias/prestaciones/'+path.name+'#8',text=chunk.text,score=1,
    category='prestaciones',filename=path.name,section=chunk.section,page_or_sheet=chunk.page_or_sheet,
    document_id='read-only-source-check',chunk_id='read-only-source-check')
previous=json.loads((ROOT/'reports/audit-real-20261006-132017/requests-current.json').read_text(encoding='utf-8'))
question=next(r['question'] for r in previous if r['request_id']=='61a95692-0e10-4b41-8be2-6a388569333c')
context=claim_context(question,(item,))
result={'file':str(path.relative_to(ROOT)),'pdf_sha256':hashlib.sha256(raw).hexdigest(),
    'chunk_sha256':hashlib.sha256(chunk.text.encode()).hexdigest(),'context':dataclasses.asdict(context),
    'limitation':'This checks actual extraction and calculation only, NOT retrieval, packing or a real model answer.'}
(OUT/'current-pdf-check.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
print(json.dumps({'applications':len(context.applications),'limitations':context.limitations,
                  'values':[str(a.percent) for a in context.applications]},ensure_ascii=False))
