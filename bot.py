from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

import asyncio
import html
import json
import subprocess
from urllib.parse import urljoin, urlsplit
import glob
import os
import re
import shutil
import threading
import time
import uuid
import zipfile
from http.cookiejar import MozillaCookieJar

import instaloader
import requests
import yt_dlp
from curl_cffi import requests as cffi_requests
from flask import Flask, after_this_request, request, send_file

TOKEN = os.getenv("BOT_TOKEN")
TIKTOK_IID = os.getenv("TIKTOK_IID")
TIKTOK_COOKIEFILE = "cookies.txt"
INSTAGRAM_COOKIEFILE = "instagram_cookies.txt"

TIKTOK_PLAYER_API = "https://www.tiktok.com/player/api/v1/items"
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
)

app_web = Flask(__name__)
cola_descargas = asyncio.Lock()
cola_tiktok = threading.Lock()


@app_web.route("/")
def home():
    return {
        "status": "ok",
        "service": "telegram-tiktok-bot",
        "instagram_stories": "disabled",
    }, 200


@app_web.route("/healthz")
def healthz():
    return {"status": "ok"}, 200


# ========================= UTILIDADES =========================

def es_tiktok(url):
    texto = str(url or "").lower()
    return any(x in texto for x in (
        "tiktok.com",
        "vt.tiktok.com",
        "vm.tiktok.com",
        "tiktokv.com",
    ))


def _es_historia_instagram(url):
    texto = str(url or "").strip().lower()
    return (
        "instagram.com/stories/" in texto
        or "instagr.am/stories/" in texto
        or "instagram.com/s/" in texto
        or "instagr.am/s/" in texto
    )


def es_instagram(url):
    texto = str(url or "").strip().lower()
    return "instagram.com/" in texto or "instagr.am/" in texto


def es_link_valido(url):
    return es_tiktok(url) or es_instagram(url)


def limpiar_temporales(nombre_archivo):
    base = os.path.splitext(nombre_archivo)[0]
    for archivo in glob.glob(f"{base}*"):
        try:
            if os.path.isfile(archivo):
                os.remove(archivo)
        except Exception as e:
            print(f"No se pudo borrar {archivo}: {e}", flush=True)


def limpiar_directorio(ruta):
    if ruta and os.path.isdir(ruta):
        shutil.rmtree(ruta, ignore_errors=True)


def _es_mp4_valido(ruta):
    try:
        if not os.path.exists(ruta) or os.path.getsize(ruta) < 12:
            return False
        with open(ruta, "rb") as f:
            cabecera = f.read(12)
        return len(cabecera) >= 8 and cabecera[4:8] == b"ftyp"
    except Exception:
        return False


# ========================= TIKTOK =========================

def _tiktok_id_desde_texto(texto):
    texto = str(texto or "")
    patrones = (
        r"/(?:video|photo)/(\d{15,22})",
        r"\bitem_ids=(\d{15,22})\b",
    )
    for patron in patrones:
        m = re.search(patron, texto, re.I)
        if m:
            return m.group(1)
    return None


def resolver_tiktok_id(url):
    video_id = _tiktok_id_desde_texto(url)
    if video_id:
        return video_id

    try:
        r = cffi_requests.get(
            url,
            headers={"User-Agent": BROWSER_UA},
            impersonate="chrome",
            allow_redirects=True,
            timeout=20,
        )
        video_id = _tiktok_id_desde_texto(r.url)
        if video_id:
            return video_id
        video_id = _tiktok_id_desde_texto(r.text)
        if video_id:
            return video_id
    except Exception as e:
        print(f"TikTok: no pude resolver el link corto: {e}", flush=True)

    raise RuntimeError("No pude identificar el ID del TikTok")


def _perfiles_player(item):
    perfiles = []
    for perfil in ((item.get("video_info") or {}).get("profiles") or []):
        play = perfil.get("play_addr") or {}
        urls = [u for u in (play.get("url_list") or []) if str(u).startswith("https://")]
        if not urls:
            continue

        codec = str(perfil.get("codec_type") or "").lower()
        width = int(play.get("width") or 0)
        height = int(play.get("height") or 0)
        bitrate = int(perfil.get("bitrate") or 0)
        data_size = int(play.get("data_size") or 0)
        perfiles.append({
            "urls": urls,
            "codec": codec,
            "width": width,
            "height": height,
            "pixels": width * height,
            "bitrate": bitrate,
            "data_size": data_size,
        })

    # Máxima calidad real: primero resolución, después bitrate y tamaño.
    # No se limita a H.264; HEVC/H.265 se conserva cuando TikTok lo ofrece
    # en una calidad superior.
    perfiles.sort(
        key=lambda p: (p["pixels"], p["bitrate"], p["data_size"]),
        reverse=True,
    )
    return perfiles


def _descargar_url_player(media_url, video_id, destino):
    r = cffi_requests.get(
        media_url,
        headers={
            "User-Agent": BROWSER_UA,
            "Referer": f"https://www.tiktok.com/player/v1/{video_id}",
            "Accept": "video/mp4,video/*;q=0.9,*/*;q=0.5",
        },
        impersonate="chrome",
        stream=True,
        allow_redirects=True,
        timeout=90,
    )
    r.raise_for_status()

    with open(destino, "wb") as f:
        for bloque in r.iter_content(chunk_size=1024 * 1024):
            if bloque:
                f.write(bloque)

    if not _es_mp4_valido(destino):
        try:
            os.remove(destino)
        except Exception:
            pass
        raise RuntimeError("TikTok Player no devolvió un MP4 válido")

    return destino


def descargar_tiktok_player(url, nombre_archivo):
    video_id = resolver_tiktok_id(url)
    print(f"TikTok Player: consultando {video_id}", flush=True)

    r = cffi_requests.get(
        TIKTOK_PLAYER_API,
        params={
            "item_ids": video_id,
            "language": "en",
            "aid": "1459",
            "data_source": "web_core",
        },
        headers={
            "User-Agent": BROWSER_UA,
            "Accept": "application/json",
            "Referer": f"https://www.tiktok.com/player/v1/{video_id}",
        },
        impersonate="chrome",
        allow_redirects=True,
        timeout=35,
    )

    if r.status_code != 200:
        raise RuntimeError(f"TikTok Player API respondió HTTP {r.status_code}")

    try:
        data = r.json()
    except Exception as e:
        raise RuntimeError("TikTok Player API devolvió una respuesta inválida") from e

    items = data.get("items") or []
    if int(data.get("status_code") or 0) != 0 or not items:
        raise RuntimeError(
            f"TikTok Player API no devolvió el video (status {data.get('status_code')})"
        )

    item = items[0]
    item_id = str(item.get("id_str") or item.get("id") or "")
    if item_id and item_id != str(video_id):
        raise RuntimeError("TikTok Player API devolvió un video diferente")

    perfiles = _perfiles_player(item)
    if not perfiles:
        raise RuntimeError("TikTok Player API no devolvió perfiles de video")

    print("TikTok Player: calidades disponibles", flush=True)
    for perfil in perfiles:
        bitrate_kbps = perfil["bitrate"] // 1000 if perfil["bitrate"] else 0
        print(
            f"TikTok Player: {perfil['width']}x{perfil['height']} "
            f"{perfil['codec'] or 'codec desconocido'} {bitrate_kbps} kbps",
            flush=True,
        )

    limpiar_temporales(nombre_archivo)
    ultimo_error = None
    for perfil in perfiles:
        bitrate_kbps = perfil["bitrate"] // 1000 if perfil["bitrate"] else 0
        print(
            f"TikTok Player: probando {perfil['width']}x{perfil['height']} "
            f"{perfil['codec'] or 'codec desconocido'} {bitrate_kbps} kbps",
            flush=True,
        )
        for media_url in perfil["urls"]:
            try:
                _descargar_url_player(media_url, video_id, nombre_archivo)
                print(
                    f"TikTok Player: descargado {perfil['width']}x{perfil['height']} "
                    f"{perfil['codec'] or 'codec desconocido'} {bitrate_kbps} kbps",
                    flush=True,
                )
                # El Player no siempre ofrece el archivo de mayor calidad.
                # La búsqueda adicional solo se realiza cuando su versión es limitada.
                _comprobar_calidad_alternativa(url, nombre_archivo, perfil)
                return nombre_archivo
            except Exception as e:
                ultimo_error = e
                limpiar_temporales(nombre_archivo)

    raise ultimo_error or RuntimeError("Todas las URLs del TikTok Player fallaron")


def _opciones_tiktok_ytdlp(nombre_archivo, metodo):
    opciones = {
        "format": "bestvideo+bestaudio/best",
        "outtmpl": nombre_archivo,
        "merge_output_format": "mp4",
        "quiet": True,
        "noplaylist": True,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 2,
        "socket_timeout": 30,
    }

    if metodo in (1, 2):
        opciones["impersonate"] = "chrome"

    if metodo == 2 and os.path.exists(TIKTOK_COOKIEFILE):
        opciones["cookiefile"] = TIKTOK_COOKIEFILE

    if metodo == 3:
        iid = TIKTOK_IID or ""
        opciones["extractor_args"] = {
            "tiktok": {
                "app_info": [
                    f"{iid}/musical_ly/35.1.3/2023501030/1233",
                    f"{iid}/trill/35.1.3/2023501030/1180",
                ]
            }
        }
        if os.path.exists(TIKTOK_COOKIEFILE):
            opciones["cookiefile"] = TIKTOK_COOKIEFILE

    return opciones


def descargar_tiktok_ytdlp(url, nombre_archivo):
    ultimo_error = None
    for metodo in (1, 2, 3):
        try:
            limpiar_temporales(nombre_archivo)
            print(f"TikTok yt-dlp: método {metodo}", flush=True)
            with yt_dlp.YoutubeDL(_opciones_tiktok_ytdlp(nombre_archivo, metodo)) as ydl:
                ydl.download([url])
            if not os.path.exists(nombre_archivo):
                raise RuntimeError("yt-dlp terminó pero no creó el archivo")
            print(f"TikTok yt-dlp: método {metodo} funcionó", flush=True)
            return nombre_archivo
        except Exception as e:
            ultimo_error = e
            print(f"TikTok yt-dlp método {metodo} falló: {e}", flush=True)
            limpiar_temporales(nombre_archivo)
            time.sleep(1)

    raise ultimo_error or RuntimeError("TikTok falló en todos los métodos")


def descargar_tiktok(url, nombre_archivo):
    with cola_tiktok:
        try:
            return descargar_tiktok_player(url, nombre_archivo)
        except Exception as e:
            print(f"TikTok Player falló: {e}", flush=True)
            limpiar_temporales(nombre_archivo)

        print("TikTok: usando yt-dlp como respaldo", flush=True)
        return descargar_tiktok_ytdlp(url, nombre_archivo)



def _info_mp4(ruta):
    """Detalles reales del archivo para no confundir tamaño con calidad."""
    info = {"bytes": os.path.getsize(ruta), "duration": 0.0, "pixels": 0}
    try:
        from mutagen.mp4 import MP4
        info["duration"] = float(MP4(ruta).info.length or 0)
    except Exception:
        pass
    if shutil.which("ffprobe"):
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height", "-of", "json", ruta],
                check=True, capture_output=True, text=True, timeout=10,
            )
            streams = (json.loads(result.stdout).get("streams") or [])
            if streams:
                stream = streams[0]
                info["pixels"] = int(stream.get("width") or 0) * int(stream.get("height") or 0)
        except Exception:
            pass
    return info


def _enlaces_ssstik(html_resultado):
    """Extrae exclusivamente enlaces de descarga del HTML devuelto por SSSTik."""
    from html.parser import HTMLParser

    class Enlaces(HTMLParser):
        def __init__(self):
            super().__init__()
            self.enlaces = []
            self.actual = None

        def handle_starttag(self, tag, attrs):
            if tag != "a":
                return
            attrs = dict(attrs)
            href = attrs.get("href") or ""
            if href:
                self.actual = [href, attrs.get("class") or "", ""]

        def handle_data(self, data):
            if self.actual:
                self.actual[2] += data

        def handle_endtag(self, tag):
            if tag == "a" and self.actual:
                self.enlaces.append(self.actual)
                self.actual = None

    parser = Enlaces()
    parser.feed(html_resultado)
    candidates = []
    for href, classes, label in parser.enlaces:
        full_url = urljoin("https://ssstik.io/", html.unescape(href))
        parsed = urlsplit(full_url)
        host = (parsed.hostname or "").lower()
        allowed = (
            host in ("ssstik.io", "www.ssstik.io", "tikcdn.io", "www.tikcdn.io",
                     "ssscdn.io", "www.ssscdn.io")
            or host.endswith(".tiktokcdn.com")
            or host.endswith(".byteoversea.com")
        )
        if not allowed or parsed.scheme != "https":
            continue
        desc = (label + " " + classes + " " + full_url).lower()
        if any(s in desc for s in ("mp3", "audio", "music", "thumbnail", "image")):
            continue
        if not (
            "download" in desc or "dl" in desc
            or host in ("tikcdn.io", "www.tikcdn.io", "ssscdn.io", "www.ssscdn.io")
        ):
            continue
        hd = int(bool(re.search(r"\bhd\b|1080|full.hd|high.quality|dlhd", desc)))
        candidates.append((hd, full_url))
    return [u for _, u in sorted(set(candidates), reverse=True)]


def _descargar_ssstik_mejor(url, destino):
    """SSSTik es una fuente no oficial: cualquier fallo preserva el video actual."""
    print("TikTok: consultando SSSTik para comprobar calidad HD", flush=True)
    nombre_base, _ = os.path.splitext(destino)
    alternativa = nombre_base + "_ssstik.mp4"
    original = _info_mp4(destino)

    with cffi_requests.Session() as session:
        page = session.get(
            "https://ssstik.io/es", impersonate="chrome",
            headers={"User-Agent": BROWSER_UA}, timeout=18,
        )
        page.raise_for_status()
        token_match = re.search(
            r"""tt\s*:\s*['"]([A-Za-z0-9_-]+)['"]""", page.text,
        )
        if not token_match:
            raise RuntimeError("SSSTik cambió su formulario (sin token)")
        response = session.post(
            "https://ssstik.io/abc?url=dl",
            data={"id": url, "locale": "es", "tt": token_match.group(1)},
            headers={
                "User-Agent": BROWSER_UA,
                "Origin": "https://ssstik.io",
                "Referer": "https://ssstik.io/es",
                "HX-Request": "true",
                "HX-Current-URL": "https://ssstik.io/es",
                "HX-Target": "target",
                "HX-Trigger": "_gcaptcha_pt",
            },
            impersonate="chrome", timeout=25,
        )
        response.raise_for_status()
        links = _enlaces_ssstik(response.text)
        if not links:
            raise RuntimeError("SSSTik no devolvió enlaces de video")

        for media_url in links[:4]:
            limpiar_temporales(alternativa)
            try:
                with session.get(
                    media_url, impersonate="chrome", stream=True, timeout=75,
                    headers={"User-Agent": BROWSER_UA, "Referer": "https://ssstik.io/"},
                ) as stream:
                    stream.raise_for_status()
                    size = int(stream.headers.get("Content-Length") or 0)
                    if size > 120 * 1024 * 1024:
                        continue
                    total = 0
                    with open(alternativa, "wb") as file:
                        for block in stream.iter_content(chunk_size=1024 * 1024):
                            if not block:
                                continue
                            total += len(block)
                            if total > 120 * 1024 * 1024:
                                raise RuntimeError("Archivo de SSSTik demasiado grande")
                            file.write(block)
                if not _es_mp4_valido(alternativa):
                    raise RuntimeError("SSSTik no entregó un MP4 válido")
                other = _info_mp4(alternativa)
                print(
                    f"TikTok: comparación Player={original['bytes']} bytes; "
                    f"SSSTik={other['bytes']} bytes; "
                    f"duraciones={original['duration']:.1f}/{other['duration']:.1f}s",
                    flush=True,
                )
                if original["duration"] and other["duration"]:
                    if abs(original["duration"] - other["duration"]) > max(
                        2.0, original["duration"] * 0.05
                    ):
                        continue
                if original["pixels"] and other["pixels"]:
                    if other["pixels"] < original["pixels"]:
                        continue
                # El tamaño por sí solo no prueba calidad. Solo se reemplaza
                # cuando la diferencia es sustancial y el video corresponde.
                if other["bytes"] > original["bytes"] * 1.35:
                    os.replace(alternativa, destino)
                    print("TikTok: SSSTik entregó un MP4 mayor; se eligió esa fuente", flush=True)
                    return True
            except Exception as e:
                print(f"TikTok: enlace SSSTik no utilizable: {e}", flush=True)
            finally:
                limpiar_temporales(alternativa)
    return False


def _comprobar_calidad_alternativa(url, nombre_archivo, perfil_player):
    """Busca una versión superior sin comprometer el MP4 ya descargado."""
    pixels_player = int(perfil_player.get("pixels") or 0)
    bitrate_player = int(perfil_player.get("bitrate") or 0)

    # No aumentar peticiones cuando el Player ya ofrece una versión HD razonable.
    if pixels_player >= 1080 * 1920 and bitrate_player >= 1_200_000:
        return

    print(
        "TikTok: comprobando otra fuente por calidad insuficiente del Player",
        flush=True,
    )
    try:
        if _descargar_ssstik_mejor(url, nombre_archivo):
            return
    except Exception as e:
        print(f"TikTok: SSSTik no disponible: {e}", flush=True)

    base, _ = os.path.splitext(nombre_archivo)
    patron_alternativo = f"{base}_alternativa.%(ext)s"
    ruta_alternativa = f"{base}_alternativa.mp4"

    # Se mantienen los métodos ya existentes, sin cambiar las credenciales ni
    # el método estable del Player. Un error deja intacto el archivo original.
    for metodo in (1, 2, 3):
        opciones = _opciones_tiktok_ytdlp(nombre_archivo, metodo)
        opciones.update({
            "format": "best[ext=mp4][acodec!=none]",
            "format_sort": ["res", "br"],
            "format_sort_force": True,
            "outtmpl": patron_alternativo,
            "retries": 1,
            "fragment_retries": 1,
            "extractor_retries": 1,
            "socket_timeout": 15,
            "noprogress": True,
            "quiet": True,
        })

        try:
            with yt_dlp.YoutubeDL(opciones) as ydl:
                info = ydl.extract_info(url, download=False)
                if not info or info.get("_type") == "playlist":
                    continue
                # Solo se aceptan archivos con video y audio juntos, sin
                # recomprimir ni depender de FFmpeg.
                if info.get("requested_formats"):
                    continue

                width = int(info.get("width") or 0)
                height = int(info.get("height") or 0)
                pixels = width * height
                bitrate = int(float(info.get("tbr") or 0) * 1000)
                es_superior = (
                    pixels > pixels_player
                    or (
                        pixels and pixels == pixels_player
                        and bitrate_player and bitrate > bitrate_player * 1.3
                    )
                )
                print(
                    f"TikTok: alternativa {metodo} {width}x{height} "
                    f"{bitrate // 1000} kbps; superior={es_superior}",
                    flush=True,
                )
                if not es_superior:
                    continue

                ydl.process_info(info)
                if not _es_mp4_valido(ruta_alternativa):
                    raise RuntimeError("La fuente alternativa no entregó un MP4 válido")

                os.replace(ruta_alternativa, nombre_archivo)
                print(
                    f"TikTok: calidad mejorada a {width}x{height} "
                    f"{bitrate // 1000} kbps (yt-dlp método {metodo})",
                    flush=True,
                )
                return
        except Exception as e:
            print(f"TikTok: alternativa {metodo} no disponible: {e}", flush=True)
        finally:
            # Solo limpia temporales de la alternativa, nunca el Player original.
            limpiar_temporales(ruta_alternativa)

    print("TikTok: se conserva calidad original del Player", flush=True)


# ========================= INSTAGRAM =========================

def leer_cookies_instagram():
    if not os.path.exists(INSTAGRAM_COOKIEFILE):
        raise RuntimeError("Falta instagram_cookies.txt en Render")

    jar = MozillaCookieJar(INSTAGRAM_COOKIEFILE)
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except Exception as e:
        raise RuntimeError("instagram_cookies.txt debe estar en formato Netscape") from e

    cookies = {c.name: c.value for c in jar}
    if not cookies.get("sessionid"):
        raise RuntimeError("Las cookies de Instagram no contienen sessionid")
    return cookies


def crear_instaloader():
    cookies = leer_cookies_instagram()
    loader = instaloader.Instaloader(
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        compress_json=False,
        quiet=True,
        iphone_support=True,
        max_connection_attempts=2,
        request_timeout=45.0,
    )
    loader.context.load_session("cookie_session", cookies)
    if cookies.get("ds_user_id"):
        loader.context.user_id = cookies["ds_user_id"]

    print("Instagram: sesión cargada", flush=True)
    return loader, cookies


def descargar_directo(url, destino, cookies):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/18.0 Mobile/15E148 Safari/604.1"
        ),
        "Referer": "https://www.instagram.com/",
        "Accept-Language": "es-MX,es;q=0.9,en;q=0.8",
    }
    with requests.get(url, headers=headers, cookies=cookies, stream=True, timeout=90) as r:
        r.raise_for_status()
        with open(destino, "wb") as f:
            for bloque in r.iter_content(chunk_size=1024 * 1024):
                if bloque:
                    f.write(bloque)

    if not os.path.exists(destino) or os.path.getsize(destino) == 0:
        raise RuntimeError("Instagram devolvió un archivo vacío")
    return destino


def resolver_url_instagram(url):
    url = str(url or "").strip()
    lower = url.lower()

    if _es_historia_instagram(url):
        return url

    if any(x in lower for x in (
        "instagram.com/reel/",
        "instagram.com/reels/",
        "instagram.com/p/",
        "instagram.com/tv/",
        "instagr.am/reel/",
        "instagr.am/p/",
    )):
        return url

    try:
        r = cffi_requests.get(
            url,
            headers={"User-Agent": BROWSER_UA},
            impersonate="chrome",
            allow_redirects=True,
            timeout=20,
        )
        final_url = r.url or url
        print(f"Instagram: link resuelto a {final_url}", flush=True)
        return final_url
    except Exception as e:
        print(f"Instagram: no se pudo resolver el link compartido: {e}", flush=True)
        return url


def shortcode_instagram(url):
    m = re.search(r"(?:instagram\.com|instagr\.am)/(?:p|reel|reels|tv)/([^/?#]+)", url, re.I)
    return m.group(1) if m else None


def guardar_media(url, video, indice, directorio, cookies):
    ext = ".mp4" if video else ".jpg"
    tipo = "video" if video else "foto"
    ruta = os.path.join(directorio, f"instagram_{indice:02d}{ext}")
    descargar_directo(url, ruta, cookies)
    return {"ruta": ruta, "tipo": tipo}


def descargar_instagram(url, directorio):
    if _es_historia_instagram(url):
        raise RuntimeError("Historias de Instagram desactivadas temporalmente")

    url_resuelta = resolver_url_instagram(url)
    if _es_historia_instagram(url_resuelta):
        raise RuntimeError("Historias de Instagram desactivadas temporalmente")

    os.makedirs(directorio, exist_ok=True)
    loader = None
    try:
        loader, cookies = crear_instaloader()

        shortcode = shortcode_instagram(url_resuelta)
        if not shortcode:
            raise RuntimeError("No pude identificar la publicación")

        print(f"Instagram: cargando {shortcode}", flush=True)
        post = instaloader.Post.from_shortcode(loader.context, shortcode)
        archivos = []

        if post.typename == "GraphSidecar":
            nodos = list(post.get_sidecar_nodes())
            if not nodos:
                raise RuntimeError("Instagram devolvió un carrusel vacío")

            for i, nodo in enumerate(nodos, start=1):
                if nodo.is_video:
                    if not nodo.video_url:
                        raise RuntimeError(f"No pude obtener el video {i}")
                    archivos.append(
                        guardar_media(nodo.video_url, True, i, directorio, cookies)
                    )
                else:
                    if not nodo.display_url:
                        raise RuntimeError(f"No pude obtener la foto {i}")
                    archivos.append(
                        guardar_media(nodo.display_url, False, i, directorio, cookies)
                    )

        elif post.is_video:
            media_url = post.video_url
            if not media_url:
                raise RuntimeError("Instagram no devolvió el video")
            archivos.append(guardar_media(media_url, True, 1, directorio, cookies))

        else:
            media_url = post.url
            if not media_url:
                raise RuntimeError("Instagram no devolvió la foto")
            archivos.append(guardar_media(media_url, False, 1, directorio, cookies))

        if not archivos:
            raise RuntimeError("Instagram no devolvió archivos")

        print(f"Instagram: {len(archivos)} archivo(s) descargado(s)", flush=True)
        return archivos

    finally:
        if loader is not None:
            try:
                loader.close()
            except Exception:
                pass


def crear_zip(archivos, directorio):
    ruta = os.path.join(directorio, "instagram_media.zip")
    with zipfile.ZipFile(ruta, "w", compression=zipfile.ZIP_STORED) as z:
        for item in archivos:
            z.write(item["ruta"], arcname=os.path.basename(item["ruta"]))
    return ruta


def mimetype(ruta):
    ext = os.path.splitext(ruta)[1].lower()
    if ext == ".mp4":
        return "video/mp4"
    if ext in (".jpg", ".jpeg"):
        return "image/jpeg"
    if ext == ".zip":
        return "application/zip"
    return "application/octet-stream"


# ========================= API =========================

@app_web.route("/download", methods=["POST"])
def download_media():
    data = request.get_json(silent=True)
    if not data:
        return {"error": "No se recibió información"}, 400

    url = str(data.get("url") or "").strip()
    if not url:
        return {"error": "No URL"}, 400
    if not es_link_valido(url):
        return {"error": "Link no válido"}, 400

    if es_tiktok(url):
        archivo = f"{uuid.uuid4()}.mp4"
        try:
            descargar_tiktok(url, archivo)

            @after_this_request
            def borrar_tiktok(response):
                limpiar_temporales(archivo)
                return response

            return send_file(
                archivo,
                as_attachment=True,
                download_name="video.mp4",
                mimetype="video/mp4",
            )
        except Exception as e:
            print(f"Error /download TikTok: {e}", flush=True)
            limpiar_temporales(archivo)
            return {"error": str(e)}, 500

    if _es_historia_instagram(url):
        return {"error": "Historias de Instagram desactivadas temporalmente"}, 409

    directorio = f"ig_{uuid.uuid4().hex}"
    try:
        archivos = descargar_instagram(url, directorio)

        @after_this_request
        def borrar_instagram(response):
            limpiar_directorio(directorio)
            return response

        if len(archivos) == 1:
            item = archivos[0]
            nombre = "instagram.mp4" if item["tipo"] == "video" else "instagram.jpg"
            response = send_file(
                item["ruta"],
                as_attachment=True,
                download_name=nombre,
                mimetype=mimetype(item["ruta"]),
            )
            response.headers["X-Media-Count"] = "1"
            response.headers["X-Media-Type"] = item["tipo"]
            return response

        ruta_zip = crear_zip(archivos, directorio)
        response = send_file(
            ruta_zip,
            as_attachment=True,
            download_name="instagram_media.zip",
            mimetype="application/zip",
        )
        response.headers["X-Media-Count"] = str(len(archivos))
        response.headers["X-Media-Type"] = "carousel"
        return response

    except Exception as e:
        print(f"Error /download Instagram: {e}", flush=True)
        limpiar_directorio(directorio)
        return {"error": str(e)}, 500


# ========================= TELEGRAM =========================

async def enviar_instagram(update, archivos):
    total = len(archivos)
    for i, item in enumerate(archivos, start=1):
        caption = f"Instagram {i}/{total}" if total > 1 else "Instagram descargado"
        try:
            if item["tipo"] == "foto":
                with open(item["ruta"], "rb") as f:
                    await update.message.reply_photo(photo=f, caption=caption)
            else:
                with open(item["ruta"], "rb") as f:
                    await update.message.reply_video(
                        video=f,
                        caption=caption,
                        supports_streaming=True,
                    )
        except Exception as e:
            print(f"Telegram no pudo enviar como {item['tipo']}: {e}", flush=True)
            with open(item["ruta"], "rb") as f:
                await update.message.reply_document(document=f, caption=caption)


async def procesar_enlace(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    url = update.message.text.strip()
    if not es_link_valido(url):
        await update.message.reply_text("Mándame un link válido de TikTok o Instagram.")
        return

    if _es_historia_instagram(url):
        await update.message.reply_text("Historias de Instagram desactivadas temporalmente.")
        return

    async with cola_descargas:
        esperando = await update.message.reply_text("Descargando...")

        if es_tiktok(url):
            archivo = f"{uuid.uuid4()}.mp4"
            try:
                await asyncio.to_thread(descargar_tiktok, url, archivo)
                with open(archivo, "rb") as f:
                    await update.message.reply_video(
                        video=f,
                        caption="Video descargado",
                        supports_streaming=True,
                    )
                await esperando.delete()
            except Exception as e:
                print(f"Error Telegram TikTok: {e}", flush=True)
                try:
                    await esperando.edit_text("No pude descargar ese TikTok.")
                except Exception:
                    pass
            finally:
                limpiar_temporales(archivo)
            return

        directorio = f"ig_{uuid.uuid4().hex}"
        try:
            archivos = await asyncio.to_thread(descargar_instagram, url, directorio)
            await enviar_instagram(update, archivos)
            await esperando.delete()
        except Exception as e:
            print(f"Error Telegram Instagram: {e}", flush=True)
            try:
                await esperando.edit_text("No pude descargar ese contenido de Instagram.")
            except Exception:
                pass
        finally:
            limpiar_directorio(directorio)


def iniciar_web():
    puerto = int(os.environ.get("PORT", 8080))
    app_web.run(host="0.0.0.0", port=puerto, threaded=True, use_reloader=False)


def main():
    if not TOKEN:
        raise RuntimeError("Falta la variable BOT_TOKEN")

    app = Application.builder().token(TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, procesar_enlace))
    print("Bot encendido...", flush=True)
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    threading.Thread(target=iniciar_web, daemon=True).start()
    main()