"""Реестр операций. Белый список argv был защитой от инъекции в shell;
реестр функций — та же защита по построению: имени нет в словаре, вызывать нечего."""

from collector.services.pipeline import analyze, collect, export, probe, rebuild

OPERATIONS = {
    "collect.gis": collect.gis,
    "collect.sites": collect.sites,
    "collect.instagram": collect.instagram,
    "collect.reviews": collect.reviews,
    "collect.site_pages": collect.site_pages,
    "collect.ig_comments": collect.ig_comments,
    "collect.ig_profile": collect.ig_profile,
    "analyze.reviews": analyze.reviews,
    "analyze.site": analyze.site,
    "analyze.instagram": analyze.instagram,
    "analyze.dossier": analyze.dossier,
    "rebuild": rebuild.run,
    "export": export.run,
    "probe.gis_list": probe.gis_list,
    "probe.gis_firm": probe.gis_firm,
    "probe.gis_rubrics": probe.gis_rubrics,
    "probe.serp": probe.serp,
}

PIPELINES = {
    "discover": {"title": "Поиск новых лидов", "steps": (
        "collect.gis", "collect.sites", "collect.site_pages", "collect.reviews",
        "collect.instagram", "collect.ig_comments", "collect.ig_profile",
        "rebuild", "export")},
    "classify": {"title": "Анализ и досье", "steps": (
        "analyze.reviews", "analyze.site", "analyze.instagram", "analyze.dossier",
        "rebuild", "export")},
    "rebuild": {"title": "Пересборка из сырья", "steps": ("rebuild", "export")},
}