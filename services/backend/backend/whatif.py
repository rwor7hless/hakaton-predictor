"""What-if анализ: как изменится прогноз при управляющем воздействии диспетчера.

Сценарии пересчитывают признаки текущей точки и заново вызывают ту же модель:

* ``extra_vehicle`` — выпуск резервного ТС: рейс выполняет машина, идущая по
  графику (обнуляются признаки накопленного отклонения и простоя);
* ``dwell_cut`` — сокращение стоянки/простоя на ``value`` секунд;
* ``signal_priority`` — приоритет на светофорах: время хода до цели
  сокращается на ``value`` процентов.
"""
from __future__ import annotations

import math

from .causes import risk_level

DEV_KEYS = ("est_now", "dev_last", "lp_dev", "dev_mean5", "dev_max15", "lb_next", "lp_lb")


def scenario_features(f: dict, scenario: str, value: float | None) -> tuple[dict, str]:
    """Вернуть изменённые признаки и текстовое пояснение сценария."""
    g = dict(f)
    if scenario == "extra_vehicle":
        for k in DEV_KEYS:
            if g.get(k) is not None:
                g[k] = 0.0
        g["dwell"], g["stop5"], g["dev_slope5"] = 0.0, 0.0, 0.0
        return g, "Рейс выполняет резервное ТС, выпущенное с конечной по графику"
    if scenario == "dwell_cut":
        cut = float(value or 60)
        for k in DEV_KEYS:
            v = g.get(k)
            if v is not None and isinstance(v, (int, float)) and math.isfinite(v):
                g[k] = v - cut
        g["dwell"] = max(0.0, float(g.get("dwell") or 0.0) - cut)
        return g, f"Простой/стоянка сокращены на {int(cut)} с (посадка через все двери, регулировщик)"
    if scenario == "signal_priority":
        return g, f"Приоритет на светофорах: время хода до цели −{int(value or 15)}%"
    raise ValueError(f"неизвестный сценарий {scenario}")


def apply_post(scenario: str, value: float | None, delay: float, f: dict, base: float) -> float:
    """Итоговая задержка сценария.

    Для ``signal_priority`` модель не видит воздействие напрямую — сокращается
    плановое время хода до цели. Меры диспетчера не ухудшают прогноз: результат
    ограничивается базовым значением со стороны нарушения графика.
    """
    if scenario == "signal_priority":
        run = f.get("plan_run") or f.get("lead") or 0.0
        delay = base - float(value or 15) / 100.0 * float(run)
    return min(delay, base) if base >= 0 else max(delay, base)


def summarize(base: dict, after_delay: float, p_late: float, p_early: float, text: str) -> dict:
    """Ответ what-if в формате контракта."""
    risk_after = risk_level(after_delay, min(p_late, 0.39), min(p_early, 0.59))
    diff = after_delay - base["delay_s"]
    return {"base": base, "delay_s_after": round(after_delay, 1), "risk_after": risk_after,
            "explanation": f"{text}. Прогноз задержки на «{base['target_stop_name']}»: "
                           f"{base['delay_s']:+.0f} с → {after_delay:+.0f} с ({diff:+.0f} с)."}
