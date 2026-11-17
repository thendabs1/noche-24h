# La noche en 24h

Índice para seguir «La noche en 24h» (Canal 24h de RTVE, de lunes a jueves, hacia las 22:00–00:30) a tu ritmo.

**Página:** https://thendabs1.github.io/noche-24h/

Contenido de la página:

- **Directo:** enlace al directo de Canal 24h en YouTube y en RTVE Play. Se destaca cuando el programa está probablemente en emisión.
- **Episodios:** los de las últimas semanas, cada uno con:
  - **entrevistas:** los fragmentos que RTVE publica aparte, situados en su minuto dentro del episodio completo;
  - **presentaciones:** frases tipo «nos acompaña…» o «X, buenas noches» sacadas de los subtítulos.
- **Horas:** cada marca lleva la hora de reloj (22:36) y el minuto del episodio (0:36:38).
- **Enlace a YouTube:** aparece cuando alguna sesión del directo aún conserva ese momento.
- **Buscador** de invitados y temas, y casilla de visto/pendiente (se guarda en tu navegador).

No guarda transcripciones: enlaza a los subtítulos de la API oficial de RTVE.

## Cómo funciona

`noche24h.py actualizar` corre cada hora en GitHub Actions y hace dos pasos:

1. **RTVE:**
   - Pide los vídeos del programa (`api.rtve.es/api/programas/25970/videos.json`).
   - De cada episodio completo lee los subtítulos (`/api/videos/{id}/subtitulos.json`) para sacar las presentaciones.
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
| `marcas` | presentaciones: episodio, segundo, frase |
| `fragmentos` | entrevistas: episodio, segundo de inicio, líneas que casan, estado (`ok`, `sin_episodio`, `sin_casar`, `sin_subtitulos`) |
| `directos` | sesiones del directo de YouTube: vídeo, inicio, primera vez visto, fin detectado |
| `estado` | último estado de cada paso (`ok` o el error) y desde cuándo |

## Limitaciones

- Las presentaciones salen de subtítulos automáticos: hay ruido y los nombres pueden venir mal escritos («Feristeza» por Félix Tezanos).
- Solo hay marcas de entrevista cuando RTVE publica el fragmento, uno o dos por semana.
- YouTube deja rebobinar un directo unas horas como mucho (la página supone 12, sin verificar en este canal). Para noches anteriores, el episodio en RTVE Play.
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
