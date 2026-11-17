#!/usr/bin/env python3
"""Índice de «La noche en 24h» (RTVE) para verla a tu ritmo.

- Episodios del programa con las entrevistas (fragmentos que RTVE publica aparte, situados
  por su texto dentro del episodio completo) y lo que se ve en los subtítulos: la mesa de
  análisis, los invitados en el plató, las conexiones en directo y los tramos en que se
  habla de elecciones, cada cosa con su minuto y su hora de reloj.
- Registro de las sesiones del directo de Canal 24h en YouTube: cuando YouTube reinicia el
  directo cambia de vídeo y se pierde el rebobinado; aquí queda qué vídeo cubría cada hora.

Todo va a una base SQLite (datos/noche24h.sqlite) y se exporta a docs/datos.json para la
página. No guarda transcripciones: enlaza a los subtítulos de la API oficial.

Uso:
  python noche24h.py actualizar [--dias 28] [--sin-rtve] [--sin-youtube]
  python noche24h.py exportar
"""
import argparse
from contextlib import closing
import json
import re
import sqlite3
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

MADRID = ZoneInfo("Europe/Madrid")
PROGRAMA = 25970  # «La noche en 24h» en la API de RTVE
API = "https://api.rtve.es/api"
CANAL_DIRECTOS = "https://www.youtube.com/@rtvenoticias/streams"
PATRON_24H = re.compile(r"CANAL 24H", re.I)
UA = "noche-24h/1.0"

RAIZ = Path(__file__).resolve().parent
BD = RAIZ / "datos" / "noche24h.sqlite"
SALIDA = RAIZ / "docs" / "datos.json"
DIAS_INICIALES = 28  # histórico en la primera ejecución
DIAS_HABITUALES = 14  # lo que se repasa en cada ejecución
REINTENTO = timedelta(days=3)  # cuánto se reintenta lo que aún no tiene subtítulos o episodio
VENTANA_FRAGMENTO = (timedelta(hours=30), timedelta(hours=1))  # episodio entre pub-30h y pub+1h

ESQUEMA = """
CREATE TABLE IF NOT EXISTS episodios(
  id INTEGER PRIMARY KEY, titulo TEXT, inicio TEXT, duracion_s INTEGER,
  url TEXT, subtitulos TEXT, estado TEXT, procesado TEXT);
CREATE TABLE IF NOT EXISTS marcas(
  episodio_id INTEGER, seg INTEGER, tipo TEXT, texto TEXT,
  PRIMARY KEY(episodio_id, tipo, seg));
CREATE TABLE IF NOT EXISTS fragmentos(
  id INTEGER PRIMARY KEY, titulo TEXT, publicado TEXT, duracion_s INTEGER, url TEXT,
  episodio_id INTEGER, seg INTEGER, coincidencias INTEGER, estado TEXT);
CREATE TABLE IF NOT EXISTS directos(
  video_id TEXT PRIMARY KEY, titulo TEXT, inicio TEXT, primera_vez TEXT, fin_detectado TEXT);
CREATE TABLE IF NOT EXISTS estado(clave TEXT PRIMARY KEY, valor TEXT, desde TEXT);
CREATE TABLE IF NOT EXISTS tramos(
  episodio_id INTEGER, tema TEXT, seg INTEGER, seg_fin INTEGER,
  PRIMARY KEY(episodio_id, tema, seg));
"""
# Sube cuando cambia cómo se sacan marcas o tramos: se rehacen los de todos los episodios.
# Se guarda en PRAGMA user_version de la base.
VERSION_MARCAS = 2
CERCA = 90  # segundos: una presentación tan cerca de un fragmento es la de esa entrevista


# --- utilidades -----------------------------------------------------------------------

def iso(dt):
    return dt.isoformat(timespec="seconds")


def fecha_rtve(texto):
    """«12-11-2026 21:45:00» (hora de Madrid) → datetime con zona."""
    return datetime.strptime(texto, "%d-%m-%Y %H:%M:%S").replace(tzinfo=MADRID)


def fecha_item(item):
    return fecha_rtve(item.get("dateOfEmission") or item["publicationDate"])


def tipo_item(item):
    return (item.get("type") or {}).get("name")


def descargar(url, intentos=3, espera=2):
    ultimo = None
    for n in range(intentos):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code < 500:
                raise
            e.close()
            ultimo = e
        except (urllib.error.URLError, TimeoutError) as e:
            ultimo = e
        time.sleep(espera * (n + 1))
    raise ultimo


def json_url(url):
    return json.loads(descargar(url))


def abrir(ruta):
    ruta = Path(ruta)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(ruta)
    conn.executescript(ESQUEMA)
    return conn


# --- subtítulos ------------------------------------------------------------------------

TIEMPO = re.compile(r"(?:(\d+):)?(\d{2}):(\d{2})[.,]\d+\s*-->")


def leer_vtt(texto):
    """VTT → [(segundo de inicio, texto)]."""
    cues = []
    for bloque in re.split(r"\n\s*\n", texto.replace("\r", "")):
        lineas = bloque.strip().split("\n")
        for n, linea in enumerate(lineas):
            m = TIEMPO.search(linea)
            if not m:
                continue
            seg = int(m[1] or 0) * 3600 + int(m[2]) * 60 + int(m[3])
            txt = " ".join(l.strip() for l in lineas[n + 1:] if l.strip())
            txt = re.sub(r"<[^>]+>", "", txt).strip()
            if txt:
                cues.append((seg, txt))
            break
    return cues


def url_subtitulos(video_id):
    return f"{API}/videos/{video_id}/subtitulos.json"


def subtitulos_es(video_id, obtener=json_url, bajar=descargar):
    """Cues del VTT en castellano, o None si RTVE aún no los tiene."""
    try:
        items = obtener(url_subtitulos(video_id))["page"]["items"]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            e.close()
            return None
        raise
    src = next((s["src"] for s in items if s.get("lang") == "es"), None)
    if not src:
        return None
    cues = leer_vtt(bajar(src).decode("utf-8", "replace"))
    return cues or None


def normalizar(texto):
    return re.sub(r"\W+", " ", texto.lower()).strip()


def alinear(fragmento, episodio, minimo=10, proporcion=0.2):
    """Dónde empieza el fragmento dentro del episodio, comparando líneas de subtítulos.

    Devuelve (segundo, líneas que casan) o None si no casan bastantes.
    """
    indice = {}
    for seg, txt in episodio:
        clave = normalizar(txt)
        if len(clave) > 25:
            indice.setdefault(clave, []).append(seg)
    desfases, casan = [], 0
    for seg, txt in fragmento:
        clave = normalizar(txt)
        if len(clave) > 25 and clave in indice:
            casan += 1
            desfases.extend(s - seg for s in indice[clave])
    if casan < max(minimo, proporcion * len(fragmento)):
        return None
    return max(0, int(statistics.median_low(desfases))), casan


NOMBRE = (r"[A-ZÁÉÍÓÚÑ][a-záéíóúñü]+"
          r"(?:\s+(?:de\s+(?:la\s+)?|del\s+)?[A-ZÁÉÍÓÚÑ][a-záéíóúñü]+){1,3}")
# Mesa de análisis: al empezar («Comenzamos. Hoy nos acompañan en la mesa…») y al renovarla.
MESA = re.compile(r"(?i:\b(?:nos acompañan|se incorporan?|renovamos)\b[^.?]{0,40}\bmesa\b"
                  r"|\bmesa\b[^.?]{0,25}\b(?:nos acompañan|se incorporan?)\b)")
APERTURA = re.compile(r"(?i:^(?:comenzamos[.,]?\s*)?(?:hoy\s+)?nos acompañan\b)")
# Invitado en el plató: «nos acompaña X, cargo», «X, bienvenido».
PLATO = re.compile(rf"(?i:\bnos acompaña\b)(?!n)|(?i:\bnos visita\b)|{NOMBRE},?\s+(?i:bienvenid[oa])")
# Conexión en directo: redacción, reporteros, corresponsales y entrevistas a distancia.
CONEXION = re.compile(
    r"(?i:redacción de informativos|\bvolvemos en directo\b|\bconectamos con\b"
    r"|\bnuestr[ao] (?:corresponsal|enviad[ao] especial)\b)"
    r"|(?i:\b(?:volvemos|vamos|nos vamos)\b(?: en directo)? hasta) [A-ZÁÉÍÓÚ]"
    r"|(?i:buenas noches) (?:en|desde) [A-ZÁÉÍÓÚ]"
    r"|(?i:\bcuént(?:anos|enos|anosla)\b)(?=.*(?i:buenas noches))"
    r"|(?i:buenas noches)(?=.*(?i:\bcuént(?:anos|enos)\b))")
SALUDO = [re.compile(rf"{NOMBRE},?\s+(?i:(?:muy\s+)?buenas\s+noches)"),
          re.compile(rf"(?i:buenas\s+noches),\s+{NOMBRE}")]
ANUNCIO = re.compile(r"(?i:\bnos acompañar[áa]n?\b)")
RUIDO = re.compile(r"(?i:miembros de la mesa|turno de preguntas)")
DESPEDIDA = re.compile(r"(?i:acabamos|despedir|hasta mañana|volvemos mañana|nos vemos mañana)")
CONECTORES = {"de", "del", "la"}
# Palabras con mayúscula que no son nombre: las de relleno se recortan por los bordes y las
# de instituciones descartan el candidato entero («Consejo General del Poder Judicial»).
RELLENO = set("""Buenas Muy Hola Comenzamos Hoy Nos Mesa Análisis Gracias Bueno Pues Sí Y
Bienvenido Bienvenida Empiezo Empieza El La Los Las Un Una Este Esta Esto Ese Esa Entre Para Con
Por En Desde Ahí Usted Yo Te Le Lo Pero Ahora Luego Bien""".split())
INSTITUCION = set("""Partido Popular Socialista Gobierno España Congreso Senado Redacción
Informativos Junta Electoral Comunidad Presidente Ministro Ministerio Estados Unidos Unión
Europea Canal Horas Radio Televisión Española Universidad Departamento Consejo General Poder
Judicial Fundación Generalitat Ciencia Política Internacional Oriente Próximo""".split())
ELECCIONES = re.compile(
    r"(?i:\b(?:elecciones|electoral(?:es)?|campaña|29 de noviembre|29N|candidat[oa]s?"
    r"|cabeza de lista|número uno|mitin(?:es)?|mítines|encuestas?|sondeos?|CIS|cara a cara"
    r"|escaños?|mayoría absoluta|urnas?|votantes?|papeletas?)\b)")


def nombres(texto):
    """Nombres propios de un texto, en orden y sin repetir. Se descartan los que llevan
    palabras de saludo o de instituciones («Consejo General del Poder Judicial»)."""
    vistos = []
    for m in re.finditer(NOMBRE, texto):
        palabras = m.group(0).split()
        if any(p in INSTITUCION for p in palabras):
            continue
        while palabras and (palabras[0] in RELLENO or palabras[0] in CONECTORES):
            palabras.pop(0)
        while palabras and (palabras[-1] in RELLENO or palabras[-1] in CONECTORES):
            palabras.pop()
        nombre = " ".join(palabras)
        if (sum(p not in CONECTORES for p in palabras) >= 2 and nombre not in vistos
                and not any(p in RELLENO for p in palabras)):
            vistos.append(nombre)
    return vistos


def presentaciones(cues, duracion=None, separacion=60, largo=160, ventana_mesa=35,
                   inicio=600, final=420):
    """Momentos en que se presenta a alguien: [(segundo, tipo, texto)].

    Tipos: «mesa» (los tertulianos, con sus nombres si se reconocen), «plato» (invitado en
    el estudio), «conexion» (redacción, reporteros, corresponsales, entrevistas a distancia),
    «anuncio» (al empezar, quién vendrá luego) y «presentacion» (un saludo sin más pistas).
    Se descartan las preguntas «a los miembros de la mesa» y las despedidas del final.
    Aproximado: sale de subtítulos automáticos y los nombres pueden venir mal escritos."""
    if duracion is None:
        duracion = cues[-1][0] if cues else 0
    marcas = []
    for n, (seg, txt) in enumerate(cues):
        # Un anuncio no tapa lo que venga detrás («nos acompañará…» y enseguida la mesa).
        ultima = next((s for s, t, _ in reversed(marcas) if t != "anuncio"), None)
        if ultima is not None and seg - ultima < separacion:
            continue
        siguiente = cues[n + 1][1] if n + 1 < len(cues) else ""
        frase = f"{txt} {siguiente}".strip()
        if RUIDO.search(frase):
            continue
        if MESA.search(frase) or (seg < inicio and APERTURA.search(txt)):
            tipo = "mesa"
        elif ANUNCIO.search(txt):
            if seg >= inicio:
                continue
            tipo = "anuncio"
        elif CONEXION.search(frase):
            tipo = "conexion"
        elif PLATO.search(txt):
            tipo = "plato"
        elif any(p.search(txt) for p in SALUDO):
            tipo = "presentacion"
        else:
            continue
        if seg > duracion - final and DESPEDIDA.search(" ".join(t for _, t in cues[n:n + 4])):
            continue
        texto = frase
        if tipo == "mesa":
            bloque = " ".join(t for s, t in cues[n:n + 40] if s - seg <= ventana_mesa)
            texto = " · ".join(nombres(bloque)) or frase
        if len(texto) > largo:
            texto = texto[:largo].rsplit(" ", 1)[0] + "…"
        marcas.append((seg, tipo, texto))
    return marcas


def tramos(cues, patron=ELECCIONES, tam=60, minimo=2, largo_min=2, hueco=3):
    """Tramos en que se habla de un tema: [(segundo inicial, segundo final)].

    Cuenta las palabras clave de cada minuto. Entra el minuto con `minimo` o más; uno con
    alguna mención no corta el tramo si el siguiente vuelve a entrar. Solo cuentan los de
    `largo_min` minutos o más, y dos separados por `hueco` minutos o menos son uno (los
    minutos sueltos se descartan antes de unir: si no, menciones aisladas encadenan tramos)."""
    if not cues:
        return []
    cubos = [0] * (cues[-1][0] // tam + 1)
    for seg, txt in cues:
        cubos[seg // tam] += len(patron.findall(txt))
    dentro = []
    for i, c in enumerate(cubos):
        sigue = (c > 0 and i and dentro[-1] and i + 1 < len(cubos) and cubos[i + 1] >= minimo)
        dentro.append(c >= minimo or bool(sigue))
    rachas, ini = [], None
    for i, d in enumerate(dentro + [False]):
        if d and ini is None:
            ini = i
        elif not d and ini is not None:
            if i - ini >= largo_min:
                if rachas and ini - rachas[-1][1] <= hueco:
                    rachas[-1][1] = i
                else:
                    rachas.append([ini, i])
            ini = None
    return [(a * tam, min(b * tam, cues[-1][0])) for a, b in rachas]


# --- RTVE ------------------------------------------------------------------------------

def listar_videos(hasta, obtener=json_url, tam=60, max_paginas=20):
    """Vídeos del programa (completos y fragmentos) emitidos desde `hasta`, más recientes primero."""
    items = []
    for pagina in range(1, max_paginas + 1):
        datos = obtener(f"{API}/programas/{PROGRAMA}/videos.json?size={tam}&page={pagina}")["page"]
        lote = datos.get("items") or []
        items.extend(lote)
        if not lote or fecha_item(lote[-1]) < hasta or pagina >= (datos.get("totalPages") or 1):
            break
    return [i for i in items if fecha_item(i) >= hasta]


def guardar_marcas(conn, episodio_id, cues, duracion):
    """Rehace las marcas y los tramos de un episodio a partir de sus subtítulos."""
    conn.execute("DELETE FROM marcas WHERE episodio_id=?", (episodio_id,))
    conn.execute("DELETE FROM tramos WHERE episodio_id=?", (episodio_id,))
    conn.executemany(
        "INSERT OR IGNORE INTO marcas VALUES(?,?,?,?)",
        [(episodio_id, seg, tipo, txt) for seg, tipo, txt in presentaciones(cues, duracion)])
    conn.executemany(
        "INSERT INTO tramos VALUES(?,?,?,?)",
        [(episodio_id, "elecciones", ini, fin) for ini, fin in tramos(cues)])


def paso_rtve(conn, momento, dias=DIAS_INICIALES, obtener=json_url, bajar=descargar):
    """Añade episodios y fragmentos nuevos. Devuelve cuántas filas ha escrito."""
    hay = conn.execute("SELECT count(*) FROM episodios").fetchone()[0]
    items = listar_videos(momento - timedelta(days=dias if not hay else DIAS_HABITUALES), obtener)
    cache = {}

    def cues_de(video_id):
        if video_id not in cache:
            cache[video_id] = subtitulos_es(video_id, obtener, bajar)
        return cache[video_id]

    escritas = 0
    if conn.execute("PRAGMA user_version").fetchone()[0] < VERSION_MARCAS:
        # Si RTVE ya no da los subtítulos de un episodio, se quedan sus marcas de antes.
        for id_, dur in conn.execute(
                "SELECT id, duracion_s FROM episodios WHERE estado='ok'").fetchall():
            cues = cues_de(id_)
            if cues:
                guardar_marcas(conn, id_, cues, dur)
                escritas += 1
        conn.execute(f"PRAGMA user_version={VERSION_MARCAS}")
        conn.commit()

    for item in (i for i in items if tipo_item(i) == "Completo"):
        fila = conn.execute("SELECT estado FROM episodios WHERE id=?", (item["id"],)).fetchone()
        if fila and (fila[0] == "ok" or fecha_item(item) < momento - REINTENTO):
            continue
        cues = cues_de(item["id"])
        estado = "ok" if cues else "sin_subtitulos"
        if fila and fila[0] == estado:
            continue
        duracion = (item.get("duration") or 0) // 1000
        conn.execute(
            "INSERT OR REPLACE INTO episodios VALUES(?,?,?,?,?,?,?,?)",
            (item["id"], item.get("title"), iso(fecha_item(item)), duracion,
             item.get("htmlUrl"), url_subtitulos(item["id"]), estado, iso(momento)))
        guardar_marcas(conn, item["id"], cues or [], duracion)
        conn.commit()
        escritas += 1

    episodios = [(i, datetime.fromisoformat(ini)) for i, ini in
                 conn.execute("SELECT id, inicio FROM episodios WHERE estado='ok'")]
    antes, despues = VENTANA_FRAGMENTO
    for item in (i for i in items if tipo_item(i) == "Fragmento"):
        if conn.execute("SELECT 1 FROM fragmentos WHERE id=?", (item["id"],)).fetchone():
            continue
        publicado = fecha_item(item)
        viejo = publicado < momento - REINTENTO
        candidatos = [i for i, ini in episodios if publicado - antes <= ini <= publicado + despues]
        mejor, estado = None, "sin_episodio"
        if candidatos:
            cues = cues_de(item["id"])
            estado = "sin_subtitulos"
            if cues:
                estado = "sin_casar"
                for ep in candidatos:
                    r = alinear(cues, cues_de(ep) or [])
                    if r and (mejor is None or r[1] > mejor[2]):
                        mejor = (ep, r[0], r[1])
        if mejor is None and not viejo:
            continue  # se reintenta en la próxima ejecución
        ep, seg, casan = mejor or (None, None, None)
        conn.execute(
            "INSERT INTO fragmentos VALUES(?,?,?,?,?,?,?,?,?)",
            (item["id"], item.get("title"), iso(publicado), (item.get("duration") or 0) // 1000,
             item.get("htmlUrl"), ep, seg, casan, "ok" if mejor else estado))
        conn.commit()
        escritas += 1
    return escritas


# --- YouTube ---------------------------------------------------------------------------

def listar_directos_24h(ejecutar=subprocess.run):
    """Directos en emisión de Canal 24h en el canal de RTVE Noticias: [(video_id, título)]."""
    r = ejecutar(["yt-dlp", "--no-warnings", "--flat-playlist", "-J", "--playlist-end", "15",
                  CANAL_DIRECTOS], capture_output=True, text=True, timeout=180)
    if r.returncode:
        raise RuntimeError(f"yt-dlp: {r.stderr.strip()[-200:]}")
    entradas = json.loads(r.stdout).get("entries") or []
    # En la lista, los directos en emisión son los que no tienen duración.
    return [(e["id"], e.get("title") or "") for e in entradas
            if PATRON_24H.search(e.get("title") or "") and e.get("duration") is None]


def inicio_directo(video_id, ejecutar=subprocess.run):
    """Hora de inicio del directo según YouTube, o None si no se puede saber."""
    try:
        r = ejecutar(["yt-dlp", "--no-warnings", "-j",
                      f"https://www.youtube.com/watch?v={video_id}"],
                     capture_output=True, text=True, timeout=180)
        ts = json.loads(r.stdout).get("release_timestamp") if r.returncode == 0 else None
    except (ValueError, subprocess.TimeoutExpired):
        ts = None
    return iso(datetime.fromtimestamp(ts, MADRID)) if ts else None


def paso_youtube(conn, momento, vivos, obtener_inicio=inicio_directo):
    """Registra las sesiones del directo. Una sesión se da por cortada cuando aparece otra;
    si la lista viene vacía no se cierra nada (puede ser un fallo puntual)."""
    escritas = 0
    for vid, titulo in vivos:
        fila = conn.execute("SELECT inicio, fin_detectado FROM directos WHERE video_id=?",
                            (vid,)).fetchone()
        if fila is None:
            conn.execute("INSERT INTO directos VALUES(?,?,?,?,NULL)",
                         (vid, titulo, obtener_inicio(vid), iso(momento)))
            escritas += 1
        elif fila[1] is not None:
            conn.execute("UPDATE directos SET fin_detectado=NULL WHERE video_id=?", (vid,))
            escritas += 1
        elif fila[0] is None:
            inicio = obtener_inicio(vid)
            if inicio:
                conn.execute("UPDATE directos SET inicio=? WHERE video_id=?", (inicio, vid))
                escritas += 1
    if vivos:
        ids = {v for v, _ in vivos}
        inicios = [datetime.fromisoformat(i) for (i,) in conn.execute(
            f"SELECT inicio FROM directos WHERE inicio IS NOT NULL AND video_id IN "
            f"({','.join('?' * len(ids))})", tuple(ids))]
        abiertos = conn.execute(
            "SELECT video_id, inicio FROM directos WHERE fin_detectado IS NULL").fetchall()
        for vid, inicio in abiertos:
            if vid in ids:
                continue
            # El corte fue, como tarde, cuando empezó la sesión nueva; si no, al detectarlo.
            posteriores = [i for i in inicios if inicio is None or i > datetime.fromisoformat(inicio)]
            fin = min(posteriores + [momento])
            conn.execute("UPDATE directos SET fin_detectado=? WHERE video_id=?", (iso(fin), vid))
            escritas += 1
    conn.commit()
    return escritas


# --- estado y exportación --------------------------------------------------------------

def anotar(conn, paso, error, momento):
    """Guarda el estado del paso solo si cambia. True si es un error nuevo (antes iba bien)."""
    valor = "ok" if error is None else f"error: {error}"[:300]
    previo = conn.execute("SELECT valor FROM estado WHERE clave=?", (paso,)).fetchone()
    if previo and previo[0] == valor:
        return False
    conn.execute("INSERT OR REPLACE INTO estado VALUES(?,?,?)", (paso, valor, iso(momento)))
    conn.commit()
    return error is not None and (previo is None or previo[0] == "ok")


def datos_pagina(conn):
    episodios = []
    for id_, titulo, inicio, dur, url, subs, estado in conn.execute(
            "SELECT id, titulo, inicio, duracion_s, url, subtitulos, estado FROM episodios "
            "ORDER BY inicio DESC"):
        marcas = [{"seg": s, "tipo": t, "texto": x} for s, t, x in conn.execute(
            "SELECT seg, tipo, texto FROM marcas WHERE episodio_id=?", (id_,))]
        for s, d, x, u in conn.execute(
                "SELECT seg, duracion_s, titulo, url FROM fragmentos "
                "WHERE episodio_id=? AND estado='ok' ORDER BY seg", (id_,)):
            entrevista = {"seg": s, "seg_fin": s + d, "tipo": "entrevista", "texto": x, "url": u}
            # La presentación del invitado y el fragmento son el mismo momento: una sola línea.
            cerca = [m for m in marcas if m["tipo"] in ("plato", "presentacion")
                     and abs(m["seg"] - s) <= CERCA]
            if cerca:
                m = min(cerca, key=lambda m: abs(m["seg"] - s))
                marcas.remove(m)
                entrevista["presenta"] = m["texto"]
                if m["tipo"] == "plato":
                    entrevista["lugar"] = "plato"
            marcas.append(entrevista)
        marcas.sort(key=lambda m: (m["seg"], m["tipo"]))
        tramos_ep = [{"tema": t, "seg": s, "seg_fin": f} for t, s, f in conn.execute(
            "SELECT tema, seg, seg_fin FROM tramos WHERE episodio_id=? ORDER BY seg", (id_,))]
        episodios.append({"id": id_, "titulo": titulo, "inicio": inicio, "duracion_s": dur,
                          "url": url, "subtitulos": subs, "estado": estado, "marcas": marcas,
                          "tramos": tramos_ep})
    directos = [dict(zip(("video_id", "titulo", "inicio", "primera_vez", "fin_detectado"), f))
                for f in conn.execute("SELECT * FROM directos ORDER BY primera_vez DESC")]
    estado = {c: {"valor": v, "desde": d} for c, v, d in conn.execute("SELECT * FROM estado")}
    return {"estado": estado, "directos": directos, "episodios": episodios}


def exportar(conn, destino, momento):
    """Escribe el JSON de la página solo si cambian los datos. True si lo ha escrito."""
    destino = Path(destino)
    datos = datos_pagina(conn)
    if destino.exists():
        previo = json.loads(destino.read_text(encoding="utf-8"))
        previo.pop("generado", None)
        if previo == datos:
            return False
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(json.dumps({"generado": iso(momento), **datos}, ensure_ascii=False,
                                  indent=1) + "\n", encoding="utf-8")
    return True


# --- línea de órdenes ------------------------------------------------------------------

def actualizar(args):
    momento = datetime.now(MADRID)
    with closing(abrir(args.bd)) as conn:
        return _actualizar(conn, args, momento)


def _actualizar(conn, args, momento):
    errores_nuevos = []
    pasos = []
    if not args.sin_rtve:
        pasos.append(("rtve", lambda: paso_rtve(conn, momento, args.dias)))
    if not args.sin_youtube:
        def youtube():
            vivos = listar_directos_24h()
            escritas = paso_youtube(conn, momento, vivos)
            if not vivos:
                raise RuntimeError("no aparece el directo de Canal 24H en la lista del canal")
            return escritas
        pasos.append(("youtube", youtube))
    for nombre, paso in pasos:
        error = None
        try:
            print(f"{nombre}: {paso()} filas escritas")
        except Exception as e:  # noqa: BLE001 — se anota y sigue con el otro paso
            error = f"{type(e).__name__}: {e}"
            print(f"{nombre}: {error}", file=sys.stderr)
        if anotar(conn, nombre, error, momento):
            errores_nuevos.append(nombre)
    print("datos.json", "actualizado" if exportar(conn, args.salida, momento) else "sin cambios")
    return 1 if errores_nuevos else 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--bd", default=BD, help="base SQLite")
    p.add_argument("--salida", default=SALIDA, help="JSON para la página")
    sub = p.add_subparsers(dest="orden", required=True)
    a = sub.add_parser("actualizar", help="trae episodios, fragmentos y directos nuevos")
    a.add_argument("--dias", type=int, default=DIAS_INICIALES,
                   help="histórico si la base está vacía (por defecto 28)")
    a.add_argument("--sin-rtve", action="store_true")
    a.add_argument("--sin-youtube", action="store_true")
    sub.add_parser("exportar", help="regenera el JSON de la página")
    args = p.parse_args(argv)
    if args.orden == "actualizar":
        return actualizar(args)
    with closing(abrir(args.bd)) as conn:
        exportar(conn, args.salida, datetime.now(MADRID))
    return 0


if __name__ == "__main__":
    sys.exit(main())
