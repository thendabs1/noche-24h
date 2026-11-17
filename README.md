# La noche en 24h

Índice para seguir «La noche en 24h» (Canal 24h de RTVE, de lunes a jueves, hacia las 22:00–00:30) a tu ritmo.

**Página:** https://thendabs1.github.io/noche-24h/

Contenido de la página:

- **Directo:** enlace al directo de Canal 24h en YouTube y en RTVE Play. Se destaca cuando el programa está probablemente en emisión.
- **Episodios:** los de las últimas semanas, cada uno con lo que sale de los subtítulos:
  - **mesa:** los tertulianos al empezar («Hoy nos acompañan en la mesa de análisis…») y al renovarla, con sus nombres;
  - **plató:** invitados en el estudio («nos acompaña X, cargo»);
  - **conexión:** redacción, reporteros, corresponsales y entrevistas a distancia («cuéntanos», «volvemos en directo»);
  - **avance:** al empezar, quién vendrá luego («nos acompañará…»);
  - **presenta:** un saludo «X, buenas noches» sin más pistas;
  - **entrevistas:** los fragmentos que RTVE publica aparte, situados en su minuto. Si coinciden con la presentación de un invitado, van en una sola línea («Entrevista en plató»).
- **Solo elecciones:** muestra los tramos en que se habla de elecciones y lo que cae dentro de ellos.
- **Horas:** cada marca lleva la hora de reloj (22:36) y el minuto del episodio (0:36:38).
- **Enlace a YouTube:** aparece cuando alguna sesión del directo aún conserva ese momento.
- **Buscador** de invitados y temas, y casilla de visto/pendiente (se guarda en tu navegador).

No guarda transcripciones: enlaza a los subtítulos de la API oficial de RTVE.

## Cómo funciona

`noche24h.py actualizar` corre cada hora en GitHub Actions y hace dos pasos:

1. **RTVE:**
   - Pide los vídeos del programa (`api.rtve.es/api/programas/25970/videos.json`).
   - De cada episodio completo lee los subtítulos (`/api/videos/{id}/subtitulos.json`). De ahí saca las presentaciones por tipo y los tramos sobre elecciones (minutos con varias palabras clave: campaña, candidato, encuesta, escaños…).
   - Cuando cambia cómo se sacan (`VERSION_MARCAS`, guardada en `PRAGMA user_version`), se rehacen las de todos los episodios en la siguiente ejecución.
   - Cada fragmento lo sitúa comparando sus líneas de subtítulos con las del episodio. El episodio candidato es el emitido entre 30 h antes y 1 h después del fragmento.
   - Lo que aún no tiene subtítulos o episodio se reintenta durante 3 días.
2. **YouTube:**
   - Busca con `yt-dlp` el directo «CANAL 24H» en la lista de directos de RTVE Noticias y guarda su vídeo y su hora de inicio.
   - Cuando YouTube reinicia el directo aparece otro vídeo: la sesión anterior se da por cortada cuando empezó la nueva.
   - Así queda registrado qué vídeo cubría cada hora.

Todo va a `datos/noche24h.sqlite` y se exporta a `docs/datos.json`, que lee la página. Solo hay commit cuando algo cambia. Si un paso empieza a fallar, la ejecución sale en rojo una vez (aviso por correo de GitHub) y el fallo aparece al pie de la página hasta que se arregla.

### Tablas

| Tabla | Contenido |
|---|---|
| `episodios` | id RTVE, título, inicio (hora de emisión), duración, enlaces, estado (`ok` / `sin_subtitulos`) |
| `marcas` | presentaciones: episodio, segundo, tipo (`mesa`, `plato`, `conexion`, `anuncio`, `presentacion`), texto |
| `tramos` | tramos de un tema dentro del episodio: episodio, tema (`elecciones`), segundo inicial y final |
| `fragmentos` | entrevistas: episodio, segundo de inicio, líneas que casan, estado (`ok`, `sin_episodio`, `sin_casar`, `sin_subtitulos`) |
| `directos` | sesiones del directo de YouTube: vídeo, inicio, primera vez visto, fin detectado |
| `estado` | último estado de cada paso (`ok` o el error) y desde cuándo |

## Limitaciones

- Las presentaciones salen de subtítulos automáticos: hay ruido y los nombres pueden venir mal escritos («Feristeza» por Félix Tezanos). El tipo es aproximado: en una revisión a ojo de 14 episodios casi todas las mesas, platós y conexiones salían bien, pero alguna mesa trae un nombre de más o partido. Un saludo suelto puede ser una entrevista a distancia, un tertuliano nuevo o una despedida.
- Los tramos de elecciones cuentan palabras clave, no distinguen qué elecciones: también salen las de Brasil o las de mitad de mandato de EE. UU.
- Solo hay marcas de entrevista cuando RTVE publica el fragmento, uno o dos por semana.
- El directo de Canal 24h en YouTube solo deja rebobinar 4 horas (medido el 17/11/2026 en su lista HLS). Para noches anteriores, RTVE Play: cada marca enlaza al episodio en su segundo (`?t=`).
- El fin de una sesión del directo es aproximado: como tarde, el inicio de la siguiente.
- GitHub retrasa a veces los cron y los desactiva tras 60 días sin actividad en el repo (los commits de datos cuentan como actividad).

## Uso local

```sh
pip install yt-dlp                      # solo para el paso de YouTube
python noche24h.py actualizar           # la primera vez trae 28 días (--dias N para cambiarlo)
python noche24h.py actualizar --sin-youtube
python -m unittest discover -s tests -v
python -m http.server -d docs           # y abrir http://localhost:8000
```
