"""
backend/scripts/run_eval.py

Прогон golden set: качество поиска, подбор порога отказа, проверка ссылок в ответах.

Запуск (из папки проекта, внутри контейнера):
    docker compose exec backend python -m scripts.run_eval            # только поиск, быстро
    docker compose exec backend python -m scripts.run_eval --api      # полный путь через /api/chat (LLM, медленно)
    docker compose exec backend python -m scripts.run_eval --api --limit 8

Golden set: data/eval/golden_set.jsonl
    {"id": "A01", "type": "answer"|"trap", "question": "...", "expected_points": ["6.6"]}
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

_CANDIDATES = [
    Path("/app/data/eval/golden_set.jsonl"),
    Path(__file__).resolve().parents[2] / "data" / "eval" / "golden_set.jsonl",
    Path(__file__).resolve().parents[1] / "data" / "eval" / "golden_set.jsonl",
]


def load_golden(path: Path | None) -> list[dict]:
    candidates = [path] if path else _CANDIDATES
    for p in candidates:
        if p and p.exists():
            lines = p.read_text(encoding="utf-8").splitlines()
            return [json.loads(x) for x in lines if x.strip()]
    raise SystemExit("golden_set.jsonl не найден; укажите --golden путь")


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


# --------------------------------------------------------------------------- #
# Режим 1: только поиск (без LLM)
# --------------------------------------------------------------------------- #
def run_retrieval(items: list[dict], top_k: int) -> int:
    from app.rag.retriever import Retriever

    retriever = Retriever()
    ans_tops: list[float] = []
    trap_tops: list[float] = []
    hits_at = {1: 0, 3: 0, 5: 0}
    n_ans = 0

    print(f"{'id':<5}{'top cos':>8}  {'результат':<10} точки (cos)")
    for it in items:
        hits = retriever.retrieve(it["question"], limit=max(top_k, 5))
        points = [h.point_no for h in hits]
        top = hits[0].vector_score if hits else 0.0
        shown = ", ".join(f"{h.point_no} ({h.vector_score:.2f})" for h in hits[:top_k])

        if it["type"] == "trap":
            trap_tops.append(top)
            print(f"{it['id']:<5}{top:>8.3f}  {'ловушка':<10} {shown}")
            continue

        n_ans += 1
        ans_tops.append(top)
        expected = set(it["expected_points"])
        found_rank = next((i for i, p in enumerate(points, 1) if p in expected), None)
        for k in hits_at:
            if found_rank is not None and found_rank <= k:
                hits_at[k] += 1
        mark = f"hit@{found_rank}" if found_rank else "ПРОМАХ"
        print(f"{it['id']:<5}{top:>8.3f}  {mark:<10} {shown}  | ждали {sorted(expected)}")
    retriever.close()

    print("\n=== Качество поиска (вопросы с ответом) ===")
    for k, v in hits_at.items():
        print(f"hit@{k}: {v}/{n_ans} = {v / n_ans:.0%}")

    if ans_tops and trap_tops:
        print("\n=== Подбор порога отказа (по лучшему косинусу) ===")
        print(f"вопросы с ответом: min {min(ans_tops):.3f}, медиана {percentile(ans_tops, 0.5):.3f}")
        print(f"ловушки:           max {max(trap_tops):.3f}, медиана {percentile(trap_tops, 0.5):.3f}\n")
        print(f"{'порог':>6} | {'отвечаем':>9} | {'отсекаем ловушки':>16} | баланс")
        best: list[float] = []
        best_bal = -1.0
        for i in range(13):
            t = round(0.40 + 0.025 * i, 3)
            a = sum(x >= t for x in ans_tops) / len(ans_tops)
            r = sum(x < t for x in trap_tops) / len(trap_tops)
            bal = (a + r) / 2
            print(f"{t:>6.3f} | {a:>8.0%} | {r:>15.0%} | {bal:.0%}")
            if bal > best_bal + 1e-9:
                best_bal, best = bal, [t]
            elif abs(bal - best_bal) < 1e-9:
                best.append(t)
        print(f"\nЛучший баланс {best_bal:.0%} при пороге {best[len(best) // 2]:.3f}")
        floor = round(max(trap_tops), 2)
        ceil = round(percentile(ans_tops, 0.9), 2)
        print(f"Для процентов: COS_FLOOR={floor}, COS_CEIL={ceil}  (0% = {floor}, 100% = {ceil})")
    return 0


# --------------------------------------------------------------------------- #
# Режим 2: полный путь через /api/chat (проверяем found и ссылки)
# --------------------------------------------------------------------------- #
def run_api(items: list[dict], url: str, timeout: float) -> int:
    ok_total = 0
    cite_ok = 0
    n_ans = 0
    times: list[float] = []
    with httpx.Client(timeout=timeout) as client:
        for it in items:
            t0 = time.perf_counter()
            try:
                resp = client.post(f"{url}/api/chat", json={"message": it["question"]})
                resp.raise_for_status()
                data = resp.json()
            except httpx.HTTPError as exc:
                print(f"{it['id']:<5} ОШИБКА API: {exc}")
                continue
            elapsed = time.perf_counter() - t0
            times.append(elapsed)

            if it["type"] == "trap":
                ok = not data["found"]
                verdict = "OK (отказ)" if ok else "ЛОЖНЫЙ ОТВЕТ"
            else:
                n_ans += 1
                cited = set(data["citations"])
                expected = set(it["expected_points"])
                ok = bool(data["found"]) and bool(cited & expected)
                cite_ok += ok
                if not data["found"]:
                    verdict = "ЛОЖНЫЙ ОТКАЗ"
                elif ok:
                    verdict = "OK"
                else:
                    verdict = f"НЕВЕРНАЯ ССЫЛКА {sorted(cited)} (ждали {sorted(expected)})"
            ok_total += ok
            print(f"{it['id']:<5} {elapsed:5.1f} c  {verdict}\n      В: {it['question']}\n      О: {data['answer'][:300]}")

    print("\n=== Итог (через API) ===")
    print(f"верно всего: {ok_total}/{len(items)}")
    if n_ans:
        print(f"вопросы с ответом: верная ссылка в {cite_ok}/{n_ans}")
    if times:
        print(f"время ответа: медиана {percentile(times, 0.5):.1f} c, максимум {max(times):.1f} c")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Прогон golden set")
    parser.add_argument("--golden", type=Path, default=None)
    parser.add_argument("--api", action="store_true", help="через /api/chat (LLM)")
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--limit", type=int, default=None, help="взять только первые N вопросов каждого типа")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args()

    items = load_golden(args.golden)
    if args.limit:
        answers = [x for x in items if x["type"] == "answer"][: args.limit]
        traps = [x for x in items if x["type"] == "trap"][: max(2, args.limit // 3)]
        items = answers + traps
    print(f"Вопросов: {len(items)} (с ответом: {sum(x['type'] == 'answer' for x in items)}, "
          f"ловушек: {sum(x['type'] == 'trap' for x in items)})\n")

    if args.api:
        return run_api(items, args.url, args.timeout)
    return run_retrieval(items, args.top_k)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
