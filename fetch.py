#!/usr/bin/env python3
"""PronoTurf - robot de collecte PMU + modele de pronostic.

Lance par GitHub Actions plusieurs fois par jour.
Ecrit data/AAAA-MM-JJ.json (pronostics), data/index.json, data/stats.json, data/model.json
et un cache (cache/...) pour ne pas re-telecharger les anciennes courses.
"""
import json, math, os, random, re, sys, time, unicodedata, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    PARIS = ZoneInfo("Europe/Paris")
except Exception:  # pragma: no cover
    PARIS = timezone(timedelta(hours=1))

BASE = "https://online.turfinfo.api.pmu.fr/rest/client"
HEADERS = {"User-Agent": "Mozilla/5.0 (PronoTurf)", "Accept": "application/json"}
ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
CACHE = os.path.join(ROOT, "cache")
PROBE = os.environ.get("PROBE_DIR")  # tests locaux
STATS = {"calls": 0, "errors": 0}

# ----------------------------------------------------------------- reseau
def get(url, tries=3):
    if PROBE:
        return probe_get(url)
    for i in range(tries):
        try:
            STATS["calls"] += 1
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as r:
                if r.status == 204:
                    return None
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (204, 404):
                return None
            time.sleep(1.5 * (i + 1))
        except Exception:
            time.sleep(1.5 * (i + 1))
    STATS["errors"] += 1
    return None


def probe_get(url):
    m = re.search(r"programme/(\d{8})(/R(\d+)/C(\d+)/([a-z-]+))?", url)
    if not m:
        return None
    if not m.group(2):
        f = os.path.join(PROBE, "programme.json")
        return json.load(open(f)) if m.group(1) == "11112026" else None
    for f in os.listdir(PROBE):
        if f"_R{m.group(3)}C{m.group(4)}_" in f and f.endswith(m.group(5) + ".json") and m.group(1) == "11112026":
            return json.load(open(os.path.join(PROBE, f)))
    return None


def pmap(fn, items, workers=8):
    items = list(items)
    if not items:
        return []
    with ThreadPoolExecutor(workers) as ex:
        return list(ex.map(fn, items))


def jload(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def jsave(path, obj, pretty=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        if pretty:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        else:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)

# ----------------------------------------------------------------- outils
def dkey(d):  # date -> DDMMYYYY (format PMU)
    return d.strftime("%d%m%Y")


def iso(d):
    return d.strftime("%Y-%m-%d")


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().upper()
    s = re.sub(r"\(.*?\)", " ", s)
    return re.sub(r"[^A-Z0-9]+", " ", s).strip()


def local_date(ms, offset):
    return (datetime.fromtimestamp((ms + (offset or 0)) / 1000, tz=timezone.utc)).date()


TERRAIN_ORDRE = [
    ("TRES SOUPLE", 3), ("BON SOUPLE", 1), ("BON LEGER", -0.5), ("SEC", -1), ("LEGER", -1),
    ("BON", 0), ("SOUPLE", 2), ("COLLANT", 4), ("LOURD", 5), ("PSF", None),
]


def terrain_val(t):
    """Etat du terrain -> echelle numerique (0 = bon, 5 = lourd). None pour PSF / inconnu."""
    t = norm(t)
    if not t:
        return None
    for k, v in TERRAIN_ORDRE:
        if k in t:
            return v
    return None


def terrain_label(t):
    return (t or "").strip().capitalize() or None


def piste_of(piste, terrain):
    t = norm(terrain)
    if "PSF" in t:
        return "PSF"
    return piste or None


def cat_group(cat):
    c = cat or ""
    if "RECLAMER" in c:
        return "A_RECLAMER"
    if "HANDICAP" in c:
        return "HANDICAP"
    if "GROUPE" in c or "LISTED" in c or "LISTE" in c:
        return "GROUPE_LISTED"
    if "CONDITION" in c:
        return "A_CONDITIONS"
    if "INCONNU" in c or not c:
        return None
    return "AUTRE"


CAT_LABEL = {
    "A_RECLAMER": "à réclamer", "HANDICAP": "handicap", "GROUPE_LISTED": "groupe / listed",
    "A_CONDITIONS": "à conditions", "AUTRE": "autre",
}
DISC_LABEL = {"ATTELE": "Trot attelé", "MONTE": "Trot monté", "PLAT": "Plat", "HAIE": "Haies",
              "STEEPLECHASE": "Steeple-chase", "CROSS": "Cross"}

# ----------------------------------------------------------------- programmes (avec cache)
def compact_programme(prog):
    """Programme brut -> index compact des courses."""
    out = []
    for reu in (prog or {}).get("programme", {}).get("reunions", []):
        for c in reu.get("courses", []):
            pen = c.get("penetrometre") or {}
            out.append({
                "r": reu.get("numOfficiel"), "c": c.get("numOrdre"),
                "prix": c.get("libelle"), "n": norm(c.get("libelle")),
                "h": (reu.get("hippodrome") or {}).get("libelleCourt"),
                "disc": c.get("discipline"), "dist": c.get("distance"),
                "corde": c.get("corde"), "piste": c.get("typePiste"),
                "cat": c.get("categorieParticularite"),
                "terrain": pen.get("intitule"),
            })
    return out


def past_programme(d):
    """Index compact du programme d'un jour passe (cache disque permanent)."""
    path = os.path.join(CACHE, "prog", iso(d) + ".json")
    cached = jload(path)
    if cached is not None:
        return cached
    prog = get(f"{BASE}/1/programme/{dkey(d)}?specialisation=INTERNET")
    if prog is None:
        return []
    idx = compact_programme(prog)
    jsave(path, idx)
    return idx


def find_race(idx, prix, disc, dist):
    n = norm(prix)
    cands = [x for x in idx if x["n"] == n]
    if not cands:
        cands = [x for x in idx if n and x["n"] and (x["n"].startswith(n) or n.startswith(x["n"]))]
    if len(cands) > 1:
        cands = [x for x in cands if x["disc"] == disc and x["dist"] == dist] or cands
    if not cands:
        c2 = [x for x in idx if x["disc"] == disc and x["dist"] == dist]
        if len(c2) == 1:
            cands = c2
    return cands[0] if cands else None

# ----------------------------------------------------------------- performances
def compact_perf(course):
    """Une course passee (format performances-detaillees) -> dict compact pour le cheval 'itsHim'."""
    me = next((p for p in course.get("participants", []) if p.get("itsHim")), None)
    if not me:
        return None
    pl = me.get("place") or {}
    d = local_date(course["date"], course.get("timezoneOffset"))
    return {
        "d": iso(d), "h": course.get("hippodrome"), "prix": course.get("nomPrix"),
        "disc": course.get("discipline"), "dist": course.get("distance"),
        "alloc": course.get("allocation"), "nb": course.get("nbParticipants"),
        "terrain": course.get("etatTerrain"), "pl": pl.get("place"),
        "st": pl.get("statusArrivee"), "raw": pl.get("rawValue"),
        "kg": me.get("poidsJockey"), "dp": me.get("distanceParcourue"), "num": me.get("numPmu"),
        "jockey": me.get("nomJockey"),
    }


def perfs_by_name(perfdoc):
    res = {}
    for p in (perfdoc or {}).get("participants", []) or []:
        lst = [x for x in (compact_perf(c) for c in p.get("coursesCourues", []) or []) if x]
        res[norm(p.get("nomCheval"))] = lst
    return res


def past_race_perfs(d, r, c):
    """Perfs (5 courses precedentes) de tous les partants d'une course passee. Cache permanent."""
    path = os.path.join(CACHE, "perf", f"{iso(d)}_R{r}C{c}.json")
    cached = jload(path)
    if cached is not None:
        return cached
    doc = get(f"{BASE}/61/programme/{dkey(d)}/R{r}/C{c}/performances-detaillees/pretty")
    res = perfs_by_name(doc) if doc else {}
    jsave(path, res)
    return res


def past_race_values(d, r, c):
    """Valeur handicap + poids de chaque partant d'une course passee. Cache permanent."""
    path = os.path.join(CACHE, "part", f"{iso(d)}_R{r}C{c}.json")
    cached = jload(path)
    if cached is not None:
        return cached
    doc = get(f"{BASE}/1/programme/{dkey(d)}/R{r}/C{c}/participants")
    res = {}
    for p in (doc or {}).get("participants", []) or []:
        res[str(p.get("numPmu"))] = {"val": p.get("handicapValeur"), "kg": p.get("handicapPoids")}
    jsave(path, res)
    return res


def enrich(perf, progs):
    """Ajoute corde / piste / type de course / terrain a une perf passee via le programme du jour."""
    idx = progs.get(perf["d"])
    if not idx:
        return perf
    race = find_race(idx, perf["prix"], perf["disc"], perf["dist"])
    if race:
        perf["corde"] = race.get("corde")
        perf["piste"] = race.get("piste")
        perf["cat"] = race.get("cat")
        perf["r"], perf["c"] = race["r"], race["c"]
        if not perf.get("terrain"):
            perf["terrain"] = race.get("terrain")
    return perf

# ----------------------------------------------------------------- modele
RECENCE = 0.85
ALLURE_TROT = ("ATTELE", "MONTE")


def perf_value(p, alloc_now):
    """Note d'une performance passee entre 0 et ~1.1."""
    pl, nb = p.get("pl"), p.get("nb") or 10
    if pl:
        v = 1 - (pl - 1) / max(3, min(nb, 18) - 1)
        v = max(0.0, v)
        if pl <= 3:
            v = max(v, (1.0, 0.8, 0.68)[pl - 1])
    else:
        st = (p.get("st") or "") + (p.get("raw") or "")
        v = 0.15 if "DISQUALIFIE" in st or st.startswith("D") else 0.08
    a = p.get("alloc") or 0
    if a and alloc_now:
        ratio = math.log2(a / alloc_now)
        v += 0.1 * max(-1.5, min(1.5, ratio))
    return max(0.0, min(1.15, v))


def wmean(items):
    """items = [(valeur, poids)]"""
    sw = sum(w for _, w in items)
    return (sum(v * w for v, w in items) / sw, sw) if sw else (None, 0)


def subscore(perfs, pred, base, prior=1.5):
    """Moyenne des perfs qui verifient pred, tiree vers base si peu d'exemples."""
    items = [(p["_v"], p["_w"]) for p in perfs if pred(p)]
    m, sw = wmean(items)
    n = len(items)
    if not n:
        return base, 0, None
    s = (m * sw + base * prior) / (sw + prior)
    return s, n, m


def same_dist(a, b, trot):
    if not a or not b:
        return False
    return abs(a - b) <= (200 if trot else max(200, 0.12 * b))


def same_terrain(a, b):
    va, vb = terrain_val(a), terrain_val(b)
    if va is None or vb is None:
        return norm(a) == norm(b) and bool(a)
    return abs(va - vb) <= 1


def fmt_pct(x):
    return round(100 * x, 1)


def analyse_horse(h, course, perfs, trot, race_min_dist):
    """Calcule la note d'un cheval + facteurs + alertes."""
    alloc_now = course.get("montantPrix") or 0
    perfs = sorted(perfs, key=lambda p: p["d"], reverse=True)
    for i, p in enumerate(perfs):
        p["_v"] = perf_value(p, alloc_now)
        p["_w"] = RECENCE ** i
    last5 = perfs[:5]
    factors, alerts = [], []
    form, _ = wmean([(p["_v"], p["_w"]) for p in last5])
    nhist = len(last5)
    if form is None:
        form = 0.32
        alerts.append({"t": "info", "m": "Aucune course connue (inédit ou étranger)"})
    musique5 = " ".join(fmt_place(p) for p in last5)
    factors.append({"k": "Forme (5 dernières)", "v": musique5 or "—", "s": round(form, 3)})
    score = form
    dist = h.get("distance") or course.get("distance")

    if trot:
        # corde
        corde = course.get("corde")
        known = [p for p in last5 if p.get("corde")]
        if corde and known:
            s, n, m = subscore(known, lambda p: p.get("corde") == corde, form)
            lab = "à droite" if corde == "CORDE_DROITE" else "à gauche"
            factors.append({"k": f"Corde {lab}", "v": f"{n} course(s) sur {len(known)}" + (f", note {m:.2f}" if m is not None else ""), "s": round(s - form, 3)})
            score += 0.6 * (s - form)
            if n == 0 and len(known) >= 3:
                alerts.append({"t": "warn", "m": f"Aucune course récente avec la corde {lab}"})
        # distance
        s, n, m = subscore(last5, lambda p: same_dist(p.get("dp") or p.get("dist"), dist, True), form)
        factors.append({"k": f"Distance {dist} m", "v": f"{n} course(s) à ±200 m", "s": round(s - form, 3)})
        score += 0.7 * (s - form)
        # recul
        recul = (h.get("distance") or 0) - (race_min_dist or 0)
        if recul > 0:
            pen = 0.05 * recul / 25
            score -= pen
            factors.append({"k": "Recul", "v": f"{recul} m", "s": -round(pen, 3)})
            alerts.append({"t": "warn", "m": f"Part avec {recul} m de recul"})
    else:
        piste = piste_of(course.get("typePiste"), (course.get("penetrometre") or {}).get("intitule"))
        terrain = (course.get("penetrometre") or {}).get("intitule")
        cat = cat_group(course.get("categorieParticularite"))
        # 5 dernieres sur le meme type de piste (historique jusqu'a 10 courses)
        known_p = [p for p in perfs if piste_of(p.get("piste"), p.get("terrain"))]
        same_p = [p for p in known_p if piste and piste_of(p.get("piste"), p.get("terrain")) == piste][:5]
        if piste and known_p:
            if same_p:
                fp, _ = wmean([(p["_v"], RECENCE ** i) for i, p in enumerate(same_p)])
                k = len(same_p)
                sp = (fp * k + form * 1.5) / (k + 1.5)
            else:
                sp, k = form, 0
            factors.append({"k": f"Piste {piste.lower()} (5 dernières)", "v": (" ".join(fmt_place(p) for p in same_p) or "jamais couru"), "s": round(sp - form, 3)})
            score = 0.5 * form + 0.5 * sp
            if k == 0 and len(known_p) >= 2:
                alerts.append({"t": "warn", "m": f"Jamais couru récemment sur piste {piste.lower()}"})
        base = score
        # terrain
        if terrain and "PSF" not in norm(terrain):
            s, n, m = subscore(perfs, lambda p: same_terrain(p.get("terrain"), terrain), base)
            factors.append({"k": f"Terrain {terrain_label(terrain)}", "v": f"{n} course(s) sur terrain proche", "s": round(s - base, 3)})
            score += 0.6 * (s - base)
            if n == 0 and nhist >= 3:
                alerts.append({"t": "warn", "m": f"Jamais couru sur un terrain {terrain_label(terrain).lower()} récemment"})
        # distance
        plat = course.get("discipline") == "PLAT"
        s, n, m = subscore(perfs, lambda p: (p.get("disc") == "PLAT") == plat and same_dist(p.get("dist"), dist, False), base)
        factors.append({"k": f"Distance {dist} m", "v": f"{n} course(s) sur distance proche", "s": round(s - base, 3)})
        score += 0.6 * (s - base)
        # type de course
        known_c = [p for p in perfs if cat_group(p.get("cat"))]
        if cat and known_c:
            s, n, m = subscore(known_c, lambda p: cat_group(p.get("cat")) == cat, base)
            factors.append({"k": f"Type : {CAT_LABEL.get(cat, cat)}", "v": f"{n} course(s) du même type sur {len(known_c)}", "s": round(s - base, 3)})
            score += 0.6 * (s - base)
        # poids
        kg_now = h.get("kg")
        last_kg = next((p["kg"] for p in perfs if p.get("kg") and (p.get("disc") == "PLAT") == plat), None)
        if kg_now and last_kg:
            dk = round(kg_now - last_kg, 1)
            pen = 0.012 * dk if dk > 0 else 0.008 * dk
            score -= pen
            factors.append({"k": "Poids", "v": f"{kg_now:g} kg ({'+' if dk > 0 else ''}{dk:g} kg vs dernière course)", "s": -round(pen, 3)})
            if dk >= 1.5:
                alerts.append({"t": "warn", "m": f"Prend {dk:g} kg de plus que lors de sa dernière course"})
        # valeur handicap
        v_now, v_prev = h.get("valeur"), h.get("valeur_prev")
        if v_now is not None:
            if v_prev is not None:
                dv = round(v_now - v_prev, 1)
                pen = 0.015 * dv if dv > 0 else 0.008 * dv
                score -= pen
                factors.append({"k": "Valeur handicap", "v": f"{v_now:g} ({'+' if dv > 0 else ''}{dv:g} vs dernière course)", "s": -round(pen, 3)})
                if dv > 0:
                    alerts.append({"t": "alert", "m": f"MONTE EN HANDICAP : valeur {v_prev:g} → {v_now:g} (+{dv:g})"})
                elif dv < 0:
                    alerts.append({"t": "good", "m": f"Descend en handicap : valeur {v_prev:g} → {v_now:g} ({dv:g})"})
            else:
                factors.append({"k": "Valeur handicap", "v": f"{v_now:g} (pas de valeur connue avant)", "s": 0})
    # fraicheur : temps depuis la derniere course (course de rentree)
    ref = course.get("_date")
    if perfs and ref:
        last_d = datetime.strptime(perfs[0]["d"], "%Y-%m-%d").date()
        jours = (ref - last_d).days
        if jours > 60:
            pen = min(0.45, 0.03 + 0.0011 * (jours - 60))
            score -= pen
        else:
            pen = 0.0
        mois = jours // 30
        duree = f"{jours} jours" if jours < 60 else f"{mois} mois"
        factors.append({"k": "Fraîcheur", "v": f"dernière course il y a {duree} ({last_d.strftime('%d/%m/%Y')})", "s": -round(pen, 3)})
        if jours > 180:
            alerts.append({"t": "alert", "m": f"RENTRÉE : n'a pas couru depuis {mois} mois (dernière course le {last_d.strftime('%d/%m/%Y')})"})
        elif jours > 90:
            alerts.append({"t": "warn", "m": f"Rentrée : n'a pas couru depuis {mois} mois"})
    # peu d'historique : on tire vers la moyenne
    if nhist < 3:
        score = (score * nhist + 0.35 * (3 - nhist)) / 3
        if nhist:
            alerts.append({"t": "info", "m": f"Seulement {nhist} course(s) connue(s)"})
    return score, factors, alerts


def fmt_place(p):
    letter = {"ATTELE": "a", "MONTE": "m", "PLAT": "p", "HAIE": "h", "STEEPLECHASE": "s", "CROSS": "c"}.get(p.get("disc"), "")
    if p.get("pl"):
        x = str(p["pl"]) if p["pl"] < 10 else "0"
    else:
        st = (p.get("st") or "")
        x = "D" if "DISQUALIFIE" in st else ("T" if "TOMBE" in st else "A")
    return x + letter


def probabilities(scores, k, sims=6000, seed=42):
    """Proba de victoire (softmax) et de top 3 (Plackett-Luce, simulation)."""
    if not scores:
        return [], []
    mx = max(scores)
    ex = [math.exp(k * (s - mx)) for s in scores]
    tot = sum(ex)
    pw = [e / tot for e in ex]
    n = len(scores)
    if n <= 3:
        return pw, [1.0] * n
    rnd = random.Random(seed)
    top3 = [0] * n
    for _ in range(sims):
        w = ex[:]
        rest = tot
        for _pos in range(3):
            x = rnd.random() * rest
            acc = 0.0
            for i in range(n):
                if w[i] <= 0:
                    continue
                acc += w[i]
                if acc >= x:
                    top3[i] += 1
                    rest -= w[i]
                    w[i] = 0
                    break
    return pw, [t / sims for t in top3]

# ----------------------------------------------------------------- traitement d'un jour
def process_day(d, model):
    prog = get(f"{BASE}/1/programme/{dkey(d)}?meteo=true&specialisation=INTERNET")
    reunions = (prog or {}).get("programme", {}).get("reunions", [])
    if not reunions:
        print(f"{iso(d)} : pas de programme")
        return None
    out_path = os.path.join(DATA, iso(d) + ".json")
    previous = jload(out_path, {}) or {}
    prev_courses = {(c["r"], c["c"]): c for reu in previous.get("reunions", []) for c in reu.get("courses", [])}

    jobs = []
    for reu in reunions:
        for c in reu.get("courses", []):
            jobs.append((reu, c))

    def fetch_course(job):
        reu, c = job
        r, n = reu["numOfficiel"], c["numOrdre"]
        old = prev_courses.get((r, n))
        # course deja analysee et terminee : on ne refait que l'arrivee
        if old and old.get("arrivee") and old.get("chevaux"):
            return job, None, None, old
        part = get(f"{BASE}/1/programme/{dkey(d)}/R{r}/C{n}/participants")
        perf = get(f"{BASE}/61/programme/{dkey(d)}/R{r}/C{n}/performances-detaillees/pretty")
        return job, part, perf, old

    fetched = pmap(fetch_course, jobs)

    # dates passees a resoudre
    perfs_map = {}
    for (reu, c), part, perf, old in fetched:
        if part is not None:
            perfs_map[(reu["numOfficiel"], c["numOrdre"])] = perfs_by_name(perf)
    dates = set()
    for pm in perfs_map.values():
        for lst in pm.values():
            for p in lst:
                dates.add(p["d"])
    progs = {}
    for ds, idx in zip(sorted(dates), pmap(lambda s: past_programme(datetime.strptime(s, "%Y-%m-%d").date()), sorted(dates))):
        progs[ds] = idx
    for pm in perfs_map.values():
        for lst in pm.values():
            for p in lst:
                enrich(p, progs)

    # galop : historique plus profond (10 courses) + valeur handicap precedente
    deep_jobs, val_jobs = set(), set()
    for (reu, c), part, perf, old in fetched:
        if part is None or c.get("specialite", "").startswith("TROT"):
            continue
        pm = perfs_map.get((reu["numOfficiel"], c["numOrdre"]), {})
        for lst in pm.values():
            if lst:
                oldest = min(lst, key=lambda p: p["d"])
                if oldest.get("r"):
                    deep_jobs.add((oldest["d"], oldest["r"], oldest["c"]))
                newest = max(lst, key=lambda p: p["d"])
                if newest.get("r"):
                    val_jobs.add((newest["d"], newest["r"], newest["c"]))
    deep = dict(zip(sorted(deep_jobs), pmap(lambda j: past_race_perfs(datetime.strptime(j[0], "%Y-%m-%d").date(), j[1], j[2]), sorted(deep_jobs))))
    vals = dict(zip(sorted(val_jobs), pmap(lambda j: past_race_values(datetime.strptime(j[0], "%Y-%m-%d").date(), j[1], j[2]), sorted(val_jobs))))
    extra_dates = {p["d"] for lst_map in deep.values() for lst in lst_map.values() for p in lst} - set(progs)
    for ds, idx in zip(sorted(extra_dates), pmap(lambda s: past_programme(datetime.strptime(s, "%Y-%m-%d").date()), sorted(extra_dates))):
        progs[ds] = idx

    out_reunions = []
    for reu in reunions:
        out_c = []
        for (r2, c), part, perf, old in fetched:
            if r2 is not reu:
                continue
            arrivee = [x[0] if isinstance(x, list) and x else x for x in (c.get("ordreArrivee") or [])]
            if part is None:
                if old:
                    old["arrivee"] = arrivee or old.get("arrivee")
                    old["statut"] = c.get("statut")
                    out_c.append(old)
                continue
            out_c.append(build_course(reu, c, part, perfs_map.get((reu["numOfficiel"], c["numOrdre"]), {}), deep, vals, progs, model, arrivee, old))
        out_reunions.append({
            "r": reu["numOfficiel"], "hippo": (reu.get("hippodrome") or {}).get("libelleCourt"),
            "pays": (reu.get("pays") or {}).get("code"), "courses": out_c,
        })
    doc = {"date": iso(d), "maj": datetime.now(PARIS).strftime("%Y-%m-%d %H:%M"), "reunions": out_reunions}
    jsave(out_path, doc)
    print(f"{iso(d)} : {sum(len(r['courses']) for r in out_reunions)} courses")
    return doc


def build_course(reu, c, part, pm, deep, vals, progs, model, arrivee, old):
    trot = (c.get("specialite") or "").startswith("TROT")
    partants = [p for p in (part or {}).get("participants", []) if p.get("statut") != "NON_PARTANT"]
    nonpartants = [p.get("numPmu") for p in (part or {}).get("participants", []) if p.get("statut") == "NON_PARTANT"]
    c["_date"] = c.get("_date") or (datetime.fromtimestamp(c["heureDepart"] / 1000, tz=PARIS).date() if c.get("heureDepart") else datetime.now(PARIS).date())
    race_min = min((p.get("handicapDistance") or c.get("distance") or 0) for p in partants) if (trot and partants) else None
    horses = []
    for p in partants:
        name = norm(p.get("nom"))
        perfs = [dict(x) for x in pm.get(name, [])]
        if not trot and perfs:
            oldest = min(perfs, key=lambda x: x["d"])
            more = deep.get((oldest["d"], oldest.get("r"), oldest.get("c")), {}).get(name, [])
            seen = {x["d"] for x in perfs}
            for x in more:
                if x["d"] not in seen:
                    perfs.append(enrich(dict(x), progs))
        h = {"distance": p.get("handicapDistance") if trot else c.get("distance")}
        if not trot:
            kg = p.get("poidsConditionMonte") if p.get("poidsConditionMonteChange") and p.get("poidsConditionMonte") else p.get("handicapPoids")
            h["kg"] = kg / 10 if kg else None
            h["valeur"] = p.get("handicapValeur")
            if perfs:
                newest = max(perfs, key=lambda x: x["d"])
                v = vals.get((newest["d"], newest.get("r"), newest.get("c")), {}).get(str(newest.get("num")))
                if v and v.get("val") is not None:
                    h["valeur_prev"] = v["val"]
        score, factors, alerts = analyse_horse(h, c, perfs, trot, race_min)
        horses.append({
            "n": p.get("numPmu"), "nom": p.get("nom"), "age": p.get("age"), "sexe": (p.get("sexe") or "")[:1],
            "jockey": p.get("driver"), "entr": p.get("entraineur"), "musique": p.get("musique"),
            "corde": p.get("placeCorde"), "kg": h.get("kg"), "valeur": h.get("valeur"),
            "dist": h["distance"] if trot else None, "oeill": p.get("oeilleres"), "def": p.get("deferre"),
            "score": round(score, 4), "f": factors, "a": alerts,
            "casaque": p.get("urlCasaque"),
        })
    k = model.get("k", 5.0)
    pw, p3 = probabilities([x["score"] for x in horses], k)
    for x, a, b in zip(horses, pw, p3):
        x["pw"], x["p3"] = fmt_pct(a), fmt_pct(b)
    horses.sort(key=lambda x: -x["pw"])
    pen = c.get("penetrometre") or {}
    return {
        "r": reu["numOfficiel"], "c": c["numOrdre"], "nom": c.get("libelle"),
        "heure": datetime.fromtimestamp(c["heureDepart"] / 1000, tz=PARIS).strftime("%H:%M") if c.get("heureDepart") else None,
        "disc": c.get("discipline"), "discLabel": DISC_LABEL.get(c.get("discipline"), c.get("discipline")),
        "dist": c.get("distance"), "corde": c.get("corde"), "piste": c.get("typePiste"),
        "terrain": pen.get("intitule"), "cat": c.get("categorieParticularite"),
        "prix": c.get("montantPrix"), "partants": len(horses), "np": nonpartants,
        "statut": c.get("statut"), "arrivee": arrivee, "chevaux": horses,
        # on garde le pronostic d'avant-course une fois figé (pour mesurer la réussite honnêtement)
        "fige": (old or {}).get("fige") or (bool(arrivee) and (old or {}).get("chevaux") and [x["n"] for x in old["chevaux"]][:5]) or None,
    }

# ----------------------------------------------------------------- stats + calibration
def update_stats():
    files = sorted(f for f in os.listdir(DATA) if re.match(r"\d{4}-\d{2}-\d{2}\.json$", f))
    tot = {"courses": 0, "gagnant": 0, "place": 0, "dans3": 0}
    by_disc = {}
    samples = []
    days = []
    for f in files:
        doc = jload(os.path.join(DATA, f), {})
        dn = {"date": doc.get("date"), "courses": 0, "gagnant": 0, "place": 0}
        for reu in doc.get("reunions", []):
            for c in reu.get("courses", []):
                arr = c.get("arrivee") or []
                ch = c.get("chevaux") or []
                if not arr or not ch:
                    continue
                order = c.get("fige") or [x["n"] for x in ch]
                top = order[0]
                g = int(arr[0] == top)
                pl = int(top in arr[:3])
                in3 = int(arr[0] in order[:3])
                tot["courses"] += 1; tot["gagnant"] += g; tot["place"] += pl; tot["dans3"] += in3
                dn["courses"] += 1; dn["gagnant"] += g; dn["place"] += pl
                b = by_disc.setdefault(c.get("discLabel") or c.get("disc"), {"courses": 0, "gagnant": 0, "place": 0})
                b["courses"] += 1; b["gagnant"] += g; b["place"] += pl
                if arr[0] in [x["n"] for x in ch]:
                    samples.append(([x["score"] for x in ch], [x["n"] for x in ch].index(arr[0])))
        if dn["courses"]:
            days.append(dn)
    jsave(os.path.join(DATA, "stats.json"), {"total": tot, "disciplines": by_disc, "jours": days[-60:]}, pretty=True)
    # calibration du parametre k (max de vraisemblance)
    model = jload(os.path.join(DATA, "model.json"), {"k": 5.0}) or {"k": 5.0}
    if len(samples) >= 150:
        best = None
        for k in [x / 2 for x in range(4, 41)]:
            ll = 0
            for sc, w in samples:
                mx = max(sc)
                z = sum(math.exp(k * (s - mx)) for s in sc)
                ll += k * (sc[w] - mx) - math.log(z)
            if best is None or ll > best[1]:
                best = (k, ll)
        model = {"k": best[0], "echantillon": len(samples), "maj": datetime.now(PARIS).strftime("%Y-%m-%d")}
        jsave(os.path.join(DATA, "model.json"), model, pretty=True)
    return model


def main():
    os.makedirs(DATA, exist_ok=True)
    model = jload(os.path.join(DATA, "model.json"), {"k": 5.0}) or {"k": 5.0}
    now = datetime.now(PARIS)
    days = [now.date()]
    if len(sys.argv) > 1:
        days = [datetime.strptime(a, "%Y-%m-%d").date() for a in sys.argv[1:]]
    elif now.hour >= 16:
        days.append(now.date() + timedelta(days=1))
    if now.hour < 9 and len(sys.argv) <= 1:
        days.insert(0, now.date() - timedelta(days=1))  # arrivees tardives de la veille
    for d in days:
        process_day(d, model)
    model = update_stats()
    files = sorted(f[:-5] for f in os.listdir(DATA) if re.match(r"\d{4}-\d{2}-\d{2}\.json$", f))
    jsave(os.path.join(DATA, "index.json"), {"dates": files[-45:], "maj": now.strftime("%Y-%m-%d %H:%M"), "k": model.get("k")}, pretty=True)
    print("appels:", STATS)


if __name__ == "__main__":
    main()
