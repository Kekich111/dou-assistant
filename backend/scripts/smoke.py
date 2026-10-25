from fastapi.testclient import TestClient
from app.main import app

QUESTIONS = [
    "скока дней на согласование",
    "приказ",
    "распоряжение",
    "обьясни проще как согласовывается положение",
    "Какой срок согласования нормативного документа?",
    "Нужен приказ о создании рабочей группы",
    "Какой курс доллара сегодня?",
]

client = TestClient(app)
for q in QUESTIONS:
    r = client.post("/api/chat", json={"message": q})
    d = r.json()
    print("=" * 70)
    print("Q:", q, "| HTTP", r.status_code, "|", d.get("elapsed_ms"), "мс")
    print("found:", d.get("found"), "| пункты:", d.get("citations"))
    print("A:", (d.get("answer") or d.get("detail") or "")[:300])
    c = d.get("clarify")
    if c:
        print("clarify:", c["id"], [o["label"] for o in c["options"]])