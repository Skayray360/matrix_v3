"""Current corporate PDF bytes through production extraction/chunking; no index/LLM."""
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import sys

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[1]
sys.path.insert(0, str(ROOT/'backend'))
os.chdir(ROOT)
from app.config import get_settings
from app.ingestion.loaders import extract_pdf
from app.rag.chunking import chunk_blocks
from app.security.prompt_guard import sanitize_untrusted_text
from pypdf import PdfReader

s = get_settings()
rows=[]
metadata=json.loads((OUT/'documents-current.json').read_text(encoding='utf-8'))
for doc in metadata:
    if not ('PENSIONES' in doc['filename'] or doc['filename'] in ('prestaciones.pdf','PLATICA DE PRESTACIONES JUNIO 2020.pdf')):
        continue
    path=ROOT/'data/prestaciones'/doc['filename']
    raw=path.read_bytes()
    parsed=extract_pdf(raw)
    chunks=chunk_blocks(parsed.blocks,chunk_size_tokens=s.rag_chunk_size_tokens,overlap_tokens=s.rag_chunk_overlap_tokens)
    selected_pages=([1,5,6,7,9,10] if 'PENSIONES' in doc['filename'] else [1,5] if doc['filename']=='prestaciones.pdf' else [1,11,21])
    reader=PdfReader(path)
    rows.append({'file':path.relative_to(ROOT).as_posix(),'sha256':hashlib.sha256(raw).hexdigest(),
        'registered_sha256':doc['sha256'],'page_count':len(reader.pages),'chunk_count':len(chunks),
        'warnings':parsed.warnings,
        'pages':[{'physical_page':i,'text':reader.pages[i-1].extract_text(),
                  'layout_text':reader.pages[i-1].extract_text(extraction_mode='layout')} for i in selected_pages],
        'chunks':[{'source_id':doc['relative_path']+'#'+str(c.index),**dataclasses.asdict(c),
                   'sha256':hashlib.sha256(c.text.encode()).hexdigest(),
                   'sanitized_sha256':hashlib.sha256(sanitize_untrusted_text(c.text).text.rstrip().encode()).hexdigest()}
                  for c in chunks]})
(OUT/'extraction-current.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps([{'file':r['file'],'hash_matches_SQL':r['sha256']==r['registered_sha256'],
                   'pages':r['page_count'],'chunks':r['chunk_count'],'warnings':r['warnings']} for r in rows]))
