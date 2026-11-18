import hashlib
import json
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import noche24h as n  # noqa: E402

AHORA = datetime(2026, 11, 17, 12, 0, tzinfo=n.MADRID)


def vtt(cues):
    partes = ["WEBVTT", ""]
    for i, (seg, txt) in enumerate(cues, 1):
        h, m, s = seg // 3600, seg % 3600 // 60, seg % 60
        partes += [str(i), f"{h:02d}:{m:02d}:{s:02d}.000 --> {h:02d}:{m:02d}:{s:02d}.900", txt, ""]
    return "\n".join(partes)


def frases(n_, prefijo):
    return [f"{prefijo} frase número {i} con texto suficiente para casar bien" for i in range(n_)]


def item(id_, tipo, fecha, titulo="t", dur_s=60):
    return {"id": id_, "title": titulo, "type": {"name": tipo},
            "dateOfEmission": fecha.strftime("%d-%m-%Y %H:%M:%S"),
            "publicationDate": fecha.strftime("%d-%m-%Y %H:%M:%S"),
            "duration": dur_s * 1000, "htmlUrl": f"https://rtve/{id_}"}


class FalsaRTVE:
    """API de RTVE en memoria: listado del programa y subtítulos por vídeo."""

    def __init__(self, items, subtitulos):
        self.items, self.subtitulos, self.pedidas = items, subtitulos, []

    def obtener(self, url):
        self.pedidas.append(url)
        if "/programas/" in url:
            pagina = int(url.rsplit("page=", 1)[1])
            lote = self.items[(pagina - 1) * 2:pagina * 2]
            return {"page": {"items": lote, "totalPages": (len(self.items) + 1) // 2}}
        vid = int(url.split("/videos/")[1].split("/")[0])
        if vid not in self.subtitulos:
            raise urllib.error.HTTPError(url, 404, "no", {}, None)
        return {"page": {"items": [{"lang": "es", "src": f"vtt:{vid}"}]}}

    def bajar(self, src):
        return vtt(self.subtitulos[int(src.split(":")[1])]).encode()


class TestSubtitulos(unittest.TestCase):
    def test_leer_vtt(self):
        texto = ("WEBVTT\r\n\r\n1\r\n00:00:01.000 --> 00:00:02.000\r\nhola <i>qué</i>\r\ntal\r\n\r\n"
                 "01:02.500 --> 01:03.000\nsin horas\n\n2\n01:00:00.000 --> 01:00:01.000\n\n")
        self.assertEqual(n.leer_vtt(texto), [(1, "hola qué tal"), (62, "sin horas")])

    def test_alinear_encuentra_el_desfase(self):
        episodio = [(i * 3, t) for i, t in enumerate(frases(300, "ep"))]
        fragmento = [(i * 3, t) for i, t in enumerate(frases(300, "ep")[100:160])]
        self.assertEqual(n.alinear(fragmento, episodio), (300, 60))

    def test_alinear_rechaza_pocas_coincidencias(self):
        episodio = [(i, t) for i, t in enumerate(frases(50, "ep"))]
        fragmento = [(i, t) for i, t in enumerate(frases(5, "ep") + frases(40, "otro"))]
        self.assertIsNone(n.alinear(fragmento, episodio))

    def test_presentaciones_por_tipo(self):
        frases_ = [(5, "Hoy precisamente nos acompañará en unos minutos el presidente del CIS"),
                   (12, "Comenzamos. Hoy nos acompañan en la"), (14, "mesa de análisis Lucía Méndez."),
                   (16, "Buenas noches, Javier Casqueiro."), (20, "Hola, buenas noches."),
                   (300, "Vamos con el turno de preguntas de los miembros de la mesa"),
                   (900, "Como les decía, nos acompaña Arcadi España, ministro de Hacienda."),
                   (1500, "Olivia Guillén, buenas noches, cuéntanos."),
                   (2100, "Volvemos en directo hasta Moncloa,"),
                   (2700, "Pablo Simón, buenas noches."),
                   (3000, "Muy buenas noches desde Nueva York,"),
                   (9000, "Estefanía Molina, muy buenas noches."), (9010, "Así acabamos por hoy")]
        # Entre frase y frase, conversación que no presenta a nadie.
        cues = sorted(frases_ + [(s + 1, "y seguimos con la actualidad del día") for s, _ in frases_])
        marcas = n.presentaciones(cues, duracion=9100)
        self.assertEqual([(s, t) for s, t, _ in marcas],
                         [(5, "anuncio"), (12, "mesa"), (900, "plato"), (1500, "conexion"),
                          (2100, "conexion"), (2700, "presentacion"), (3000, "conexion")])
        self.assertEqual(marcas[1][2], "Lucía Méndez · Javier Casqueiro")

    def test_nombres(self):
        texto = ("Hola, buenas noches, Javier Casqueiro. Entre Ejerique. Consejo General del Poder "
                 "Judicial. Paloma Esteban. Javier Casqueiro.")
        self.assertEqual(n.nombres(texto), ["Javier Casqueiro", "Paloma Esteban"])

    def test_mesa_en_aperturas_reales(self):
        # Trozos de 70 s de subtítulos reales (junio–noviembre de 2026), uno por caso difícil.
        ruta = Path(__file__).resolve().parent / "aperturas.json"
        for caso in json.loads(ruta.read_text(encoding="utf-8")):
            with self.subTest(caso["fecha"], prueba=caso["prueba"]):
                cues = [tuple(c) for c in caso["cues"]]
                mesas = [x for _, t, x in n.presentaciones(cues, caso["duracion"]) if t == "mesa"]
                self.assertEqual(mesas, caso["mesa"])

    def test_sin_mayusculas(self):
        self.assertEqual(n.sin_mayusculas("Chema CRESPO y Ana MARTINEZ HOYOS, del PSOE"),
                         "Chema Crespo y Ana Martinez Hoyos, del PSOE")

    def test_invitado_de_plato(self):
        casos = {
            "humano. Y como les decíamos, hoy nos acompaña Félix Bolaños, ministro de Presidencia":
                "Félix Bolaños, ministro de Presidencia",
            "Semana. Hoy nos acompaña en el plató de la noche Miguel Pardeza, que acaba de "
            "publicar su última novela": "Miguel Pardeza, que acaba de publicar su última novela",
            "Nos acompaña Miriam Jiménez, que es socióloga. Buenas noches, bienvenida":
                "Miriam Jiménez, que es socióloga",
            "pues nos acompaña juanjo fernández ingeniero": "Juanjo fernández ingeniero",
            "Pablo Simón nos acompaña ya. Te voy a pedir": "Pablo Simón",
            "Nos visita en la Noche Teresa Perales ¿qué tal? Buenas noches": "Teresa Perales",
            "Pepón Nieto, bienvenido. Hola. Muchas gracias.": "Pepón Nieto",
        }
        for frase, esperado in casos.items():
            self.assertEqual(n.invitado(frase), esperado)


def ficha(id_, nombre, seguras=(), probables=(), descartadas=()):
    return {"id": id_, "nombre": nombre, "tipo": "tertuliano", "medio": None, "wikipedia": None,
            "fuentes": [],
            "seguras": list(seguras), "probables": list(probables), "descartadas": list(descartadas)}


TABLA = [ficha("pedro-vallin", "Pedro Vallín", ["Pedro Ballín", "Pedro Aballín"]),
         ficha("marta-nebot", "Marta Nebot"),
         ficha("anton-losada", "Antón Losada", ["Antón Lozada"], ["Antonio Sara"]),
         ficha("marisa-cruz", "Marisa Cruz", probables=["María Cruz"])]


class TestAnalistas(unittest.TestCase):
    def test_identificar(self):
        casos = {"Pedro Vallín": ("pedro-vallin", "automatica"),   # nombre exacto
                 "Pedro Ballín": ("pedro-vallin", "automatica"),   # variante segura
                 "pedro ballin": ("pedro-vallin", "automatica"),   # sin tildes ni mayúsculas
                 "Antonio Sara": ("anton-losada", "candidata"),    # variante probable
                 "Pedro Avellín": ("pedro-vallin", "candidata"),   # solo parecido
                 "Juan Cruz": (None, None),                        # no se parece bastante
                 "Pedro Sánchez": (None, None)}
        for forma, esperado in casos.items():
            self.assertEqual(n.identificar(forma, TABLA), esperado, forma)

    def test_colision_empate_y_descartes(self):
        tabla = TABLA + [ficha("otro-vallin", "Pablo Vallín", ["Pedro Ballín"])]
        self.assertEqual(n.identificar("Pedro Ballín", tabla), (None, None))  # dos personas
        self.assertEqual(n.identificar("Pedro Ballín", tabla, descartes={"otro-vallin"}),
                         ("pedro-vallin", "candidata"))
        gemelos = [ficha("a", "Ana Ruiz Gil"), ficha("b", "Ana Ruiz Gal")]
        self.assertEqual(n.identificar("Ana Ruiz Gul", gemelos), (None, None))  # empate
        tabla = [ficha("pedro-vallin", "Pedro Vallín", descartadas=["Pedro Avellín"])]
        self.assertEqual(n.identificar("Pedro Avellín", tabla), (None, None))
        self.assertEqual(n.identificar("Pedro Vallín", tabla, descartes={"pedro-vallin"}),
                         (None, None))

    def test_problemas_de_la_tabla(self):
        mala = [ficha("a", "Ana Ruiz", ["Ruiz"]), ficha("a", "Eva Gil"),
                ficha("b", "Eva Sanz", ["Ana Ruiz"], ["Eva Sanz"], ["Eva Sanz"])]
        mala[1]["tipo"] = "contertulio"
        self.assertEqual(n.problemas_analistas(mala), [
            "id repetido: a",
            "«ana ruiz» apunta a a, b",
            "a: forma segura de una sola palabra «Ruiz»",
            "a: tipo «contertulio» (tertuliano o invitado)",
            "b: «eva sanz» repetida",
            "b: «eva sanz» es a la vez variante y descartada"])

    def test_tablas_del_repo(self):
        tabla = n.leer_analistas()
        self.assertEqual(n.problemas_analistas(tabla), [])
        for a in tabla:
            self.assertRegex(a["id"], r"^[a-z0-9]+(-[a-z0-9]+)*$")
            self.assertTrue(all(u.startswith("https://") for u in a["fuentes"]), a["id"])
            self.assertIn(type(a["wikipedia"]), (str, type(None)))
        ids = {a["id"] for a in tabla}
        for r in n.leer_revisiones():
            self.assertIsInstance(r["episodio"], int)
            self.assertIn(r["decision"], {"confirmada", "descartada", "sin_identificar",
                                          "no_es_mesa"})
            if r["decision"] in ("confirmada", "descartada"):
                self.assertIn(r["persona"], ids)

    def test_identificacion_del_corpus(self):
        # Formas detectadas en las mesas de 84 noches (12/2025–11/2026): solo los nombres.
        # Si cambia una asignación, que sea a propósito: se regenera el fichero y se revisa.
        tabla = n.leer_analistas()
        indice = n.formas(tabla)
        ruta = Path(__file__).resolve().parent / "identificacion.json"
        for caso in json.loads(ruta.read_text(encoding="utf-8")):
            with self.subTest(caso["forma"]):
                self.assertEqual(n.identificar(caso["forma"], tabla, indice),
                                 (caso["id"], caso["estado"]))


class TestRTVE(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ruta = Path(self.tmp.name) / "d.sqlite"
        self.conn = n.abrir(self.ruta)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def api(self, con_fragmento=True, subs_episodio=True):
        ep_ini = datetime(2026, 11, 16, 21, 50, tzinfo=n.MADRID)
        texto = frases(400, "ep")
        items = [item(3, "Fragmento", ep_ini + timedelta(hours=3), "Entrevista a Fulano", 120),
                 item(2, "Completo", ep_ini, "La noche en 24 horas - 16/11/26", 9000),
                 item(1, "Completo", datetime(2026, 9, 1, 22, tzinfo=n.MADRID))]
        if not con_fragmento:
            items = items[1:]
        subs = {3: [(i * 3, t) for i, t in enumerate(texto[200:240])]}
        if subs_episodio:
            subs[2] = [(i * 3, t) for i, t in enumerate(texto)] + [(1300, "nos acompaña Mengana")]
        return FalsaRTVE(items, subs)

    def test_episodio_fragmento_y_marcas(self):
        api = self.api()
        self.assertEqual(n.paso_rtve(self.conn, AHORA, 28, api.obtener, api.bajar), 2)
        ep = self.conn.execute("SELECT id, inicio, duracion_s, estado FROM episodios").fetchall()
        self.assertEqual(ep, [(2, "2026-11-16T21:50:00+01:00", 9000, "ok")])  # el 1 queda fuera
        self.assertEqual(self.conn.execute("SELECT episodio_id, seg, estado FROM fragmentos")
                         .fetchall(), [(2, 600, "ok")])
        self.assertEqual(self.conn.execute("SELECT seg, tipo FROM marcas").fetchall(),
                         [(1300, "plato")])
        datos = n.datos_pagina(self.conn)["episodios"][0]["marcas"]
        self.assertEqual([(m["seg"], m["tipo"]) for m in datos],
                         [(600, "entrevista"), (1300, "plato")])
        self.assertEqual(datos[0]["seg_fin"], 720)

    def test_presentacion_y_fragmento_son_una_linea(self):
        api = self.api()
        api.subtitulos[2][203] = (609, "Como les decía, nos acompaña Mengana, ministra")
        n.paso_rtve(self.conn, AHORA, 28, api.obtener, api.bajar)
        marcas = n.datos_pagina(self.conn)["episodios"][0]["marcas"]
        self.assertEqual([(m["seg"], m["tipo"], m.get("lugar")) for m in marcas],
                         [(600, "entrevista", "plato"), (1300, "plato", None)])
        self.assertTrue(marcas[0]["presenta"].startswith("Mengana, ministra"))

    def test_cambio_de_version_rehace_las_marcas(self):
        api = self.api(con_fragmento=False)
        n.paso_rtve(self.conn, AHORA, 28, api.obtener, api.bajar)
        # Base de antes del cambio: marcas sin clasificar y sin versión.
        self.conn.execute("UPDATE marcas SET tipo='presentacion'")
        self.conn.execute("PRAGMA user_version=1")
        self.conn.commit()
        self.assertEqual(n.paso_rtve(self.conn, AHORA, 28, api.obtener, api.bajar), 1)
        self.assertEqual(self.conn.execute("SELECT seg, tipo FROM marcas").fetchall(),
                         [(1300, "plato")])
        self.assertEqual(self.conn.execute("PRAGMA user_version").fetchone(),
                         (n.VERSION_MARCAS,))
        self.assertEqual(n.paso_rtve(self.conn, AHORA, 28, api.obtener, api.bajar), 0)

    def test_historico_repasa_aunque_haya_episodios(self):
        api = self.api(con_fragmento=False)
        n.paso_rtve(self.conn, AHORA, 28, api.obtener, api.bajar)
        # El episodio del 1/9 queda fuera de los 14 días habituales; con historico=90, dentro.
        self.assertEqual(n.paso_rtve(self.conn, AHORA, 28, api.obtener, api.bajar), 0)
        self.assertEqual(n.paso_rtve(self.conn, AHORA, 28, api.obtener, api.bajar,
                                     historico=90), 1)
        self.assertEqual(self.conn.execute("SELECT id FROM episodios ORDER BY id").fetchall(),
                         [(1,), (2,)])

    def mesa_de_prueba(self):
        api = self.api(con_fragmento=False)
        api.subtitulos[2] += [
            (4990, "y seguimos con la actualidad del día"),
            (5000, "Renovamos la mesa, se incorporan Pedro Aballín, Marta Nebot, Antonio Sara "
                   "y Fulano Mengánez."),
            (5100, "Antón, ¿cómo lo ves?"), (5200, "Antón, una última cosa"),
            (5300, "como decía Anton Losada en su columna")]
        return api

    def test_mesa_con_personas_y_estados(self):
        api = self.mesa_de_prueba()
        n.paso_rtve(self.conn, AHORA, 28, api.obtener, api.bajar, analistas=TABLA)
        datos = n.datos_pagina(self.conn, TABLA, [])
        mesa = [m for m in datos["episodios"][0]["marcas"] if m["tipo"] == "mesa"][0]
        self.assertEqual(mesa["texto"], "Pedro Vallín · Marta Nebot · Antón Losada · Fulano Mengánez")
        self.assertEqual([(p.get("id"), p["estado"]) for p in mesa["personas"]],
                         [("pedro-vallin", "automatica"), ("marta-nebot", "automatica"),
                          ("anton-losada", "candidata"), (None, None)])
        # Las señales de esa noche apoyan a la candidata, pero no cambian su estado.
        self.assertEqual(mesa["personas"][2]["apoyos"],
                         ["esa noche también sale «Antón Losada»", "le llaman «Antón» 2 veces"])
        self.assertEqual(datos["sin_ficha"],
                         [{"forma": "Fulano Mengánez", "veces": 1,
                           "noches": [{"episodio": 2, "seg": 4990}]}])
        self.assertEqual(datos["revisiones_huerfanas"], [])
        # El vocabulario guardado solo tiene formas de la tabla y nombres de pila.
        vocabulario = dict(self.conn.execute("SELECT forma, veces FROM vocabulario"))
        self.assertEqual(vocabulario["pila:anton"], 3)
        self.assertEqual(vocabulario["anton losada"], 1)
        self.assertTrue(all(f.startswith("pila:") or f in n.formas(TABLA) for f in vocabulario))
        self.assertNotIn("fulano menganez", vocabulario)

    def test_revisiones_mandan_sobre_las_reglas(self):
        api = self.mesa_de_prueba()
        n.paso_rtve(self.conn, AHORA, 28, api.obtener, api.bajar, analistas=TABLA)
        revisiones = [
            {"episodio": 2, "forma": "antonio sara", "decision": "confirmada",
             "persona": "anton-losada"},
            {"episodio": 2, "forma": "Pedro Aballín", "decision": "descartada",
             "persona": "pedro-vallin"},
            {"episodio": 2, "forma": "Fulano Mengánez", "decision": "sin_identificar"},
            {"episodio": 99, "forma": "Nadie", "decision": "sin_identificar"}]
        datos = n.datos_pagina(self.conn, TABLA, revisiones)
        mesa = [m for m in datos["episodios"][0]["marcas"] if m["tipo"] == "mesa"][0]
        self.assertEqual([(p.get("id"), p["estado"], p.get("origen", "-")) for p in mesa["personas"]],
                         [(None, None, "automatica"), ("marta-nebot", "automatica", "-"),
                          ("anton-losada", "revisada", "candidata"), (None, "revisada", None)])
        # Lo revisado como «sin identificar» ya no está pendiente; lo descartado, sí.
        self.assertEqual([x["forma"] for x in datos["sin_ficha"]], ["Pedro Aballín"])
        self.assertEqual(datos["revisiones_huerfanas"], [revisiones[3]])

    def test_no_es_mesa_y_una_persona_por_mesa(self):
        api = self.mesa_de_prueba()
        api.subtitulos[2] += [(6000, "y seguimos con la actualidad del día"),
                              (6010, "Renovamos la mesa, se incorporan Antonio Sara, "
                                     "Antón Lozada y Marta Nebot.")]
        n.paso_rtve(self.conn, AHORA, 28, api.obtener, api.bajar, analistas=TABLA)
        revisiones = [{"episodio": 2, "forma": "Marta Nebot", "decision": "no_es_mesa"}]
        mesas = [m for m in n.datos_pagina(self.conn, TABLA, revisiones)["episodios"][0]["marcas"]
                 if m["tipo"] == "mesa"]
        # La segunda mesa: «Antonio Sara» (candidata) y «Antón Lozada» (segura) son la misma
        # persona y queda la aparición más firme; «Marta Nebot» no era de la mesa.
        self.assertEqual([(p.get("id"), p["estado"], p["forma"]) for p in mesas[1]["personas"]],
                         [("anton-losada", "automatica", "Antón Lozada"),
                          (None, "no_es_mesa", "Marta Nebot")])
        self.assertEqual(mesas[1]["personas"][1]["origen"], "automatica")
        self.assertEqual(mesas[1]["texto"], "Antón Losada")
        self.assertNotIn("Marta Nebot", mesas[0]["texto"])

    def test_recuento_que_falla_se_reintenta(self):
        api = self.mesa_de_prueba()
        n.paso_rtve(self.conn, AHORA, 28, api.obtener, api.bajar, analistas=TABLA)
        otra = TABLA + [ficha("fulano", "Fulano Mengánez")]
        guardados = api.subtitulos.pop(2)  # RTVE no da los subtítulos esta vez
        self.assertEqual(n.paso_rtve(self.conn, AHORA, 28, api.obtener, api.bajar,
                                     analistas=otra), 0)
        api.subtitulos[2] = guardados
        self.assertEqual(n.paso_rtve(self.conn, AHORA, 28, api.obtener, api.bajar,
                                     analistas=otra), 1)
        self.assertEqual(n.paso_rtve(self.conn, AHORA, 28, api.obtener, api.bajar,
                                     analistas=otra), 0)

    def test_cambio_de_vocabulario_rehace_los_recuentos(self):
        api = self.mesa_de_prueba()
        n.paso_rtve(self.conn, AHORA, 28, api.obtener, api.bajar, analistas=TABLA)
        self.assertEqual(n.paso_rtve(self.conn, AHORA, 28, api.obtener, api.bajar,
                                     analistas=TABLA), 0)
        otra = TABLA + [ficha("fulano", "Fulano Mengánez")]
        self.assertEqual(n.paso_rtve(self.conn, AHORA, 28, api.obtener, api.bajar,
                                     analistas=otra), 1)
        self.assertIn(("fulano menganez", 1),
                      self.conn.execute("SELECT forma, veces FROM vocabulario").fetchall())

    def test_segunda_pasada_no_escribe_ni_toca_el_fichero(self):
        api = self.api()
        n.paso_rtve(self.conn, AHORA, 28, api.obtener, api.bajar)
        n.anotar(self.conn, "rtve", None, AHORA)
        antes = hashlib.sha256(self.ruta.read_bytes()).hexdigest()
        # Sin novedades no cambia ni un byte: así git no hace commit cada hora.
        self.assertEqual(n.paso_rtve(self.conn, AHORA, 28, api.obtener, api.bajar), 0)
        n.anotar(self.conn, "rtve", None, AHORA)
        self.assertEqual(hashlib.sha256(self.ruta.read_bytes()).hexdigest(), antes)

    def test_sin_subtitulos_se_reintenta(self):
        api = self.api(con_fragmento=False, subs_episodio=False)
        n.paso_rtve(self.conn, AHORA, 28, api.obtener, api.bajar)
        self.assertEqual(self.conn.execute("SELECT estado FROM episodios").fetchone(),
                         ("sin_subtitulos",))
        self.assertEqual(n.paso_rtve(self.conn, AHORA, 28, api.obtener, api.bajar), 0)
        api2 = self.api(con_fragmento=False)
        self.assertEqual(n.paso_rtve(self.conn, AHORA, 28, api2.obtener, api2.bajar), 1)
        self.assertEqual(self.conn.execute("SELECT estado FROM episodios").fetchone(), ("ok",))

    def test_fragmento_sin_episodio_espera_y_luego_se_cierra(self):
        frag = item(9, "Fragmento", AHORA - timedelta(hours=2))
        api = FalsaRTVE([frag], {})
        n.paso_rtve(self.conn, AHORA, 28, api.obtener, api.bajar)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM fragmentos").fetchone(), (0,))
        n.paso_rtve(self.conn, AHORA + timedelta(days=4), 28, api.obtener, api.bajar)
        self.assertEqual(self.conn.execute("SELECT estado FROM fragmentos").fetchone(),
                         ("sin_episodio",))

    def test_listar_videos_pagina_hasta_la_fecha(self):
        fechas = [AHORA - timedelta(days=d) for d in (0, 5, 10, 20, 40, 50)]
        api = FalsaRTVE([item(i, "Completo", f) for i, f in enumerate(fechas)], {})
        vistos = n.listar_videos(AHORA - timedelta(days=28), api.obtener)
        self.assertEqual([i["id"] for i in vistos], [0, 1, 2, 3])
        self.assertEqual(len(api.pedidas), 3)  # la tercera página cruza la fecha y para


def memoria(caso):
    conn = n.abrir(":memory:")
    caso.addCleanup(conn.close)
    return conn


class TestYouTube(unittest.TestCase):
    def setUp(self):
        self.conn = memoria(self)

    def filas(self):
        return self.conn.execute(
            "SELECT video_id, inicio, fin_detectado FROM directos ORDER BY video_id").fetchall()

    def test_sesiones(self):
        inicios = {"A": "2026-03-16T20:01:38+01:00", "B": "2026-11-17T23:10:00+01:00"}
        t1 = AHORA
        self.assertEqual(n.paso_youtube(self.conn, t1, [("A", "CANAL 24H")], inicios.get), 1)
        self.assertEqual(n.paso_youtube(self.conn, t1, [("A", "CANAL 24H")], inicios.get), 0)
        self.assertEqual(n.paso_youtube(self.conn, t1, [], inicios.get), 0)  # vacía: no cierra
        t2 = datetime(2026, 11, 18, 0, 5, tzinfo=n.MADRID)
        n.paso_youtube(self.conn, t2, [("B", "CANAL 24H")], inicios.get)
        self.assertEqual(self.filas(), [("A", inicios["A"], inicios["B"]),
                                        ("B", inicios["B"], None)])

    def test_sin_hora_de_inicio_se_reintenta_y_se_cierra_al_detectar(self):
        n.paso_youtube(self.conn, AHORA, [("A", "x")], lambda v: None)
        self.assertEqual(n.paso_youtube(self.conn, AHORA, [("A", "x")], lambda v: "2026-01-01T00:00:00+01:00"), 1)
        n.paso_youtube(self.conn, AHORA, [("B", "x")], lambda v: None)
        self.assertEqual(self.filas()[0][2], n.iso(AHORA))

    def test_listar_directos_24h(self):
        salida = {"entries": [
            {"id": "c", "title": "🔴DIRECTO | Congreso", "duration": None},
            {"id": "v", "title": "🔴DIRECTO | CANAL 24H | TODA LA ACTUALIDAD", "duration": None},
            {"id": "x", "title": "CANAL 24H resumen", "duration": 300.0}]}
        falso = lambda *a, **k: SimpleNamespace(returncode=0, stdout=json.dumps(salida), stderr="")
        self.assertEqual(n.listar_directos_24h(falso), [("v", salida["entries"][1]["title"])])
        roto = lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="Sign in")
        with self.assertRaises(RuntimeError):
            n.listar_directos_24h(roto)

    def test_inicio_directo(self):
        falso = lambda *a, **k: SimpleNamespace(returncode=0, stdout='{"release_timestamp": 1773687698}')
        self.assertEqual(n.inicio_directo("v", falso), "2026-03-16T20:01:38+01:00")
        roto = lambda *a, **k: SimpleNamespace(returncode=1, stdout="")
        self.assertIsNone(n.inicio_directo("v", roto))


class TestEstadoYExportar(unittest.TestCase):
    def test_anotar_solo_avisa_del_error_nuevo(self):
        conn = memoria(self)
        self.assertFalse(n.anotar(conn, "rtve", None, AHORA))
        self.assertTrue(n.anotar(conn, "rtve", "403", AHORA))
        self.assertFalse(n.anotar(conn, "rtve", "403", AHORA))
        self.assertFalse(n.anotar(conn, "rtve", "500", AHORA))
        self.assertFalse(n.anotar(conn, "rtve", None, AHORA))
        self.assertEqual(conn.execute("SELECT valor FROM estado").fetchone(), ("ok",))

    def test_exportar_solo_si_cambia(self):
        conn = memoria(self)
        n.paso_youtube(conn, AHORA, [("A", "CANAL 24H")], lambda v: None)
        with tempfile.TemporaryDirectory() as d:
            destino = Path(d) / "docs" / "datos.json"
            self.assertTrue(n.exportar(conn, destino, AHORA))
            self.assertFalse(n.exportar(conn, destino, AHORA + timedelta(hours=1)))
            datos = json.loads(destino.read_text())
            self.assertEqual(datos["generado"], n.iso(AHORA))
            self.assertEqual(datos["directos"][0]["video_id"], "A")
            n.anotar(conn, "youtube", None, AHORA)
            self.assertTrue(n.exportar(conn, destino, AHORA))


class TestDatosPublicados(unittest.TestCase):
    def test_datos_json_del_repo_es_valido(self):
        ruta = n.SALIDA
        if not ruta.exists():
            self.skipTest("sin datos.json todavía")
        datos = json.loads(ruta.read_text(encoding="utf-8"))
        # Las claves de los analistas llegan con la primera exportación tras añadirlos.
        self.assertEqual(set(datos) - {"sin_ficha", "analistas", "revisiones_huerfanas", "reglas"},
                         {"generado", "estado", "directos", "episodios"})
        for ep in datos["episodios"]:
            self.assertTrue(ep["url"] and ep["inicio"])
            for m in ep["marcas"]:
                self.assertIn(m["tipo"], {"entrevista", "presentacion", "mesa", "plato",
                                          "conexion", "anuncio"})
                self.assertLessEqual(0, m["seg"])


if __name__ == "__main__":
    unittest.main()
