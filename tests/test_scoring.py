"""Тесты умного отбора (TZ §4.7, шаг 11)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from src.db.models import Chat, Message
from src.jobs import retrain
from src.nlp.threads import segment
from src.scoring import score as scoring
from src.scoring.llm_rubric import Rubric, format_examples, rate_thread
from src.scoring.novelty import cosine, novelty_from_similarity, score_novelty
from src.scoring.signals import ThreadSignals, collect_signals, engagement, normalize, percentile

START = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
VASYA, PETYA, MASHA, OWNER = 101, 102, 103, 132036441


def msg(tg_msg_id: int, *, user: int = VASYA, text: str = "текст", minutes: int = 0,
        reply_to: int | None = None, **kw: Any) -> Message:
    return Message(
        chat_id=1, tg_msg_id=tg_msg_id, tg_user_id=user, author_name=f"user{user}",
        text=text, reply_to=reply_to, date=START + timedelta(minutes=minutes), **kw,
    )


def thread_of(messages: list[Message]):
    return segment(messages)[0]


# ================================================================= сигналы

def test_signals_of_a_live_discussion():
    thread = thread_of([
        msg(1, text="Сколько стоит генерация? https://example.com/pricing"),
        msg(2, user=PETYA, reply_to=1, text="12 рублей за ролик", minutes=2, reactions=3),
        msg(3, user=MASHA, reply_to=1, text="подтверждаю", minutes=10),
    ])
    signals = collect_signals(thread)

    assert signals.participants == 3
    assert signals.messages == 3
    assert signals.replies == 2
    assert signals.reactions == 3
    assert signals.duration_min == 10.0
    assert signals.links == 1
    assert signals.has_numbers is True
    assert signals.has_price is True, "«12 рублей» — это цена"


def test_question_is_answered_when_someone_else_replies():
    answered = thread_of([
        msg(1, text="как это починить?"),
        msg(2, user=PETYA, reply_to=1, text="перезапусти", minutes=1),
    ])
    assert collect_signals(answered).answered is True


def test_question_talking_to_yourself_is_not_answered():
    """Иначе собственное «ну ладно» закрывало бы свой же вопрос."""
    alone = thread_of([
        msg(1, text="кто-нибудь знает?"),
        msg(2, user=VASYA, reply_to=1, text="ладно, сам разберусь", minutes=1),
    ])
    assert collect_signals(alone).answered is False
    assert collect_signals(alone).has_question is True


def test_owner_signal_counts_pin_and_copy():
    """Владелец уже показал интерес — это самый честный сигнал (§4.7)."""
    plain = collect_signals(thread_of([msg(1), msg(2, user=PETYA, reply_to=1, minutes=1)]))
    pinned = collect_signals(thread_of([
        msg(1, is_pinned_by_me=True), msg(2, user=PETYA, reply_to=1, minutes=1),
    ]))
    copied = collect_signals(thread_of([
        msg(1, copy_count=2), msg(2, user=PETYA, reply_to=1, minutes=1),
    ]))

    assert plain.owner_signal == 0.0
    assert pinned.owner_signal > 0
    assert copied.owner_signal > 0


def test_author_weight_is_averaged():
    thread = thread_of([msg(1, user=VASYA), msg(2, user=PETYA, reply_to=1, minutes=1)])
    signals = collect_signals(thread, author_weights={VASYA: 2.0, PETYA: 1.0})

    assert signals.author_weight == pytest.approx(1.5)


def test_muted_share():
    thread = thread_of([msg(1, user=VASYA), msg(2, user=PETYA, reply_to=1, minutes=1)])
    assert collect_signals(thread, muted_authors={PETYA}).muted_share == 0.5


# -------------------------------------------------------- нормализация

def test_percentile():
    history = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert percentile(0.0, history) == 0.0
    assert percentile(3.0, history) == 0.6
    assert percentile(9.0, history) == 1.0
    assert percentile(5.0, []) == 0.5, "без истории — середина"


def test_normalization_is_relative_to_the_group():
    """Двадцать сообщений значат разное в шумном и тихом чате (§4.7)."""
    signals = ThreadSignals(participants=3, messages=20, replies=10, reactions=2,
                            duration_min=30, links=1)
    noisy = {"messages": [float(n) for n in range(5, 105)]}      # обычное дело — 50+
    quiet = {"messages": [1.0, 2.0, 3.0, 2.0, 1.0] * 4}          # обычно 1-3

    in_noisy = normalize(signals, noisy)
    in_quiet = normalize(signals, quiet)

    assert in_quiet["messages"] == 1.0, "в тихом чате это выдающийся тред"
    assert in_noisy["messages"] < 0.5, "в шумном — рядовой"


def test_normalization_falls_back_to_today_when_history_is_short():
    signals = ThreadSignals(messages=10)
    peers = [ThreadSignals(messages=n) for n in (1, 2, 3, 10, 20)]

    result = normalize(signals, {"messages": [5.0]}, peers=peers)

    assert 0 < result["messages"] < 1


def test_unanswered_question_is_flagged():
    signals = ThreadSignals(has_question=True, answered=False)
    assert normalize(signals, {})["unanswered_question"] == 1.0

    signals = ThreadSignals(has_question=True, answered=True)
    assert normalize(signals, {})["unanswered_question"] == 0.0


def test_engagement_is_bounded():
    everything = dict.fromkeys(
        ["participants", "replies", "reactions", "messages", "duration_min", "links",
         "author_weight"], 1.0,
    )
    assert engagement(everything) == 1.0
    assert engagement({}) == 0.0


# ================================================================== рубрика

def fake_llm(payload: Any):
    calls: list[dict[str, Any]] = []

    async def call(messages, *, purpose: str, chat_id: int | None = None, **kw: Any):
        calls.append({"messages": messages, "purpose": purpose})
        return payload, Usage_stub()

    call.calls = calls  # type: ignore[attr-defined]
    return call


class Usage_stub:
    tokens_in = 10
    tokens_out = 5
    model = "test"

    def __add__(self, other: Any) -> Any:
        return self


def a_thread():
    return thread_of([
        msg(1, text="Seedance режет ролики?"),
        msg(2, user=PETYA, reply_to=1, text="да, обрезает", minutes=1),
    ])


async def test_rubric_parses_scores():
    llm = fake_llm({
        "kind": "decision", "usefulness": 8, "specificity": 7, "relevance": 9,
        "takeaway": "ужимать референсы", "why": "поймали трое",
    })
    rubric, _ = await rate_thread(a_thread(), Chat(id=1, tg_id=-100), llm=llm)

    assert rubric.kind == "decision"
    assert rubric.usefulness == 8
    assert rubric.takeaway == "ужимать референсы"
    assert llm.calls[0]["purpose"] == "score", "расход пишется как скоринг, не как дайджест"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(11, 10.0), (-3, 0.0), ("8/10", 8.0), ("7", 7.0), (None, 0.0), ("ерунда", 0.0)],
)
async def test_rubric_survives_creative_scores(raw: Any, expected: float):
    """Модель охотно возвращает 11, «8/10» и слова вместо чисел."""
    llm = fake_llm({"kind": "insight", "usefulness": raw})
    rubric, _ = await rate_thread(a_thread(), Chat(id=1, tg_id=-100), llm=llm)
    assert rubric.usefulness == expected


async def test_unknown_kind_becomes_other():
    llm = fake_llm({"kind": "шедевр", "usefulness": 5})
    rubric, _ = await rate_thread(a_thread(), Chat(id=1, tg_id=-100), llm=llm)
    assert rubric.kind == "other"


async def test_feedback_examples_go_into_the_prompt():
    """Именно примеры делают отбор «как выбрал бы владелец» (§4.7)."""
    llm = fake_llm({"kind": "insight", "usefulness": 5})
    examples = [
        {"value": 1, "title": "Полезная тема", "takeaway": "делать так"},
        {"value": -2, "title": "Скучная тема", "takeaway": ""},
    ]
    await rate_thread(a_thread(), Chat(id=1, tg_id=-100), examples=examples, llm=llm)

    prompt = llm.calls[0]["messages"][1]["content"]
    assert "👍 полезно: Полезная тема" in prompt
    assert "🔕 больше не показывать: Скучная тема" in prompt


async def test_interests_profile_goes_into_the_rubric():
    llm = fake_llm({"kind": "insight", "usefulness": 5})
    chat = Chat(id=1, tg_id=-100, settings={"interests_profile": "интересно: AI-видео"})
    await rate_thread(a_thread(), chat, llm=llm)

    assert "интересно: AI-видео" in llm.calls[0]["messages"][1]["content"]


def test_examples_are_capped():
    many = [{"value": 1, "title": f"Тема {n}"} for n in range(20)]
    assert format_examples(many).count("- ") == 8
    assert format_examples([]) == ""


# ================================================================== новизна

def test_cosine():
    assert cosine([1, 0], [1, 0]) == pytest.approx(1.0)
    assert cosine([1, 0], [0, 1]) == pytest.approx(0.0)
    assert cosine([], [1]) == 0.0


def test_novelty_drops_sharply_after_the_threshold():
    """Повторы должны отсекаться, а слегка похожие темы — нет (§4.7)."""
    assert novelty_from_similarity(0.0) == 1.0
    assert novelty_from_similarity(0.5) > 0.8
    assert novelty_from_similarity(0.84) > 0.65
    assert novelty_from_similarity(0.86) < 0.7
    assert novelty_from_similarity(1.0) == 0.0


async def test_repeated_topic_loses_novelty(monkeypatch: pytest.MonkeyPatch):
    from src.scoring import novelty as novelty_mod

    async def embed(text: str) -> list[float]:
        return [1.0, 0.0, 0.0]

    monkeypatch.setattr(novelty_mod, "embed_one", embed)

    fresh, _, _ = await score_novelty("Новая тема", "вывод", [])
    repeat, _, similarity = await score_novelty("Та же тема", "вывод", [[1.0, 0.0, 0.0]])

    assert fresh == 1.0
    assert repeat < 0.75 and similarity == pytest.approx(1.0)


async def test_novelty_is_neutral_when_embeddings_fail(monkeypatch: pytest.MonkeyPatch):
    """Лучше показать тему лишний раз, чем потерять её из-за сбоя модели."""
    from src.scoring import novelty as novelty_mod

    async def broken(text: str) -> list[float]:
        raise RuntimeError("нет эмбеддингов")

    monkeypatch.setattr(novelty_mod, "embed_one", broken)

    novelty, embedding, _ = await score_novelty("Тема", "вывод", [[1.0]])

    assert novelty == 0.8 and embedding is None


# ================================================================ итоговый скор

def scored(**kw: Any) -> scoring.Scored:
    defaults: dict[str, Any] = dict(
        engagement=0.5, rubric=Rubric(usefulness=5, specificity=5, relevance=5),
        novelty=1.0, normalized={},
    )
    defaults.update(kw)
    return scoring.compute(**defaults)


def test_useful_topic_beats_noisy_one():
    """Главное в ТЗ: отбор не по количеству сообщений."""
    useful = scored(engagement=0.2, rubric=Rubric(usefulness=9, specificity=9, relevance=9))
    noisy = scored(engagement=1.0, rubric=Rubric(usefulness=1, specificity=1, relevance=1))

    assert useful.score > noisy.score


def test_drama_is_penalised():
    calm = scored(rubric=Rubric(kind="insight", usefulness=8, specificity=8, relevance=8))
    drama = scored(rubric=Rubric(kind="drama", usefulness=8, specificity=8, relevance=8))

    assert drama.score < calm.score
    assert drama.features["penalty"] > 0


def test_muted_authors_are_penalised():
    normal = scored(normalized={"muted_share": 0.0})
    muted = scored(normalized={"muted_share": 1.0})

    assert muted.score < normal.score


def test_repeat_loses_to_fresh_topic():
    fresh = scored(novelty=1.0)
    repeat = scored(novelty=0.1)

    assert fresh.score > repeat.score


def test_weights_come_from_chat_settings():
    settings = {"weights": {"w_use": 1.0, "w_eng": 0.0}}
    with_weights = scored(
        settings=settings, engagement=1.0, rubric=Rubric(usefulness=10)
    )
    assert with_weights.features["weights"]["w_use"] == 1.0
    assert with_weights.score >= 1.0


def test_broken_weights_fall_back_to_defaults():
    result = scored(settings={"weights": {"w_use": "восемь", "w_zzz": 5}})
    assert result.features["weights"]["w_use"] == scoring.DEFAULT_WEIGHTS["w_use"]


def test_threshold_decides_what_is_shown():
    low = scored(settings={"threshold": 0.9}, rubric=Rubric(usefulness=5))
    high = scored(settings={"threshold": 0.1}, rubric=Rubric(usefulness=5))

    assert low.passed is False and high.passed is True


def test_features_explain_the_decision():
    """В админке должно быть видно, почему тема прошла (§4.7)."""
    result = scored()
    for key in ("engagement", "usefulness", "novelty", "weights", "threshold", "penalty"):
        assert key in result.features


def test_select_respects_threshold_and_top_n():
    items = [(f"тема {n}", scoring.Scored(score=n / 10, passed=n >= 5)) for n in range(10)]
    shown, missed = scoring.select(items, settings={"top_n": 3})

    assert len(shown) == 3
    assert shown[0] == "тема 9", "сначала самые высокие"
    assert len(missed) == 7, "остальные не выбрасываем — они нужны для /missed"


# ============================================================== пересчёт весов

def sample(value: int, **features: float) -> dict[str, Any]:
    base = dict.fromkeys(retrain.FEATURES, 0.0)
    base.update(features)
    return {"value": value, "features": base}


def test_dataset_maps_thumbs_to_labels():
    rows = [sample(1, usefulness=0.9), sample(-1, usefulness=0.1), sample(-2, usefulness=0.2)]
    features, labels = retrain.build_dataset(rows)

    assert labels == [1, 0, 0], "и 👎, и 🔕 — это «не показывать»"
    assert len(features) == 3


def test_dataset_skips_empty_features():
    features, _ = retrain.build_dataset([sample(1), sample(1, usefulness=0.5)])
    assert len(features) == 1


def test_weights_keep_the_original_scale():
    weights = retrain.weights_from_coefficients([2.0, 4.0, 0.0, 0.0, 0.0, 0.0])

    assert sum(weights.values()) == pytest.approx(sum(scoring.DEFAULT_WEIGHTS.values()), abs=0.01)
    assert weights["w_use"] > weights["w_eng"], "больший коэффициент — больший вес"


def test_weights_survive_zero_coefficients():
    assert retrain.weights_from_coefficients([0.0] * 6) == scoring.DEFAULT_WEIGHTS


async def test_retrain_refuses_on_small_sample(monkeypatch: pytest.MonkeyPatch):
    """Меньше 30 оценок — регрессия выучит шум (§4.7)."""
    async def dataset(chat_id: int | None = None) -> list[dict[str, Any]]:
        return [sample(1, usefulness=0.9) for _ in range(29)]

    monkeypatch.setattr(retrain.repo, "feedback_dataset", dataset)

    result = await retrain.retrain_chat(1)

    assert result.trained is False
    assert "29" in result.reason


async def test_retrain_refuses_when_all_marks_are_the_same(monkeypatch: pytest.MonkeyPatch):
    async def dataset(chat_id: int | None = None) -> list[dict[str, Any]]:
        return [sample(1, usefulness=0.9) for _ in range(40)]

    monkeypatch.setattr(retrain.repo, "feedback_dataset", dataset)

    result = await retrain.retrain_chat(1)
    assert result.trained is False and "одного знака" in result.reason


async def test_retrain_proposes_but_does_not_apply(monkeypatch: pytest.MonkeyPatch):
    """Веса применяет человек в админке — автоматически менять отбор нельзя."""
    async def dataset(chat_id: int | None = None) -> list[dict[str, Any]]:
        liked = [sample(1, usefulness=0.9, specificity=0.8, engagement=0.2) for _ in range(20)]
        disliked = [sample(-1, usefulness=0.1, specificity=0.1, engagement=0.9) for _ in range(20)]
        return liked + disliked

    async def get_chat_by_id(chat_id: int) -> Chat:
        return Chat(id=1, tg_id=-100, settings={})

    saved: list[dict[str, Any]] = []

    async def update_chat_settings(chat_id: int, patch: dict[str, Any]) -> dict[str, Any]:
        saved.append(patch)
        return patch

    monkeypatch.setattr(retrain.repo, "feedback_dataset", dataset)
    monkeypatch.setattr(retrain.repo, "get_chat_by_id", get_chat_by_id)
    monkeypatch.setattr(retrain.repo, "update_chat_settings", update_chat_settings)

    result = await retrain.retrain_chat(1)

    assert result.trained is True
    assert result.accuracy > 0.8, "разделимые данные должны разделиться"
    assert saved and "weights_proposal" in saved[0]
    assert "weights" not in saved[0], "текущие веса не трогаем"
    assert saved[0]["weights_proposal"]["samples"] == 40


async def test_retrain_reports_whether_it_is_better(monkeypatch: pytest.MonkeyPatch):
    async def dataset(chat_id: int | None = None) -> list[dict[str, Any]]:
        liked = [sample(1, usefulness=0.9) for _ in range(20)]
        disliked = [sample(-1, usefulness=0.1) for _ in range(20)]
        return liked + disliked

    async def get_chat_by_id(chat_id: int) -> Chat:
        return Chat(id=1, tg_id=-100, settings={})

    async def update_chat_settings(chat_id: int, patch: dict[str, Any]) -> dict[str, Any]:
        return patch

    monkeypatch.setattr(retrain.repo, "feedback_dataset", dataset)
    monkeypatch.setattr(retrain.repo, "get_chat_by_id", get_chat_by_id)
    monkeypatch.setattr(retrain.repo, "update_chat_settings", update_chat_settings)

    result = await retrain.retrain_chat(1)

    assert 0 <= result.baseline_accuracy <= 1
    assert "против" in result.describe()


def test_repeat_of_yesterday_is_filtered_out():
    """Линейного веса новизны не хватало: повтор проходил порог (§4.7)."""
    fresh = scored(
        engagement=0.75, novelty=1.0, similarity=0.1,
        rubric=Rubric(kind="insight", usefulness=7, specificity=6, relevance=9),
    )
    repeat = scored(
        engagement=0.75, novelty=0.13, similarity=0.99,
        rubric=Rubric(kind="insight", usefulness=7, specificity=6, relevance=9),
    )

    assert fresh.passed is True
    assert repeat.passed is False, "об этом уже было вчера"
    assert repeat.features["is_repeat"] is True
    assert fresh.features["is_repeat"] is False


def test_repeat_penalty_is_configurable():
    """Если повторы нужны, штраф выключается настройкой."""
    result = scored(
        similarity=0.99, novelty=0.1, engagement=0.75,
        rubric=Rubric(kind="insight", usefulness=7, specificity=6, relevance=9),
        settings={"penalties": {"repeat": 0.0}},
    )
    assert result.passed is True


def test_proposed_weights_are_plain_numbers():
    """sklearn отдаёт numpy-числа; в jsonb и в админку должны уходить обычные."""
    import numpy as np

    weights = retrain.weights_from_coefficients([np.float64(2.0), np.float64(1.0)] + [0.0] * 4)
    assert all(type(v) is float for v in weights.values())


# ============================================ повтор с новым содержанием (§4.7)

async def test_similar_topic_is_found_by_title(monkeypatch: pytest.MonkeyPatch):
    """Похожесть ищем по заголовку: вывод выдаёт рубрика, а знать надо до неё."""
    from src.scoring import novelty as novelty_mod

    async def embed(text: str) -> list[float]:
        return [1.0, 0.0] if "Seedance" in text else [0.0, 1.0]

    monkeypatch.setattr(novelty_mod, "embed_one", embed)
    recent = [
        {"title": "Seedance режет ролики", "takeaway": "ужимать до 1920",
         "embedding": [1.0, 0.0]},
        {"title": "Про конференцию", "takeaway": "", "embedding": [0.0, 1.0]},
    ]

    found, score = await novelty_mod.find_similar("Seedance опять режет", recent)

    assert found is not None and found["title"] == "Seedance режет ролики"
    assert score > 0.9


async def test_nothing_similar_returns_none(monkeypatch: pytest.MonkeyPatch):
    from src.scoring import novelty as novelty_mod

    async def embed(text: str) -> list[float]:
        return [1.0, 0.0]

    monkeypatch.setattr(novelty_mod, "embed_one", embed)

    found, score = await novelty_mod.find_similar("Тема", [
        {"title": "Другое", "embedding": [0.0, 1.0]},
    ])
    assert found is None and score < 0.85


async def test_rubric_asks_what_is_new_for_a_repeat():
    llm = fake_llm({"kind": "insight", "usefulness": 5, "what_new": "теперь и на 4K"})
    similar = {"title": "Seedance режет ролики", "takeaway": "ужимать до 1920"}

    rubric, _ = await rate_thread(
        a_thread(), Chat(id=1, tg_id=-100), similar=similar, llm=llm
    )

    prompt = llm.calls[0]["messages"][1]["content"]
    assert "Похожее уже обсуждали недавно" in prompt
    assert "Seedance режет ролики" in prompt
    assert rubric.what_new == "теперь и на 4K"


async def test_rubric_without_repeat_has_no_extra_block():
    llm = fake_llm({"kind": "insight", "usefulness": 5})
    await rate_thread(a_thread(), Chat(id=1, tg_id=-100), llm=llm)

    assert "Похожее уже обсуждали" not in llm.calls[0]["messages"][1]["content"]
