"""
monitor_paralelo_fotmob.py
---------------------------
UN SOLO ARCHIVO, autocontenido (solo necesita 'requests'), para correr
EN PARALELO a tu sistema de produccion (Mimoexcel) sin tocarlo. No lee
ni escribe nada del repo real -- es puramente de validacion/comparacion.

QUE HACE, paso a paso:
  1. Pide a ESPN (mismo backend que ya usa Mimoexcel) los partidos EN
     VIVO de hoy.
  2. Para cada uno, busca su matchId en FotMob via el endpoint publico
     de busqueda (apigw.fotmob.com/searchapi/suggest) usando los
     nombres de los equipos -- SIN necesitar ningun slug guardado de
     antemano.
  3. Con ese matchId, pide la pagina del partido en FotMob y extrae el
     bloque __NEXT_DATA__ (JSON embebido en el HTML) -- tecnica
     confirmada offline, alternativa mas simple a la ruta _next/data
     con buildId que se probo antes (no necesita cachear ni renovar
     ningun buildId).
  4. Imprime lado a lado: goles/minuto de ESPN vs xG de FotMob.
  5. Ademas guarda cada resultado como una linea JSON en
     log_comparacion_fotmob.jsonl (se crea junto a este archivo) para
     poder revisar despues de que terminen los partidos.

LIMITACION HONESTA (documentada explicitamente, no oculta): FotMob solo
incluye 'content' (con las estadisticas/xG) en el HTML para partidos
PRE-RENDERIZADOS (populares/recientes). Para partidos menos populares,
el HTML trae un shell vacio ('fetchingLeagueData': true) y los datos
reales se piden despues via JavaScript con un token firmado que este
script NO puede replicar (necesitaria un navegador real). Cuando pasa
esto, el script lo reporta explicitamente como
'sin_datos_prerenderizados' -- nunca lo confunde con un error ni lo
rellena con datos inventados.

USO:
    pip install requests
    python monitor_paralelo_fotmob.py

    Corre UN ciclo y termina (pensado para un cron/GitHub Actions cada
    5-10 min, igual que ya haces con Mimoexcel). Si prefieres que quede
    corriendo en loop local, pasa --loop:

    python monitor_paralelo_fotmob.py --loop
"""

import re
import sys
import json
import time
import difflib
import unicodedata
from datetime import datetime, timezone

import requests

TIMEOUT = 20
LOG_FILE = "log_comparacion_fotmob.jsonl"

HEADERS_FOTMOB = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

BASE_ESPN = "https://site.api.espn.com/apis/site/v2/sports/soccer"
BASE_FOTMOB_SEARCH = "https://apigw.fotmob.com/searchapi/suggest"


# =====================================================================
# PASO 1 -- partidos en vivo desde ESPN (version minima, solo lo
# necesario para esta comparacion; el 'all/scoreboard' global de ESPN
# no cubre absolutamente todas las ligas, pero para validar la tecnica
# alcanza de sobra)
# =====================================================================

def obtener_partidos_en_vivo_espn():
    hoy = datetime.now(timezone.utc).strftime("%Y%m%d")
    url = f"{BASE_ESPN}/all/scoreboard?dates={hoy}"
    try:
        r = requests.get(url, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"[ERROR] No se pudo consultar ESPN: {e}")
        return []

    partidos = []
    for evento in data.get("events", []):
        try:
            comp = evento["competitions"][0]
            status = comp.get("status", {}).get("type", {})
            if status.get("state") != "in":
                continue  # solo partidos EN VIVO ahora mismo

            home = next(c for c in comp["competitors"] if c["homeAway"] == "home")
            away = next(c for c in comp["competitors"] if c["homeAway"] == "away")

            partidos.append({
                "espn_id": evento["id"],
                "liga": evento.get("league", {}).get("name", ""),
                "home_nombre": home["team"]["displayName"],
                "away_nombre": away["team"]["displayName"],
                "goles_home": int(home.get("score", 0) or 0),
                "goles_away": int(away.get("score", 0) or 0),
                "minuto": status.get("displayClock") or comp.get("status", {}).get("displayClock"),
            })
        except (KeyError, IndexError, StopIteration, TypeError, ValueError):
            continue

    print(f"[ESPN] {len(partidos)} partido(s) en vivo ahora mismo.")
    return partidos


# =====================================================================
# PASO 2 -- resolver matchId de FotMob buscando por nombre de equipos
# =====================================================================

def _normalizar(nombre):
    if not nombre:
        return ""
    # quita acentos ("Atletico" == "Atlético") y todo lo no alfanumerico
    sin_acentos = unicodedata.normalize("NFKD", nombre).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]", "", sin_acentos.lower())


def _fecha_opcion(payload):
    """Devuelve el matchDate como datetime UTC, o None si no se puede."""
    valor = payload.get("matchDate")
    if not valor:
        return None
    try:
        return datetime.fromisoformat(valor.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _extraer_opciones(data):
    """Normaliza las distintas formas que ha tenido la respuesta de
    searchapi/suggest a una sola lista plana de 'options'.

    Forma actual (verificada): data['matchSuggest'] es una lista de
    bloques, cada uno con su lista 'options'. Formas antiguas: una lista
    de bloques con 'suggestionName', o 'matchSuggest' como dict.
    """
    opciones = []

    def _agregar(bloque):
        if isinstance(bloque, dict):
            for opt in bloque.get("options", []) or []:
                if isinstance(opt, dict):
                    opciones.append(opt)

    if isinstance(data, dict):
        ms = data.get("matchSuggest")
        if isinstance(ms, list):
            for bloque in ms:
                _agregar(bloque)
        elif isinstance(ms, dict):
            _agregar(ms)
        if not opciones:
            for clave in ("suggestions", "data"):
                bloques = data.get(clave)
                if isinstance(bloques, list):
                    for bloque in bloques:
                        if isinstance(bloque, dict) and bloque.get("suggestionName") == "matchSuggest":
                            _agregar(bloque)
    elif isinstance(data, list):
        for bloque in data:
            if isinstance(bloque, dict) and bloque.get("suggestionName") == "matchSuggest":
                _agregar(bloque)

    return opciones


def buscar_match_id_fotmob(home_nombre, away_nombre):
    """Busca en FotMob por 'home away' y elige, entre los resultados de
    matchSuggest, el que mejor coincide por nombre de ambos equipos.
    Devuelve (match_id, home_fotmob, away_fotmob) o (None, None, None)
    si no encuentra nada razonable."""
    termino = f"{home_nombre} {away_nombre}"
    try:
        r = requests.get(
            BASE_FOTMOB_SEARCH,
            params={"term": termino, "lang": "en"},
            headers=HEADERS_FOTMOB,
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"[AVISO] Busqueda en FotMob fallo para '{termino}': {e}")
        return None, None, None

    opciones = _extraer_opciones(data)
    if not opciones:
        return None, None, None

    home_norm = _normalizar(home_nombre)
    away_norm = _normalizar(away_nombre)
    ahora = datetime.now(timezone.utc)

    mejor = None
    mejor_score = 0.0
    mejor_delta = None
    for opcion in opciones:
        payload = opcion.get("payload", opcion)
        if not isinstance(payload, dict):
            continue
        h = payload.get("homeName", "")
        a = payload.get("awayName", "")
        score_directo = (
            difflib.SequenceMatcher(None, home_norm, _normalizar(h)).ratio() +
            difflib.SequenceMatcher(None, away_norm, _normalizar(a)).ratio()
        )
        # tambien se prueba cruzado por si ESPN y FotMob listan
        # local/visitante en orden distinto para partidos en sede neutral
        score_cruzado = (
            difflib.SequenceMatcher(None, home_norm, _normalizar(a)).ratio() +
            difflib.SequenceMatcher(None, away_norm, _normalizar(h)).ratio()
        )
        score = max(score_directo, score_cruzado)

        # desempate: si dos candidatos empatan en nombre, se prefiere el
        # partido cuya fecha este mas cerca de ahora (evita quedarse con
        # un partido historico del mismo cruce)
        fecha = _fecha_opcion(payload)
        delta = abs((ahora - fecha).total_seconds()) if fecha else float("inf")

        if score > mejor_score + 1e-9 or (
            abs(score - mejor_score) <= 1e-9 and (mejor_delta is None or delta < mejor_delta)
        ):
            mejor_score = score
            mejor_delta = delta
            mejor = payload

    if mejor is None or mejor_score < 1.2:  # umbral laxo -- ambos nombres razonablemente parecidos
        return None, None, None

    return mejor.get("id"), mejor.get("homeName"), mejor.get("awayName")


# =====================================================================
# PASO 3 -- pagina del partido en FotMob, parseo de __NEXT_DATA__
# =====================================================================

def obtener_stats_fotmob(match_id):
    """Sigue la redireccion 308 de /match/{id} a la URL canonica
    /matches/{slug}/{codigo}, y extrae content.stats.Periods.All.stats
    del bloque __NEXT_DATA__ embebido en el HTML.

    Devuelve un dict con xG/posesion/tiros si el partido esta
    pre-renderizado, o {'sin_datos_prerenderizados': True} si FotMob
    difirio la carga a JavaScript (ver limitacion en el docstring del
    archivo) -- nunca inventa valores en ese caso.
    """
    url_corta = f"https://www.fotmob.com/match/{match_id}"
    try:
        r = requests.get(url_corta, headers=HEADERS_FOTMOB, timeout=TIMEOUT, allow_redirects=True)
        r.raise_for_status()
    except Exception as e:
        print(f"[AVISO] No se pudo abrir la pagina de FotMob para matchId {match_id}: {e}")
        return None

    match = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.DOTALL
    )
    if not match:
        print(f"[AVISO] No se encontro __NEXT_DATA__ en la pagina de matchId {match_id}")
        return None

    try:
        next_data = json.loads(match.group(1))
        page_props = next_data["props"]["pageProps"]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        print(f"[AVISO] __NEXT_DATA__ no tiene la forma esperada para matchId {match_id}: {e}")
        return None

    if "content" not in page_props or not page_props.get("content"):
        return {"sin_datos_prerenderizados": True}

    content = page_props["content"]
    general = page_props.get("general", {})

    stats_planas = {}
    try:
        grupos = content["stats"]["Periods"]["All"]["stats"]
        for grupo in grupos:
            for item in grupo.get("stats", []):
                titulo = item.get("title")
                valores = item.get("stats")
                if titulo and isinstance(valores, list) and len(valores) == 2:
                    stats_planas[titulo] = valores
    except (KeyError, TypeError):
        pass  # se deja stats_planas parcial/vacio en vez de fallar

    return {
        "match_name": general.get("matchName"),
        "finished": general.get("finished"),
        "stats": stats_planas,  # ej: {"Expected goals (xG)": ["1.72", "0.51"], ...}
    }


# =====================================================================
# PASO 4 -- comparar y loguear
# =====================================================================

def ciclo_comparacion():
    partidos_espn = obtener_partidos_en_vivo_espn()
    if not partidos_espn:
        print("No hay partidos en vivo en ESPN ahora mismo. Nada que comparar en este ciclo.")
        return

    resultados = []
    for p in partidos_espn:
        print(f"\n=== {p['home_nombre']} vs {p['away_nombre']} ({p['liga']}) ===")
        print(f"[ESPN] {p['goles_home']}-{p['goles_away']}  minuto {p['minuto']}")

        match_id, home_fm, away_fm = buscar_match_id_fotmob(p["home_nombre"], p["away_nombre"])
        if match_id is None:
            print("[FotMob] No se encontro un partido correspondiente en la busqueda.")
            resultados.append({**p, "fotmob_match_id": None, "fotmob_stats": None})
            continue

        print(f"[FotMob] Encontrado: matchId={match_id} ({home_fm} vs {away_fm})")
        stats = obtener_stats_fotmob(match_id)

        if stats is None:
            print("[FotMob] No se pudo obtener la pagina del partido.")
        elif stats.get("sin_datos_prerenderizados"):
            print("[FotMob] Partido SIN datos pre-renderizados (requeriria navegador con JS). "
                  "No es un error de este script -- FotMob no expone estos datos para este "
                  "partido via HTML plano.")
        else:
            xg = stats["stats"].get("Expected goals (xG)")
            posesion = stats["stats"].get("Ball possession")
            tiros = stats["stats"].get("Total shots")
            print(f"[FotMob] xG: {xg}  |  Posesion: {posesion}  |  Tiros: {tiros}")

        resultados.append({
            **p,
            "fotmob_match_id": match_id,
            "fotmob_stats": stats,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    with open(LOG_FILE, "a", encoding="utf-8") as f:
        for r in resultados:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"\n[log] {len(resultados)} resultado(s) agregado(s) a {LOG_FILE}")


def main():
    en_loop = "--loop" in sys.argv
    if not en_loop:
        ciclo_comparacion()
        return

    print("Corriendo en loop local (Ctrl+C para detener)...")
    try:
        while True:
            ciclo_comparacion()
            time.sleep(300)  # 5 min entre ciclos
    except KeyboardInterrupt:
        print("\nDetenido por el usuario.")


if __name__ == "__main__":
    main()
