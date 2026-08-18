"""Реестр операций. Белый список argv был защитой от инъекции в shell;
реестр функций — та же защита по построению: имени нет в словаре, вызывать нечего."""

from services.pipeline import analyze, collect, export, probe, rebuild

OPERATIONS = {
    "collect.gis": collect.gis,
    "collect.sites": collect.sites,
    "collect.instagram": collect.instagram,
    "analyze.profile": analyze.profile,
    "analyze.ig_signals": analyze.ig_signals,
    "rebuild": rebuild.run,
    "export": export.run,
    "probe.gis_list": probe.gis_list,
    "probe.gis_firm": probe.gis_firm,
    "probe.gis_rubrics": probe.gis_rubrics,
    "probe.serp": probe.serp,
}

PIPELINES = {
    "discover": {"title": "Поиск новых лидов", "steps": ("collect.gis", "collect.sites",
                 "collect.instagram", "rebuild", "export")},
    "classify": {"title": "Обогащение и оценка", "steps": ("analyze.profile",
                 "analyze.ig_signals", "rebuild", "export")},
    "rebuild": {"title": "Пересборка из сырья", "steps": ("rebuild", "export")},
}