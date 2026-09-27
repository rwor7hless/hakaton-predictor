"""Уровень риска, предполагаемые причины сбоя и рекомендации диспетчеру.

Причины определяются правилами по производным признакам телеметрии (простой,
скорость на участке, накопленное опоздание, качество GPS) и дополняются
вкладами признаков модели (SHAP) — так объяснение согласовано с прогнозом.
"""
from __future__ import annotations

import math


def _fmt(sec: float) -> str:
    sign = "+" if sec >= 0 else "−"
    sec = abs(int(round(sec)))
    return f"{sign}{sec // 60}:{sec % 60:02d}"


# Правила светофора по порядку проверки: (уровень, правило, порог, сработало ли).
def _rules(delay: float, p_late: float, p_early: float) -> list[tuple[str, str, float, float, bool]]:
    return [
        ("red", "delay_late", delay, 180.0, delay >= 180),
        ("red", "p_late", p_late, 0.65, p_late >= 0.65),
        ("yellow", "delay_late", delay, 90.0, delay >= 90),
        ("yellow", "p_late", p_late, 0.4, p_late >= 0.4),
        ("yellow", "delay_early", delay, -90.0, delay <= -90),
        ("yellow", "p_early", p_early, 0.6, p_early >= 0.6),
    ]


def risk_level(delay: float, p_late: float, p_early: float) -> str:
    """Светофор риска: красный — вероятное опоздание, жёлтый — умеренный риск или опережение."""
    return next((lvl for lvl, *_, hit in _rules(delay, p_late, p_early) if hit), "green")


def risk_reason(delay: float, p_late: float, p_early: float) -> dict | None:
    """Какое правило светофора сработало: ``{risk, rule, value, threshold}``; ``None`` для зелёного."""
    for lvl, rule, value, thr, hit in _rules(delay, p_late, p_early):
        if hit:
            return {"risk": lvl, "rule": rule, "value": round(float(value), 3), "threshold": thr}
    return None


def _num(f: dict, k: str) -> float | None:
    v = f.get(k)
    if v is None:
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def detect_causes(f: dict, delay: float, contributions: list[dict]) -> list[dict]:
    """Список причин ``{code, text, weight}`` по убыванию значимости (не более трёх)."""
    out: list[dict] = []
    dwell = _num(f, "dwell") or 0.0
    at_stop = (_num(f, "at_stop") or 0.0) > 0.5
    est = _num(f, "est_now")
    spd5 = _num(f, "spd5")
    stop5 = _num(f, "stop5")
    gps_age = _num(f, "gps_age")
    valid = _num(f, "valid_share5")
    prev = _num(f, "prev_loop_delta")
    if gps_age is None or gps_age > 120 or (valid is not None and valid < 0.5):
        out.append({"code": "gps_lost", "text": "Нет устойчивого GPS-сигнала — прогноз по последнему состоянию и профилю",
                    "weight": 0.5})
    if dwell >= 90 and not at_stop:
        out.append({"code": "dwell_off_stop", "text": f"Простой {dwell / 60:.0f} мин вне остановки (затор, светофор, ДТП)",
                    "weight": min(1.0, dwell / 300)})
    elif dwell >= 60 and at_stop:
        out.append({"code": "long_boarding", "text": f"Длительная посадка/высадка на остановке ({dwell / 60:.0f} мин)",
                    "weight": min(1.0, dwell / 240)})
    if spd5 is not None and stop5 is not None and spd5 < 8 and stop5 > 0.5 and dwell < 90:
        out.append({"code": "slow_segment", "text": f"Низкая скорость на участке: {spd5:.0f} км/ч, {stop5 * 100:.0f}% времени в простое",
                    "weight": 0.6})
    since = _num(f, "since_last")
    reliable = est is not None and since is not None and since <= 600 and abs(est) < 900
    if not reliable:
        est = None
    if est is not None and est >= 120 and delay >= 60:
        out.append({"code": "accumulated", "text": f"Накопленное опоздание {_fmt(est)} распространяется по маршруту",
                    "weight": min(1.0, est / 400)})
    if est is not None and est <= -60 and delay <= 0:
        out.append({"code": "ahead", "text": f"ТС идёт с опережением графика {_fmt(est)}", "weight": min(1.0, -est / 300)})
    if prev is not None and abs(prev) >= 90:
        out.append({"code": "prev_loop",
                    "text": f"На прошлом рейсе этот участок дал {_fmt(prev)} к отклонению", "weight": min(1.0, abs(prev) / 300)})
    for c in contributions:
        if c.get("feature") == "tgt_stop" and abs(c.get("shap", 0)) >= 40:
            txt = ("Расписание систематически недооценивает время хода до этой остановки" if c["shap"] > 0
                   else "В расписании заложен запас времени до этой остановки (риск опережения)")
            out.append({"code": "schedule_profile", "text": txt, "weight": min(1.0, abs(c["shap"]) / 150)})
    if not out and contributions:
        c = contributions[0]
        out.append({"code": "model", "text": f"Главный фактор модели: {c.get('text', c.get('feature'))}",
                    "weight": 0.3})
    out.sort(key=lambda x: -x["weight"])
    return [{**c, "weight": round(float(c["weight"]), 2)} for c in out[:3]]


def recommendations(risk: str, delay: float, causes: list[dict], target_name: str, segment: dict | None) -> list[str]:
    """Упреждающие меры для карточки инцидента."""
    codes = {c["code"] for c in causes}
    seg = f"«{segment['from_stop']}» → «{segment['to_stop']}»" if segment else "участке"
    rec: list[str] = []
    if risk == "green":
        return ["Вмешательство не требуется, продолжать мониторинг"]
    if delay >= 90:
        if "dwell_off_stop" in codes or "slow_segment" in codes:
            rec.append(f"Проверить дорожную ситуацию на участке {seg}, при заторе — согласовать объезд")
            rec.append(f"Запросить приоритет проезда на светофорах участка {seg}")
        if "long_boarding" in codes:
            rec.append("Организовать посадку через все двери / направить дополнительное ТС на линию")
        if "schedule_profile" in codes:
            rec.append("Скорректировать время хода в расписании на этом участке")
        if delay >= 180:
            rec.append("Выпустить резервное ТС с ближайшей конечной для восстановления интервала")
        rec.append(f"Оповестить пассажиров на остановке «{target_name}» об опоздании ~{_fmt(delay)}")
    elif delay <= -60:
        rec.append(f"Дать водителю указание на удержание ~{int(-delay)} с на ближайшей остановке")
        rec.append("Проверить соблюдение интервала с попутными ТС маршрута")
    else:
        rec.append("Усилить мониторинг ТС, при росте отклонения — меры по интервалу")
    return rec[:4]
