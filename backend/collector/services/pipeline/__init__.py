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
    # Две дорожки сбора идут рядом: они не делят ни одного хоста и ни одного
    # файла — сбор кладёт страницы в raw/ (имя файла sha1 адреса) и читает
    # выдачу прошлого прогона только на чтение. Instagram платит шесть секунд
    # за запрос и тянется почти три часа; 2GIS с сайтами укладываются в сорок
    # минут и раньше просто ждали своей очереди. Инвариант «один воркер»
    # касается rebuild, который пересобирает derived прогоном, — он и остаётся
    # отдельной стадией после всего сбора.
    "discover": {"title": "Поиск новых лидов", "steps": (
        "collect.gis",
        (("collect.reviews", "collect.sites", "collect.site_pages"),
         ("collect.instagram", "collect.ig_comments", "collect.ig_profile")),
        "rebuild", "export")},
    "classify": {"title": "Анализ и досье", "steps": (
        "analyze.reviews", "analyze.site", "analyze.instagram", "analyze.dossier",
        "rebuild", "export")},
    "rebuild": {"title": "Пересборка из сырья", "steps": ("rebuild", "export")},
}