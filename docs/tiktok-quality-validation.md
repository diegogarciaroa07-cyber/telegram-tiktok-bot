# Validación de calidad de TikTok

Fecha: 7 de noviembre de 2026 (America/Mexico_City).
Base analizada: `323d9f593fa4ae1a2166165d28923a41eccbef95` de `main`.
Estado: propuesta para revisión; sin despliegue en Render.

## Causas reproducidas

1. El reproductor de TikTok expone representaciones de distribución comprimidas. Escoger la mejor de esa lista no demuestra que sea la mejor accesible por otras rutas.
2. SSSTik ofrece el enlace normal como `href`, pero su botón HD usa `data-directurl`, un token `tt` nuevo y una segunda petición POST. La URL final llega en `HX-Redirect`. El código anterior no hacía esa petición ni leía esa cabecera. También interpretaba `href="#"` como un enlace descargable a la portada.
3. Los métodos 1 y 2 de yt-dlp pasaban `impersonate="chrome"` a la API Python. La versión fijada exige `ImpersonateTarget`. Se reprodujo el `AssertionError` sin texto que explicaba los errores vacíos de Render.
4. La búsqueda se detenía al alcanzar 1080 × 1920 y 1,2 Mbps, y después de la primera alternativa aceptada. Ese umbral ocultaba versiones con mayor bitrate a la misma resolución.
5. La comparación con SSSTik se decidía por un aumento del 35 % en el peso total. Además, medir duración dependía de Mutagen y resolución de un ffprobe que no estaban declarados como dependencias. Sin ellos, se omitían esas comprobaciones.

## Implementación

- Se mantienen Player, SSSTik y los tres métodos de yt-dlp; se consultan aunque el primer archivo ya sea HD. SSSTik también funciona si Player falla.
- Se solicita el botón HD real y se sigue su `HX-Redirect`, con validación del host. Se ignoran enlaces de portada, placeholders y audio.
- Se mide el MP4/MOV descargado: dimensiones, códec, muestras, duración, fps, bitrate de VIDEO y presencia de audio. Se leen las tablas del contenedor sin depender de ffprobe o Mutagen en producción. Las tablas incompletas y los códecs no compatibles se rechazan.
- La selección prioriza resolución y fps, y usa el bitrate de video como desempate. Se rechazan alternativas que difieran sustancialmente en duración o relación de aspecto, o pierdan audio. El peso total nunca decide.
- Las fuentes escriben temporales separados. Un error posterior conserva la mejor descarga válida. Las cookies existentes se cargan en memoria y no se reescriben.
- Se conservan HEVC/H.265 y otros códecs compatibles. No se aumenta resolución ni bitrate por recodificación. `imageio-ffmpeg==0.8.1` proporciona un ejecutable de FFmpeg para que yt-dlp pueda unir pistas separadas mediante stream copy; no se cambia la versión fijada de yt-dlp.
- El endpoint mantiene `POST /download` con `{ "url": "..." }` y devuelve el archivo como antes. Añade cabeceras `X-Video-*`; Telegram muestra las características y puede enviar el archivo como documento si falla el envío como video. Si no se encuentra Full HD o faltan fuentes, se informa de la limitación.
- Instagram, sus rutas HTTP, sus funciones y su entrega en Telegram se compararon con la base: texto idéntico. No se modificaron el servicio, secretos ni archivos de cookies.

## Descargas reales

Se solicitó el archivo anterior al servicio existente de Render. La implementación nueva se ejecutó localmente en Windows, incluyendo peticiones HTTP a Flask y descargas reales de Internet. MediaInfo midió independientemente los archivos recibidos. Se decodificaron completos los seis archivos con FFmpeg (`-v error -xerror -i archivo -f null -`): los seis terminaron con código 0 y sin errores.

Bitrates de la pista de video, en kbps decimales; tamaños en MB decimales. Todos conservan audio y aproximadamente 30 fps.

| Video | Antes: resolución / códec / kbps / MB | Después: resolución / códec / kbps / MB | Duración de video | Tiempo de la nueva petición local |
| --- | --- | --- | --- | --- |
| [Ejemplo del usuario](https://vt.tiktok.com/ZSprE6uYW/) · `7705284346378243349` | 576 × 768 / H.264 / 187 / 0,426 | 1080 × 1440 / HEVC / 6.196 / 11,873 | 15,00 s | 22,44 s |
| [Segundo](https://www.tiktok.com/@_/video/7705284325507452180) | 720 × 960 / HEVC / 400 / 0,879 | 1080 × 1440 / HEVC / 5.957 / 11,307 | 14,90 s | 26,86 s |
| [Tercero](https://www.tiktok.com/@_/video/7705505208041803016) | 1080 × 1920 / HEVC / 1.458 / 2,843 | 1080 × 1920 / HEVC / 6.770 / 12,835 | ≈14,87 s | 30,25 s |

En el tercero, la duración de las muestras da 29,993 fps de media en la versión HD; MediaInfo informa 30 fps nominales. La pequeña diferencia de cálculo no representa una conversión del bot.

El archivo nuevo del ejemplo y la descarga HD de referencia obtenida mediante el formulario de SSSTik tienen exactamente 11.873.409 bytes y el mismo SHA-256:

`012f76c5d08125275b8ec4421e754cad640cabfc6fdaf6a10cdec8616694bede`

Esto verifica que el bot entrega los mismos bytes que esa fuente, sin recodificación. No demuestra que SSSTik tenga el archivo maestro privado del creador.

| Archivo | SHA-256 |
| --- | --- |
| Ejemplo anterior | `0933499356b6a11811b57d2ae343c5a5d4430cb15fdb4dfc144abfcd4f36d7da` |
| Segundo anterior | `09b293e3e1fef70a0f3f42a77f2baaeb4979110be4cceabed3308ae105b6b3ea` |
| Segundo nuevo | `d2bf3e396c9fe93c878d991d51fb4935925628fbe317ce53967fdf16586dcda6` |
| Tercero anterior | `d29904c5685110d6cbf25f96d54088a5912380fc09929ee9992fd92e6b67e9c8` |
| Tercero nuevo | `a18dae25876d0983a1110d0163939cef2654bc78b185db081ca54df8ba44f0a6` |

Los archivos, capturas y resultados completos permanecen en `.local/evidence/` de la copia de trabajo; no se suben videos, respuestas con URLs firmadas ni datos de sesión al repositorio.

## Pruebas y reproducción

1. Instalar `requirements.txt` y `pytest` en un entorno virtual.
2. Ejecutar `python -m pytest -q`: **19 pruebas aprobadas**.
3. Para repetir una descarga sin arrancar el bot de Telegram: `python -c "import bot; bot.descargar_tiktok('https://vt.tiktok.com/ZSprE6uYW/', 'prueba.mp4')"`. El resultado técnico queda también en `prueba_calidad.json`. No se necesita BOT_TOKEN para importar el módulo o ejecutar esta función; sí se necesita para arrancar el programa completo.

Las pruebas cubren el POST HD y su token nuevo, HX-Redirect, fallback sin HD, caída de Player, fallo de otras fuentes, cookies de prueba sin reescritura, selección entre métodos, audio, duración, relación de aspecto, contenedores inflados, archivos truncados y temporales. Comprueban los bytes exactos de la respuesta HTTP y de los archivos pasados a las funciones de Telegram; no envían mensajes reales. Una prueba ejecuta el merger real de yt-dlp y verifica que los paquetes de video conservan su hash.

## Límites y validación pendiente

- Se probó la mejora real en tres publicaciones, no en todos los TikToks existentes. Las fuentes varían por publicación, región, IP y momento. SSSTik es un servicio externo sin garantía de disponibilidad o estabilidad de su interfaz.
- En esta conexión local, Player devolvió HTTP 403; SSSTik entregó HD y yt-dlp entregó las versiones inferiores. La integración nueva todavía no se ha ejecutado dentro de Render, donde el Player existente sí funciona.
- Resolución, fps y bitrate permiten una selección técnica, pero no certifican calidad perceptual absoluta ni detectan por sí solos un escalado previo. El bitrate entre códecs distintos no es directamente equivalente: su desempate es una heurística declarada. La aplicación informa `best-accessible`, no una garantía de archivo maestro original.
- El lector interno acepta los MP4/MOV no fragmentados medidos en estas pruebas. Si una fuente cambia a un contenedor no verificable, se rechaza y se intenta conservar otra descarga válida; no se oculta ese fallo como una mejora.
- Puede haber más latencia, tráfico y archivos mayores. Telegram conserva sus límites de subida: si no puede enviar el archivo, el bot indica usar el Atajo, sin reducirlo.
- Falta aprobar la publicación, instalar/verificar la dependencia en Render y comprobar desde el Telegram y el iPhone reales del usuario. El protocolo HTTP local se probó; no se abrió ni modificó el Atajo.

## Referencias primarias consultadas

- [Formulario actual de SSSTik](https://ssstik.io/es) y sus respuestas reales de descarga.
- [API Python de yt-dlp](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/YoutubeDL.py) e [impersonación](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/networking/impersonate.py).
- [Extractor TikTok de yt-dlp](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/tiktok.py) y [merger FFmpeg](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/postprocessor/ffmpeg.py).
- [Distribución del ejecutable por imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg#installation).
- [Telegram Bot API](https://core.telegram.org/bots/api#sendvideo).
