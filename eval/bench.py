# Benchmark di search_docs: eseguito dentro il container backend, legge le domande
# (JSON) da stdin e stampa una riga JSONL per domanda e modalità.
#   A = solo la domanda;  B = domanda + keywords (campo keywords della domanda o KW, come le passerebbe Claude).
# Uso (dalla radice del repository):
#   python3 -c "import yaml,json;json.dump(yaml.safe_load(open('eval/domande.yaml')),open('/tmp/d.json','w'))"
#   KEY=$(docker exec docsgpt-postgres-1 psql -U docsgpt -d docsgpt -Atc \
#         "select key from agents where name='Claude - archivio fiscale'")
#   docker cp eval/bench.py docsgpt-backend-1:/tmp/bench.py
#   docker exec -i -e AGENT_KEY="$KEY" -e PYTHONPATH=/app -w /app docsgpt-backend-1 \
#         python /tmp/bench.py < /tmp/d.json > eval/risultati-AAAA-MM-GG.jsonl
import sys, json, os, time, fnmatch, urllib.parse
from docsgpt.services.search_service import search_documents
KW = {
 'rif-01':'"21/E" 2016','rif-02':'"a-ter" "reverse charge" edile','rif-03':'"sanzione unica" ravvedimento "codice tributo"',
 'rif-04':'"12-ter" "ravvedimento anni pregressi" "codice tributo"','rif-05':'"45/E" 2024','rif-06':'"9/E" "concordato preventivo biennale" 2025',
 'par-01':'"locazioni brevi" ritenuta intermediari','par-02':'cripto-attività plusvalenze "2.000 euro"','par-03':'"prestazioni sanitarie di diagnosi" esenti',
 'tem-01':'"locazioni brevi" "26 per cento"','tem-02':'"locazioni brevi" cedolare','tem-03':'"patent box" "110 per cento"',
 'tem-04':'"b-bis" plusvalenza superbonus','sim-01':'"b-bis" plusvalenza comproprietari superbonus','sim-02':'concordato forfetario "maggiorato del 50 per cento"',
 'sim-03':'"cedolare secca" "C/1" proroga','giu-01':'"cedolare secca" conduttore società','giu-02':'criptovalute plusvalenze omessa dichiarazione',
 'loc-01':'"imposta di soggiorno" Bologna 2026','loc-02':'CIN sanzione annuncio','loc-03':'CIN CIR "Emilia-Romagna"',
 'fil-01':'"concordato preventivo biennale" "associazione professionale"'}
key = os.environ['AGENT_KEY']; qs = json.load(sys.stdin)['domande']
# SOLO_PREFISSO=<id fonte>-: solo le domande di una fonte (prova_fonte.py bench).
qs = [q for q in qs if q['id'].startswith(os.environ.get('SOLO_PREFISSO', ''))]
for q in qs:
    for mode in ('A', 'B'):
        kw = (q.get('keywords') or KW.get(q['id'])) if mode == 'B' else None
        t = time.time()
        try: res = search_documents(key, q['domanda'], 10, kw); err = None
        except Exception as e: res, err = [], repr(e)
        dt = time.time() - t
        paths = [urllib.parse.unquote(r.get('url', '')).split('/docs/', 1)[-1] for r in res]
        ranks = []
        for pat in q.get('attesi') or []:
            rk = next((i + 1 for i, p in enumerate(paths) if fnmatch.fnmatch(p, pat)), None)
            ranks.append(rk)
        print(json.dumps({'id': q['id'], 'cat': q['categoria'], 'mode': mode, 'kw': kw, 'ranks': ranks,
                          'sec': round(dt, 1), 'chars': len(json.dumps(res, ensure_ascii=False)), 'err': err,
                          'top3': [p.rsplit('/', 1)[-1][:70] for p in paths[:3]]}, ensure_ascii=False), flush=True)
