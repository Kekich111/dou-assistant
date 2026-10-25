from app.rag.retriever import search, get_point
from app.rag.generator import generate_answer

h = get_point("8.16", "x")
print("п. 8.16 длина:", len(h.text) if h else None, "| есть 'Times':", bool(h and "Times" in h.text), "| есть '30 мм':", bool(h and "30 мм" in h.text))

QS = [
    "Можно ли исправлять подписанный документ ручкой или в файле?",
    "Как заверяется бумажная копия документа для внутреннего пользования?",
    "Какие установлены размеры полей для документов?",
    "Что значит формулировка срока «до 15 ноября»?",
]
for q in QS:
    print("\n" + "=" * 70 + "\nQ:", q)
    hits = list(search(q, limit=3, rerank=False))
    print("hits:", [(x.point_no, round(x.vector_score, 3)) for x in hits])
    try:
        r = generate_answer(q, hits)
        print("found:", r.found, "| citations:", r.citations, "| top_score:", round(r.top_score, 3))
        print("answer:", r.answer[:300])
    except Exception as e:  # noqa: BLE001
        print("EXC:", type(e).__name__, e)