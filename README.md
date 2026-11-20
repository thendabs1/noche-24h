# La noche en 24h

Índice para seguir «La noche en 24h» (Canal 24h de RTVE, de lunes a jueves, hacia las 22:00–00:30) a tu ritmo.

**Página:** https://thendabs1.github.io/noche-24h/

Contenido de la página:

- **Directo:** enlace al directo de Canal 24h en YouTube y en RTVE Play. Se destaca cuando el programa está probablemente en emisión.
- **Episodios:** los de las últimas semanas, cada uno con lo que sale de los subtítulos:
  - **mesa:** los tertulianos al empezar y al renovarla, con sus nombres. Se reconoce por la frase de entrada («Hoy nos acompañan en la mesa de análisis…», «se incorporan…») o, sin ella, por una ronda de nombres con su saludo («Irene Lozano, buenas noches. Marta Nebot, buenas noches…»). La ronda acaba en la primera pregunta («Empiezo por ti»);
  - **plató:** invitados en el estudio («nos acompaña X, cargo»), con solo el nombre y el cargo;
  - **conexión:** redacción, reporteros, corresponsales y entrevistas a distancia («cuéntanos», «volvemos en directo»);
  - **avance:** al empezar, quién vendrá luego («nos acompañará…»);
  - **presenta:** un saludo «X, buenas noches» sin más pistas;
  - **entrevistas:** los fragmentos que RTVE publica aparte, situados en su minuto. Si coinciden con la presentación de un invitado, van en una sola línea («Entrevista en plató»).
- **Horas:** cada marca lleva la hora de reloj (22:36) y el minuto del episodio (0:36:38).
- **Enlace a RTVE Play:** cada marca abre el episodio en su segundo (`?t=`).
- **Buscador** de invitados y temas, y casilla de visto/pendiente (se guarda en tu navegador).
- **Analistas** (otra pestaña): cada tertuliano con su medio, su artículo de Wikipedia si lo tiene, sus noches en la mesa con enlace al minuto, con quién coincide más y cuándo salió por última vez. Con la estrella lo sigues: sus noches se destacan en la lista de episodios (se guarda en tu navegador).
- **Pendientes** (otra pestaña): lo que la identificación automática no asegura (noches «por confirmar» y nombres sin identificar), con enlace al minuto y un formulario de issue rellenado para confirmarlo o corregirlo, y una muestra de control de 30 apariciones automáticas para medir el acierto.

No guarda transcripciones: enlaza a los subtítulos de la API oficial de RTVE.

## Cómo funciona

`noche24h.py actualizar` corre cada hora en GitHub Actions y hace dos pasos:

1. **RTVE:**
   - Pide los vídeos del programa (`api.rtve.es/api/programas/25970/videos.json`).
   - De cada episodio completo lee los subtítulos (`/api/videos/{id}/subtitulos.json`). De ahí saca las presentaciones por tipo.
   - Cuando cambia cómo se sacan (`VERSION_MARCAS`, guardada en `PRAGMA user_version`), se rehacen las de todos los episodios en la siguiente ejecución.
   - Cada fragmento lo sitúa comparando sus líneas de subtítulos con las del episodio. El episodio candidato es el emitido entre 30 h antes y 1 h después del fragmento.
   - Lo que aún no tiene subtítulos o episodio se reintenta durante 3 días.
2. **YouTube:**
   - Busca con `yt-dlp` el directo «CANAL 24H» en la lista de directos de RTVE Noticias y guarda su vídeo y su hora de inicio.
   - Cuando YouTube reinicia el directo aparece otro vídeo: la sesión anterior se da por cortada cuando empezó la nueva.
   - Así el botón «Ver en YouTube» lleva al directo de Canal 24h y no al de otro evento del canal.

Todo va a `datos/noche24h.sqlite` y se exporta a `docs/datos.json`, que lee la página.

Solo hay commit cuando algo cambia. Para cargar noches antiguas: en Actions, «actualizar» → *Run workflow* con el campo `historico` (días que repasar). Si un paso empieza a fallar, la ejecución sale en rojo una vez (aviso por correo de GitHub) y el fallo aparece al pie de la página hasta que se arregla.

### Tertulianos: identificación automática que se corrige

La identificación es automática y se publica sin esperar a nadie; lo dudoso se marca y se corrige rápido. Hay dos ficheros que se editan a mano y se revisan en los PR:

- **`analistas.json`**, la tabla curada: por persona, `id`, `nombre`, `tipo` (`tertuliano` habitual o `invitado` de una o pocas noches, que en la web van en una lista aparte), `medio` (solo si está documentado), `wikipedia` (título del artículo, tomado de la lista del programa en Wikipedia o comprobado a mano; nunca buscado por el nombre), `fuentes` (URL) y las variantes de los subtítulos: `seguras` (diferencia obvia: «Pedro Ballín»), `probables` (ambiguas: «Antonio Sala») y `descartadas` (formas que no son esa persona).
- **`revisiones.json`**, decisiones humanas sobre apariciones concretas: `episodio` (id RTVE), `forma` (el nombre tal como sale), `decision` (`confirmada`, `descartada`, `sin_identificar` o `no_es_mesa`, que la quita de la mesa), `persona` (id; en un descarte, a quién se descarta), `evidencia`, `issue`, `fecha`.

Al exportar, cada nombre de la mesa recibe un estado:

| Estado | Cuándo | En la web |
|---|---|---|
| `revisada` | hay una decisión en `revisiones.json` (manda sobre todo lo demás) | normal |
| `automatica` | el nombre exacto o una variante segura de una sola persona | normal; cuenta en la ficha |
| `candidata` | una variante probable o un parecido ≥ 0,85 sin empate con otra persona | con «?»; se cuenta aparte y no entra en las coincidencias |
| sin estado | no casa con nadie | sin ficha; sale en Pendientes |

A las candidatas se les añaden las señales de esa noche (otra forma de su nombre, o el presentador diciendo su nombre de pila), solo como información: nunca cambian el estado. Para calcularlas, de cada noche se guarda únicamente cuántas veces sale cada forma de la tabla y cada nombre de pila (tabla `vocabulario`), no la transcripción ni nombres de terceros: el repositorio es público. Si cambia ese vocabulario, se rehacen los recuentos de todos los episodios en la siguiente ejecución.

Los tests fallan si una forma apunta a dos personas, si una forma segura es de una sola palabra o si una revisión cita a alguien que no está en la tabla. La exportación lista en `revisiones_huerfanas` las revisiones que ya no encuentran su nombre en la mesa, y en cada aparición revisada guarda lo que decían las reglas (`origen`, `origen_id`). La muestra de control de Pendientes sale de las apariciones automáticas del 31/08 al 18/11/2026 (siempre las mismas 30) y cuenta aciertos y errores según se revisan.

Cada cambio en las formas de `analistas.json` vuelve a contar todos los episodios (se descargan sus subtítulos otra vez); los que no se pueden contar se reintentan en la siguiente ejecución.

### Tablas

| Tabla | Contenido |
|---|---|
| `episodios` | id RTVE, título, inicio (hora de emisión), duración, enlaces, estado (`ok` / `sin_subtitulos`) |
| `marcas` | presentaciones: episodio, segundo, tipo (`mesa`, `plato`, `conexion`, `anuncio`, `presentacion`), texto |
| `fragmentos` | entrevistas: episodio, segundo de inicio, líneas que casan, estado (`ok`, `sin_episodio`, `sin_casar`, `sin_subtitulos`) |
| `directos` | sesiones del directo de YouTube: vídeo, inicio, primera vez visto, fin detectado |
| `estado` | último estado de cada paso (`ok` o el error) y desde cuándo |
| `vocabulario` | por episodio, cuántas veces salen las formas de `analistas.json` y los nombres de pila |
| `recuentos` | por episodio, la firma del vocabulario con que se contó (si cambia, se recuenta) |

## Limitaciones

- Las presentaciones salen de subtítulos automáticos: hay ruido y los nombres pueden venir mal escritos («Feristeza» por Félix Tezanos). El tipo es aproximado. En 59 noches (junio–noviembre de 2026) revisadas a mano, la mesa de apertura trae el 93 % de los tertulianos y 3 nombres de más. Los que faltan casi siempre llegan sin nombre completo en los subtítulos («Peral», «Elsa», «Malucas»). Un mismo tertuliano puede salir escrito de varias formas («Pedro Ballín / Vallín / Aballín»). Antes de diciembre de 2025 el programa tenía otro formato (unas 2,5 h, con la tertulia hacia la hora) y casi nunca se reconoce la mesa. Un saludo suelto puede ser una entrevista a distancia, un tertuliano nuevo o una despedida.
- Solo hay marcas de entrevista cuando RTVE publica el fragmento, uno o dos por semana.
- Las marcas no enlazan a YouTube: el directo solo deja rebobinar 4 h (medido el 17/11/2026 en su lista HLS), los subtítulos suelen llegar después y el enlace no lleva al minuto. RTVE Play sí.
- El fin de una sesión del directo es aproximado: como tarde, el inicio de la siguiente.
- GitHub retrasa a veces los cron y los desactiva tras 60 días sin actividad en el repo (los commits de datos cuentan como actividad).

## Uso local

```sh
pip install yt-dlp                      # solo para el paso de YouTube
python noche24h.py actualizar           # la primera vez trae 28 días (--dias N para cambiarlo)
python noche24h.py actualizar --sin-youtube
python noche24h.py actualizar --historico 80   # repasa 80 días aunque la base ya tenga episodios
python -m unittest discover -s tests -v
python -m http.server -d docs           # y abrir http://localhost:8000
```
