"""Compare indexing recipe using observed inventory digest; no embeddings or vector store."""
import json
import os
from pathlib import Path
import sys
OUT=Path(__file__).resolve().parent
ROOT=OUT.parents[1]
sys.path.insert(0,str(ROOT/'backend'))
os.chdir(ROOT)
from app.config import get_settings
from app.rag.index_manifest import indexing_fingerprint
s=get_settings()
inventory=json.loads((OUT/'ollama-inventory.json').read_text(encoding='utf-8-sig'))
digest=next(m['digest'] for m in inventory if m['name']==s.ollama_embedding_model)
class ObservedInventory:
    def embedding_revision(self): return digest
fingerprint=indexing_fingerprint(ObservedInventory())
docs=json.loads((OUT/'documents-current.json').read_text(encoding='utf-8'))
result={'observed_embedding_digest':digest,'matches_configured_pin':digest==s.llm_embedding_digest,
    'calculated_index_fingerprint':fingerprint,'registered_fingerprints_match':all(d['index_fingerprint']==fingerprint for d in docs),
    'configured_dimension':s.rag_embedding_dimension,
    'limitation':'No vector collection or chunk payload read. Matching SQL recipe is not proof of current indexed bytes.'}
(OUT/'embedding-metadata-check.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
print(json.dumps(result))
