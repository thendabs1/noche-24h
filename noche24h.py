#!/usr/bin/env python3
"""Índice de «La noche en 24h» (RTVE) para verla a tu ritmo.

- Episodios del programa con las entrevistas (fragmentos que RTVE publica aparte, situados
  por su texto dentro del episodio completo) y lo que se ve en los subtítulos: la mesa de
  análisis, los invitados en el plató y las conexiones en directo, cada cosa con su minuto y
  su hora de reloj.
- Registro de las sesiones del directo de Canal 24h en YouTube: cuando YouTube reinicia el
  directo cambia de vídeo y se pierde el rebobinado; aquí queda qué vídeo cubría cada hora.

Todo va a una base SQLite (datos/noche24h.sqlite) y se exporta a docs/datos.json para la
página. No guarda transcripciones: enlaza a los subtítulos de la API oficial.

Uso:
  python noche24h.py actualizar [--dias 28] [--sin-rtve] [--sin-youtube]
  python noche24h.py exportar
"""
import argparse
from collections import Counter
from contextlib import closing
import hashlib
import json
import re
import sqlite3
import statistics
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from difflib import SequenceMatcher
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
# Tabla curada de tertulianos (identidad, fuentes y variantes de los subtítulos) y decisiones
# humanas sobre apariciones concretas. Las dos se editan a mano y se revisan en los PR.
ANALISTAS = RAIZ / "analistas.json"
REVISIONES = RAIZ / "revisiones.json"
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
DROP TABLE IF EXISTS tramos;
CREATE TABLE IF NOT EXISTS vocabulario(
  episodio_id INTEGER, forma TEXT, veces INTEGER, PRIMARY KEY(episodio_id, forma));
DROP TABLE IF EXISTS meta;
CREATE TABLE IF NOT EXISTS recuentos(episodio_id INTEGER PRIMARY KEY, firma TEXT);
"""
# Sube cuando cambia cómo se sacan las marcas: se rehacen las de todos los episodios.
# Se guarda en PRAGMA user_version de la base.
VERSION_MARCAS = 4
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
# Sin «nos» y en singular también («acompañan en la mesa», «nos acompaña en la mesa»); los
# subtítulos meten «..» a mitad de frase.
MESA = re.compile(r"(?i:\b(?:acompañan?|se incorporan?|renovamos)\b(?:[^.?]|\.\.+){0,40}\bmesa\b"
                  r"|\bmesa\b[^.?]{0,25}\b(?:nos acompañan?|se incorporan?)\b)")
# Entradas que también pueden ser de un invitado: valen si detrás vienen dos o más nombres.
ENTRADA_SUAVE = re.compile(r"(?i:\bnos acompaña\b|\bcomentar(?:lo)?(?: esta noche)? con\b"
                           r"|\bmesa de análisis\b)")
# Al presentar la mesa cada nombre va con su saludo; la primera pregunta cierra la ronda.
SALUDO_MESA = re.compile(r"(?i:\bbuenas\b|bienvenid[oa]\b|qué tal|\bhola\b|un placer|buena nit)")
FIN_MESA = re.compile(r"(?i:\bempie(?:zo|za|zamos)\b|\bcomienzo por\b|\b(?:os|te) pregunt|\bos quer[ií]a\b"
                      r"|\bpor ti\b|\bprimera (?:valoración|ronda)\b|\bdecíamos\b)")
# «Chema CRESPO», «Ana MARTINEZ HOYOS»: apellidos en mayúsculas detrás de un nombre.
MAYUSCULAS = re.compile(r"\b[A-ZÁÉÍÓÚÑ][a-záéíóúñü]+(?:\s+[A-ZÁÉÍÓÚÑ]{3,}\b)+")
APERTURA = re.compile(r"(?i:^(?:comenzamos[.,]?\s*)?(?:hoy\s+)?nos acompañan\b)")
# Invitado en el plató: «nos acompaña X, cargo», «X, bienvenido».
PLATO = re.compile(rf"(?i:\bnos acompaña\b)(?!n)|(?i:\bnos visita\b)|{NOMBRE},?\s+(?i:bienvenid[oa])")
# Lo que va entre «nos acompaña» y el invitado: «hoy nos acompaña en el plató de la noche X».
RELLENO_PLATO = re.compile(
    r"(?i:^(?:\s*(?:ahora|hoy|ya|aquí|también|a continuación|esta noche|en la noche"
    r"|en (?:el )?(?:plat[oó]|estudio)(?: de la noche)?)\b[\s,]*)*)")
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
# Lo que va delante del nombre y se recorta: «del Supremo José Antonio Martín».
CARGO = set("Supremo Tribunal Constitucional Magistrado Magistrada General Comandante".split())
# Palabras con mayúscula que no son nombre: las de relleno se recortan por los bordes y las
# de instituciones descartan el candidato entero («Consejo General del Poder Judicial»).
RELLENO = set("""Buenas Muy Hola Comenzamos Hoy Nos Mesa Análisis Gracias Bueno Pues Sí Y
Bienvenido Bienvenida Empiezo Empieza El La Los Las Un Una Este Esta Esto Ese Esa Entre Para Con
Por En Desde Ahí Usted Yo Te Le Lo Pero Ahora Luego Bien
Xavi Xabier Fortes Gabón Sabín Sabi""".split())  # el presentador, como lo escriben los subtítulos
INSTITUCION = set("""Partido Popular Socialista Gobierno España Congreso Senado Redacción
Informativos Junta Electoral Comunidad Presidente Ministro Ministerio Estados Unidos Unión
Europea Canal Horas Radio Televisión Española Universidad Departamento Consejo General Poder
Judicial Fundación Generalitat Ayuntamiento York Ciencia Política Internacional Oriente Próximo""".split())


def sin_mayusculas(texto):
    """«Chema CRESPO» → «Chema Crespo» (no toca siglas sueltas como «PSOE»)."""
    return MAYUSCULAS.sub(lambda m: re.sub(r"\b([A-ZÁÉÍÓÚÑ])([A-ZÁÉÍÓÚÑ]{2,})\b",
                                           lambda p: p[1] + p[2].lower(), m[0]), texto)


def nombres(texto):
    """Nombres propios de un texto, en orden y sin repetir. Se descartan los que llevan
    palabras de saludo o de instituciones («Consejo General del Poder Judicial»)."""
    vistos = []
    for nombre, _ in nombres_con_posicion(texto):
        if nombre not in vistos:
            vistos.append(nombre)
    return vistos


def nombres_con_posicion(texto):
    """Como nombres(), con repetidos y el carácter en que empieza cada uno."""
    vistos = []
    for m in re.finditer(NOMBRE, texto):
        palabras = m.group(0).split()
        if any(p in INSTITUCION for p in palabras):
            continue
        while palabras and (palabras[0] in RELLENO or palabras[0] in CONECTORES
                            or palabras[0] in CARGO):
            palabras.pop(0)
        while palabras and (palabras[-1] in RELLENO or palabras[-1] in CONECTORES):
            palabras.pop()
        nombre = " ".join(palabras)
        if (sum(p not in CONECTORES for p in palabras) >= 2
                and not any(p in RELLENO for p in palabras)):
            vistos.append((nombre, m.start() + m[0].index(palabras[0])))
    return vistos


def saludada(cues, i, cerca=4):
    """¿Hay un saludo («buenas noches», «bienvenido»…) a `cerca` segundos o menos de la línea i?"""
    return any(abs(s - cues[i][0]) <= cerca and SALUDO_MESA.search(t)
               for s, t in cues[max(0, i - 5):i + 6])


def tertulianos(cues, n, entrada=None, ventana=40, cerca=4):
    """Nombres de la mesa que se presenta a partir de la línea n.

    Cuentan los que van con un saludo a `cerca` segundos o menos («Paloma Esteban, buenas
    noches»), los que cierran la lista («Y Marta Nebot») y, si se da `entrada` (el match de
    la frase de entrada sobre esta línea y la siguiente), todos los de su primera frase:
    «se incorporan Gerardo Lorenzo, Carmelo Encinas, Juan Romero y Estefanía Molina.».
    La ronda acaba en la primera pregunta («Empiezo por ti») o a los `ventana` segundos:
    así no entran los nombres de los que se habla después."""
    desde = entrada.start() if entrada else 0
    if desde > len(cues[n][1]):  # la entrada empieza en la línea siguiente
        n, desde = n + 1, desde - len(cues[n][1]) - 1
    texto, lineas = "", []  # lineas: (carácter inicial en texto, índice en cues)
    for i in range(n, len(cues)):
        seg, txt = cues[i]
        if seg - cues[n][0] > ventana:
            break
        txt = txt[desde:] if i == n else txt
        fin = FIN_MESA.search(txt)
        lineas.append((len(texto), i))
        texto += (txt[:fin.start()] if fin else txt) + " "
        if fin:
            break
    texto = sin_mayusculas(texto)

    candidatos = nombres_con_posicion(texto)
    # Fin de la primera frase con nombres: el primer punto (no «..») detrás de la entrada y
    # del primer nombre («…se incorporan ya a la mesa. Ruth Ferrero, doctora…, y Vicente…»).
    primera = len(texto)
    if entrada:
        largo = entrada.end() - entrada.start()
        desde_nombre = next((pos for _, pos in candidatos if pos >= largo), largo)
        if desde_nombre - largo > 60 or any(pos < largo for _, pos in candidatos):
            desde_nombre = largo
        corte = re.compile(r"(?<!\.)[.?!](?!\.)").search(texto, desde_nombre)
        primera = corte.start() if corte else len(texto)
    vistos = []
    for nombre, pos in candidatos:
        linea = max(i for ini, i in lineas if ini <= pos)
        if nombre not in vistos and (
                (entrada and pos < primera) or saludada(cues, linea, cerca)
                or re.search(r"(?i:\by)\s+$", texto[max(0, pos - 4):pos])):
            vistos.append(nombre)
    return vistos


def invitado(frase):
    """El invitado de una presentación en plató, sin lo que la rodea:
    «humano. Y hoy nos acompaña Félix Bolaños, ministro de Presidencia. Buenas noches»
    → «Félix Bolaños, ministro de Presidencia». Si detrás no queda nada («Pablo Simón nos
    acompaña ya»), el nombre de delante. Si no se reconoce, la frase tal cual."""
    m = PLATO.search(frase)
    if not m:
        return frase
    if m.group(0).lower().startswith("nos "):
        resto = RELLENO_PLATO.sub("", frase[m.end():].lstrip(" .,:"))
        texto = re.split(r"[.?!¿]+(?:\s|$)|(?i:\b(?:qué tal|hola|(?:muy )?buenas noches|bienvenid[oa])\b)",
                         resto, maxsplit=1)[0].rstrip(" ,;:¿")
        if not texto:
            texto = (nombres(frase[:m.start()]) or [""])[-1]
    else:  # «X, bienvenido»
        texto = re.match(NOMBRE, m.group(0)).group(0)
    return texto[:1].upper() + texto[1:] if texto else frase


def presentaciones(cues, duracion=None, separacion=60, largo=160, inicio=600, final=420):
    """Momentos en que se presenta a alguien: [(segundo, tipo, texto)].

    Tipos: «mesa» (los tertulianos, con sus nombres si se reconocen), «plato» (invitado en
    el estudio), «conexion» (redacción, reporteros, corresponsales, entrevistas a distancia),
    «anuncio» (al empezar, quién vendrá luego) y «presentacion» (un saludo sin más pistas).
    Se descartan las preguntas «a los miembros de la mesa» y las despedidas del final.
    Aproximado: sale de subtítulos automáticos y los nombres pueden venir mal escritos."""
    if duracion is None:
        duracion = cues[-1][0] if cues else 0
    cues = [(s, sin_mayusculas(t)) for s, t in cues]
    marcas = []
    for n, (seg, txt) in enumerate(cues):
        siguiente = cues[n + 1][1] if n + 1 < len(cues) else ""
        frase = f"{txt} {siguiente}".strip()
        entrada = MESA.search(frase) or (seg < inicio and APERTURA.search(txt))
        # Sin frase de entrada, al empezar: tres o más nombres seguidos, cada uno con su saludo
        # («Vamos a comentarlo con Irene Lozano, buenas noches. Marta Nebot, buenas noches…»).
        sin_mesa = seg < inicio and not any(t == "mesa" for _, t, _ in marcas)
        empieza_ronda = (ENTRADA_SUAVE.search(frase)
                         or nombres(frase) and saludada(cues, n))
        ronda = (tertulianos(cues, n) if not entrada and sin_mesa and empieza_ronda
                 and not CONEXION.search(frase) else [])
        # Un anuncio no tapa lo que venga detrás («nos acompañará…» y enseguida la mesa), y una
        # conexión o un saludo tampoco tapan la mesa que llega justo después.
        ultima = next((t for t in reversed(marcas) if t[1] != "anuncio"), None)
        es_mesa = entrada or len(ronda) >= (2 if ENTRADA_SUAVE.search(frase) else 3)
        if ultima and seg - ultima[0] < separacion and (ultima[1] == "mesa" or not es_mesa):
            continue
        if RUIDO.search(frase):
            continue
        if es_mesa:
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
        if tipo == "plato":
            texto = invitado(frase)
        elif tipo == "mesa":
            if entrada:
                ronda = tertulianos(cues, n, entrada)
            texto = " · ".join(ronda) or "Mesa de análisis (nombres no reconocidos)"
        if len(texto) > largo:
            texto = texto[:largo].rsplit(" ", 1)[0] + "…"
        marcas.append((seg, tipo, texto))
    return marcas


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


def guardar_marcas(conn, episodio_id, cues, duracion, analistas=()):
    """Rehace las marcas de un episodio y el recuento del vocabulario de la tabla de
    analistas a partir de sus subtítulos."""
    conn.execute("DELETE FROM marcas WHERE episodio_id=?", (episodio_id,))
    conn.executemany(
        "INSERT OR IGNORE INTO marcas VALUES(?,?,?,?)",
        [(episodio_id, seg, tipo, txt) for seg, tipo, txt in presentaciones(cues, duracion)])
    conn.execute("DELETE FROM vocabulario WHERE episodio_id=?", (episodio_id,))
    conn.executemany("INSERT INTO vocabulario VALUES(?,?,?)",
                     [(episodio_id, f, v) for f, v in contar_vocabulario(cues, analistas).items()])
    conn.execute("INSERT OR REPLACE INTO recuentos VALUES(?,?)",
                 (episodio_id, firma_vocabulario(analistas)))


def paso_rtve(conn, momento, dias=DIAS_INICIALES, obtener=json_url, bajar=descargar,
              historico=None, analistas=None):
    """Añade episodios y fragmentos nuevos. Devuelve cuántas filas ha escrito."""
    analistas = leer_analistas() if analistas is None else analistas
    firma = firma_vocabulario(analistas)
    hay = conn.execute("SELECT count(*) FROM episodios").fetchone()[0]
    # `historico` repasa más días aunque la base ya tenga episodios (carga de histórico).
    dias = historico or (dias if not hay else DIAS_HABITUALES)
    items = listar_videos(momento - timedelta(days=dias), obtener)
    cache = {}

    def cues_de(video_id):
        if video_id not in cache:
            cache[video_id] = subtitulos_es(video_id, obtener, bajar)
        return cache[video_id]

    escritas = 0
    # Se rehacen todos si cambia cómo se sacan las marcas, y los que se contaron con otro
    # vocabulario (o no se pudieron contar: se reintentan en la siguiente ejecución).
    todos = conn.execute("PRAGMA user_version").fetchone()[0] < VERSION_MARCAS
    rehacer = conn.execute(
        "SELECT e.id, e.duracion_s FROM episodios e LEFT JOIN recuentos r ON r.episodio_id=e.id "
        "WHERE e.estado='ok' AND (? OR r.firma IS NOT ?)", (todos, firma)).fetchall()
    for id_, dur in rehacer:
        # Si RTVE ya no da los subtítulos de un episodio, se quedan sus marcas de antes.
        cues = cues_de(id_)
        if cues:
            guardar_marcas(conn, id_, cues, dur, analistas)
            escritas += 1
    if todos:
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
        guardar_marcas(conn, item["id"], cues or [], duracion, analistas)
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


def plano(texto):
    """Minúsculas y sin acentos, para comparar nombres."""
    return " ".join(unicodedata.normalize("NFKD", texto).encode("ascii", "ignore")
                    .decode().lower().split())


def leer_json(ruta, vacio):
    try:
        return json.loads(Path(ruta).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return vacio


def leer_analistas(ruta=ANALISTAS):
    """Tabla curada de la mesa (analistas.json, versión 1): fichas con id, nombre, tipo
    (tertuliano habitual o invitado), medio, wikipedia, fuentes y variantes de los subtítulos
    seguras, probables y descartadas."""
    return leer_json(ruta, {"analistas": []})["analistas"]


def leer_revisiones(ruta=REVISIONES):
    """Decisiones humanas sobre apariciones concretas (revisiones.json, versión 1)."""
    return leer_json(ruta, {"revisiones": []})["revisiones"]


def formas(analistas):
    """{forma en plano: {id: "segura" | "probable"}}. El nombre cuenta como forma segura."""
    indice = {}
    for a in analistas:
        for nivel, lista in (("segura", [a["nombre"], *a["seguras"]]),
                             ("probable", a["probables"])):
            for f in lista:
                indice.setdefault(plano(f), {}).setdefault(a["id"], nivel)
    return indice


def problemas_analistas(analistas):
    """Errores de la tabla: ids repetidos, una forma en dos personas, formas seguras de una
    sola palabra o formas repetidas. Los tests hacen fallar el PR con ellos; si aun así llegan
    a la exportación, esas formas no se asignan con seguridad (ver identificar)."""
    errores = []
    ids = [a["id"] for a in analistas]
    errores += [f"id repetido: {i}" for i in sorted({i for i in ids if ids.count(i) > 1})]
    for f, personas in sorted(formas(analistas).items()):
        if len(personas) > 1:
            errores.append(f"«{f}» apunta a {', '.join(sorted(personas))}")
    for a in analistas:
        if a.get("tipo") not in ("tertuliano", "invitado"):
            errores.append(f"{a['id']}: tipo «{a.get('tipo')}» (tertuliano o invitado)")
        errores += [f"{a['id']}: forma segura de una sola palabra «{x}»"
                    for x in a["seguras"] if len(x.split()) < 2]
        propias = [plano(x) for x in [a["nombre"], *a["seguras"], *a["probables"]]]
        errores += [f"{a['id']}: «{x}» repetida"
                    for x in sorted({x for x in propias if propias.count(x) > 1})]
        descartadas = {plano(x) for x in a["descartadas"]}
        errores += [f"{a['id']}: «{x}» es a la vez variante y descartada"
                    for x in sorted(descartadas & set(propias))]
    return errores


def identificar(forma, analistas, indice=None, descartes=(), parecido=0.85, margen=0.03):
    """Persona y estado de un nombre de la mesa: (id, "automatica" | "candidata") o (None, None).

    - automatica: el nombre o una variante segura de una sola persona.
    - candidata: una variante probable, o un nombre que no está en la tabla pero se parece
      (≥ `parecido`) a las formas de una persona, sin empate con otra por menos de `margen`.
    - Una forma que la tabla da a dos personas no se asigna a ninguna (salvo que una revisión
      haya descartado a una), y nunca se asigna a quien la tenga en «descartadas».
    `descartes`: personas que una revisión ya rechazó para esta aparición."""
    indice = formas(analistas) if indice is None else indice
    p = plano(forma)
    rechazadas = set(descartes) | {a["id"] for a in analistas
                                   if p in {plano(x) for x in a["descartadas"]}}
    todas = indice.get(p, {})
    personas = {i: nivel for i, nivel in todas.items() if i not in rechazadas}
    if len(personas) == 1:
        (i, nivel), = personas.items()
        return i, "automatica" if nivel == "segura" and len(todas) == 1 else "candidata"
    if personas:  # colisión en la tabla: no se elige
        return None, None
    puntos = {}
    for f, ps in indice.items():
        r = SequenceMatcher(None, p, f).ratio()
        for i in ps:
            if i not in rechazadas and r > puntos.get(i, 0):
                puntos[i] = r
    orden = sorted(puntos.items(), key=lambda kv: -kv[1])
    if orden and orden[0][1] >= parecido and (len(orden) < 2 or orden[0][1] - orden[1][1] >= margen):
        return orden[0][0], "candidata"
    return None, None


def vocabulario(analistas):
    """Lo único que se cuenta en los subtítulos de cada noche: las formas de la tabla y los
    nombres de pila que no comparten dos personas. Así se apoyan las candidatas sin guardar
    la transcripción ni nombres de terceros (el repositorio es público)."""
    pilas = Counter(plano(a["nombre"].split()[0]) for a in analistas)
    return sorted(formas(analistas)), sorted(p for p, c in pilas.items() if c == 1 and len(p) >= 3)


def firma_vocabulario(analistas):
    return hashlib.sha1(json.dumps(vocabulario(analistas)).encode()).hexdigest()[:12]


def contar_vocabulario(cues, analistas):
    """{forma en plano: veces} y {"pila:nombre": veces} en toda la noche, solo del vocabulario."""
    if not analistas or not cues:
        return {}
    texto = sin_mayusculas(" ".join(t for _, t in cues))
    lista_formas, pilas = vocabulario(analistas)
    conjunto = set(lista_formas)
    cuenta = Counter(plano(x) for x, _ in nombres_con_posicion(texto) if plano(x) in conjunto)
    palabras = Counter(plano(w) for w in re.findall(r"\b[A-ZÁÉÍÓÚÑ][a-záéíóúñü]+\b", texto))
    for p in pilas:
        if palabras[p]:
            cuenta[f"pila:{p}"] = palabras[p]
    return dict(cuenta)


def apoyos(forma, ficha, vocab):
    """Señales de esa noche a favor de una candidata. Solo se muestran: nunca cambian su estado
    (dos menciones pueden repetir el mismo error del reconocimiento)."""
    propia = plano(forma)
    senales = []
    otras = [x for x in [ficha["nombre"], *ficha["seguras"], *ficha["probables"]]
             if plano(x) != propia and vocab.get(plano(x))]
    if otras:
        senales.append(f"esa noche también sale «{otras[0]}»")
    # El nombre de pila suelto: sin contar las veces que va dentro de un nombre completo de
    # esa persona (eso ya es la señal anterior) ni el de la propia presentación.
    pila = ficha["nombre"].split()[0]
    completas = {plano(x) for x in [ficha["nombre"], *ficha["seguras"], *ficha["probables"]]}
    dentro = sum(vocab.get(f, 0) for f in completas if f.split()[0] == plano(pila))
    if propia not in completas and propia.split()[0] == plano(pila):
        dentro += 1
    veces = vocab.get(f"pila:{plano(pila)}", 0) - dentro
    if veces >= 2:
        senales.append(f"le llaman «{pila}» {veces} veces")
    return senales


ORDEN_ESTADO = {"revisada": 0, "automatica": 1, "candidata": 2, None: 3}


def personas_de_mesa(episodio_id, texto, analistas, indice, revisiones, vocab, usadas):
    """Cada nombre de una mesa con su persona y su estado. Una revisión manda sobre las reglas:
    confirma a una persona, la descarta (se busca otra sin ella), deja el nombre sin identificar
    o lo quita de la mesa (no_es_mesa). Cada persona sale una vez por mesa, con su estado más
    firme. Si hay revisión, «origen» guarda el estado que daban las reglas."""
    por_id = {a["id"]: a for a in analistas}
    personas = []
    for forma in texto.split(" · "):
        clave = (episodio_id, plano(forma))
        r = revisiones.get(clave)
        if r:
            usadas.add(clave)
        reglas = identificar(forma, analistas, indice)
        if r and r["decision"] == "no_es_mesa":
            # Error de detección: no se muestra en la mesa, pero se conserva (muestra de control).
            personas.append({"forma": forma, "estado": "no_es_mesa", "origen": reglas[1],
                             "origen_id": reglas[0]})
            continue
        if r and r["decision"] == "confirmada" and r.get("persona") in por_id:
            pid, estado = r["persona"], "revisada"
        elif r and r["decision"] == "sin_identificar":
            pid, estado = None, "revisada"
        else:
            descartes = {r.get("persona")} if r and r["decision"] == "descartada" else ()
            pid, estado = identificar(forma, analistas, indice, descartes)
        entrada = {"forma": forma, "estado": estado}
        if r:  # lo que decían las reglas antes de la revisión (para medir su acierto)
            entrada.update(origen=reglas[1], origen_id=reglas[0])
        if pid:
            entrada.update(id=pid, nombre=por_id[pid]["nombre"])
            if estado == "candidata":
                entrada["apoyos"] = apoyos(forma, por_id[pid], vocab)
        repetida = next((x for x in personas if pid and x.get("id") == pid), None)
        if repetida is None:
            personas.append(entrada)
        elif ORDEN_ESTADO[estado] < ORDEN_ESTADO[repetida["estado"]]:
            personas[personas.index(repetida)] = entrada
    return personas


def datos_pagina(conn, analistas=None, revisiones=None):
    """Lo que lee la página. Cada nombre de la mesa lleva su persona de la tabla y su estado
    (automatica, candidata o revisada); los que no se asignan van a «sin_ficha» con sus noches
    y las revisiones que ya no encuentran su aparición, a «revisiones_huerfanas»."""
    analistas = leer_analistas() if analistas is None else analistas
    revisiones = leer_revisiones() if revisiones is None else revisiones
    indice = formas(analistas)
    por_clave = {(r["episodio"], plano(r["forma"])): r for r in revisiones}
    usadas, sin_ficha = set(), {}
    episodios = []
    for id_, titulo, inicio, dur, url, subs, estado in conn.execute(
            "SELECT id, titulo, inicio, duracion_s, url, subtitulos, estado FROM episodios "
            "ORDER BY inicio DESC"):
        vocab = dict(conn.execute("SELECT forma, veces FROM vocabulario WHERE episodio_id=?",
                                  (id_,)))
        marcas = [{"seg": s, "tipo": t, "texto": x} for s, t, x in conn.execute(
            "SELECT seg, tipo, texto FROM marcas WHERE episodio_id=?", (id_,))]
        for m in marcas:
            if m["tipo"] != "mesa" or "no reconocidos" in m["texto"]:
                continue
            m["personas"] = personas_de_mesa(id_, m["texto"], analistas, indice, por_clave,
                                             vocab, usadas)
            m["texto"] = (" · ".join(x.get("nombre", x["forma"]) for x in m["personas"]
                                     if x["estado"] != "no_es_mesa")
                          or "Mesa de análisis (nombres no reconocidos)")
            for x in m["personas"]:
                if "id" not in x and x["estado"] is None:
                    sin_ficha.setdefault(x["forma"], []).append({"episodio": id_, "seg": m["seg"]})
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
        episodios.append({"id": id_, "titulo": titulo, "inicio": inicio, "duracion_s": dur,
                          "url": url, "subtitulos": subs, "estado": estado, "marcas": marcas})
    directos = [dict(zip(("video_id", "titulo", "inicio", "primera_vez", "fin_detectado"), f))
                for f in conn.execute("SELECT * FROM directos ORDER BY primera_vez DESC")]
    estado = {c: {"valor": v, "desde": d} for c, v, d in conn.execute("SELECT * FROM estado")}
    fichas = [{k: a[k] for k in ("id", "nombre", "tipo", "medio", "wikipedia", "fuentes")}
              for a in analistas]
    return {
        "estado": estado, "directos": directos, "episodios": episodios, "analistas": fichas,
        "sin_ficha": [{"forma": f, "veces": len(n), "noches": n} for f, n in
                      sorted(sin_ficha.items(), key=lambda kv: (-len(kv[1]), kv[0]))],
        "revisiones_huerfanas": [r for k, r in por_clave.items() if k not in usadas],
        "reglas": {"version_marcas": VERSION_MARCAS, "vocabulario": firma_vocabulario(analistas),
                   "analistas": hashlib.sha1(json.dumps(analistas, sort_keys=True).encode())
                   .hexdigest()[:12]},
    }


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
        pasos.append(("rtve", lambda: paso_rtve(conn, momento, args.dias,
                                                historico=args.historico)))
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
    a.add_argument("--historico", type=int,
                   help="repasa N días aunque la base ya tenga episodios (cargar histórico)")
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
