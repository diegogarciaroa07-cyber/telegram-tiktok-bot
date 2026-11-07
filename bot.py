from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

import asyncio
import html
import json
import struct
from contextlib import contextmanager
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
from yt_dlp.networking.impersonate import ImpersonateTarget
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
        if codec in ("bytevc2", "h266", "vvc"):
            continue
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

    # Orden de búsqueda inicial. La selección final mide los archivos.
    # No se limita a H.264; HEVC/H.265 se conserva cuando TikTok lo ofrece
    # en una calidad superior.
    perfiles.sort(
        key=lambda p: (p["pixels"], p["bitrate"]),
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
    try:
        r.raise_for_status()
        with open(destino, "wb") as f:
            for bloque in r.iter_content(chunk_size=1024 * 1024):
                if bloque:
                    f.write(bloque)
    finally:
        r.close()

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
                _info_mp4(nombre_archivo)
                return nombre_archivo
            except Exception as e:
                ultimo_error = e
                limpiar_temporales(nombre_archivo)

    raise ultimo_error or RuntimeError("Todas las URLs del TikTok Player fallaron")


def _opciones_tiktok_ytdlp(nombre_archivo, metodo):
    opciones = {
        "format": "bestvideo[ext=mp4][vcodec!=bytevc2]+bestaudio/best[ext=mp4][vcodec!=bytevc2]",
        "format_sort": ["res", "fps", "br"],
        "format_sort_force": True,
        "outtmpl": nombre_archivo,
        "merge_output_format": "mp4",
        "quiet": True,
        "noplaylist": True,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 2,
        "socket_timeout": 30,
        "noprogress": True,
        "fixup": "never",
    }

    if metodo in (1, 2):
        opciones["impersonate"] = ImpersonateTarget.from_str("chrome")

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
    return opciones


def _tiktok_ffmpeg():
    if shutil.which("ffmpeg"):
        return shutil.which("ffmpeg")
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError):
        return None


@contextmanager
def _abrir_tiktok_ytdlp(opciones, metodo):
    with yt_dlp.YoutubeDL(opciones) as ydl:
        # Cargar en memoria: YoutubeDL.close() no debe reescribir cookies.txt.
        if metodo in (2, 3) and os.path.exists(TIKTOK_COOKIEFILE):
            jar = MozillaCookieJar(TIKTOK_COOKIEFILE)
            jar.load(ignore_discard=True, ignore_expires=True)
            for cookie in jar:
                ydl.cookiejar.set_cookie(cookie)
        yield ydl


def descargar_tiktok_ytdlp(url, nombre_archivo):
    ultimo_error = None
    funciono = False
    base = os.path.splitext(nombre_archivo)[0]
    for metodo in (1, 2, 3):
        alternativa = f"{base}_ytdlp_{metodo}.mp4"
        try:
            print(f"TikTok yt-dlp: método {metodo}", flush=True)
            opciones = _opciones_tiktok_ytdlp(alternativa, metodo)
            ffmpeg = _tiktok_ffmpeg()
            if ffmpeg:
                # El merger de yt-dlp usa stream copy, nunca un codificador.
                opciones["ffmpeg_location"] = ffmpeg
            else:
                # MP4 con audio incluido si no hay un muxer instalado. No
                # transcodificar ni seleccionar video sin audio por accidente.
                opciones["format"] = "best[ext=mp4][vcodec!=bytevc2][acodec!=none]/best[ext=mp4][vcodec!=bytevc2]"
            with _abrir_tiktok_ytdlp(opciones, metodo) as ydl:
                info = ydl.extract_info(url, download=True)
                if not info or info.get("_type") == "playlist":
                    raise RuntimeError("La respuesta no es un video individual")
            _tiktok_conservar_candidata(alternativa, nombre_archivo, f"yt-dlp/{metodo}")
            funciono = True
        except Exception as e:
            ultimo_error = e
            print(f"TikTok yt-dlp método {metodo} falló: {type(e).__name__}: {e}", flush=True)
        finally:
            limpiar_temporales(alternativa)
    if funciono:
        return nombre_archivo
    raise ultimo_error or RuntimeError("TikTok falló en todos los métodos")


def descargar_tiktok(url, nombre_archivo):
    with cola_tiktok:
        # Cada fuente usa archivos separados. Un fallo posterior conserva la
        # mejor descarga válida y nunca borra el archivo que se va a entregar.
        limpiar_temporales(nombre_archivo)
        errores = []
        try:
            descargar_tiktok_player(url, nombre_archivo)
        except Exception as e:
            print(f"TikTok Player falló: {type(e).__name__}: {e}", flush=True)
            limpiar_temporales(nombre_archivo)
            errores.append("Player no disponible")
        errores.extend(_comprobar_calidad_alternativa(url, nombre_archivo, None))
        if not os.path.exists(nombre_archivo):
            raise RuntimeError("Ninguna fuente entregó un video válido: " + "; ".join(errores))
        info = _info_mp4(nombre_archivo)
        if os.path.exists(_tiktok_ruta_calidad(nombre_archivo)):
            info["source"] = _tiktok_calidad(nombre_archivo).get("source", "Player")
        else:
            info["source"] = "Player"
        info["status"] = "best-accessible"
        info["warnings"] = errores
        if min(info["width"], info["height"]) < 1080:
            info["warnings"].append("No se encontró una versión Full HD accesible")
        with open(_tiktok_ruta_calidad(nombre_archivo), "w", encoding="utf-8") as file:
            json.dump(info, file, ensure_ascii=False)
        print(f"TikTok: mejor accesible {info['width']}x{info['height']}; sin recodificar", flush=True)
        return nombre_archivo



def _info_mp4(ruta):
    """Mide las pistas, no el peso del contenedor. No requiere FFmpeg en Render.

    Lee tablas ISO BMFF de MP4/MOV no fragmentados (los archivos de TikTok).
    Un archivo truncado, cifrado o sin tablas verificables se rechaza; nunca se
    sustituye un video válido por HTML o por metadatos estimados del servidor.
    """
    def cajas(datos, inicio=0, fin=None):
        fin = len(datos) if fin is None else fin
        while inicio < fin:
            if fin - inicio < 8:
                raise ValueError("Cabecera MP4 incompleta")
            size, tipo = struct.unpack_from(">I4s", datos, inicio)
            head = 8
            if size == 1:
                if fin - inicio < 16:
                    raise ValueError("Cabecera MP4 extendida incompleta")
                size = struct.unpack_from(">Q", datos, inicio + 8)[0]
                head = 16
            elif size == 0:
                size = fin - inicio
            if size < head or inicio + size > fin:
                raise ValueError("Caja MP4 truncada")
            yield tipo, datos[inicio + head:inicio + size]
            inicio += size

    size = os.path.getsize(ruta)
    moov = None
    media_bytes = 0
    with open(ruta, "rb") as file:
        pos = 0
        while pos < size:
            file.seek(pos)
            header = file.read(8)
            if len(header) != 8:
                raise ValueError("MP4 incompleto")
            length, tipo = struct.unpack(">I4s", header)
            head = 8
            if length == 1:
                extended = file.read(8)
                if len(extended) != 8:
                    raise ValueError("MP4 incompleto")
                length = struct.unpack(">Q", extended)[0]
                head = 16
            elif length == 0:
                length = size - pos
            if length < head or pos + length > size:
                raise ValueError("MP4 truncado")
            if tipo == b"moov":
                if length > 64 * 1024 * 1024:
                    raise ValueError("Tablas MP4 excesivas")
                moov = file.read(length - head)
            elif tipo == b"mdat":
                media_bytes += length - head
            pos += length
    if moov is None or not media_bytes:
        raise ValueError("Faltan pistas o contenido MP4")

    tracks = []
    for tipo, trak in cajas(moov):
        if tipo != b"trak":
            continue
        mdia = dict(cajas(trak)).get(b"mdia")
        if mdia is None:
            continue
        fields = dict(cajas(mdia))
        handler = fields.get(b"hdlr", b"")[8:12]
        if handler not in (b"vide", b"soun"):
            continue
        mdhd = fields.get(b"mdhd", b"")
        version = mdhd[0]
        offset = 20 if version == 1 else 12
        timescale = struct.unpack_from(">I", mdhd, offset)[0]
        duration = struct.unpack_from(">Q" if version == 1 else ">I", mdhd, offset + 4)[0]
        seconds = duration / timescale if timescale else 0
        minf = dict(cajas(fields[b"minf"]))
        table = dict(cajas(minf[b"stbl"]))
        descriptions = list(cajas(table[b"stsd"], 8))
        codec, description = descriptions[0]
        sizes = table[b"stsz"]
        sample_size, count = struct.unpack_from(">II", sizes, 4)
        # stts mide el tiempo de las muestras; mdhd puede estar redondeado.
        timing = table.get(b"stts")
        if timing is not None:
            entries = struct.unpack_from(">I", timing, 4)[0]
            if len(timing) != 8 + entries * 8:
                raise ValueError("Tabla temporal MP4 incompleta")
            samples = ticks = 0
            for n, delta in struct.iter_unpack(">II", timing[8:]):
                samples += n
                ticks += n * delta
            if samples != count:
                raise ValueError("Número de muestras MP4 inconsistente")
            seconds = ticks / timescale if timescale else 0
        if not count or not seconds:
            raise ValueError("Pista MP4 vacía o fragmentada")
        if sample_size:
            total = sample_size * count
        else:
            if len(sizes) != 12 + count * 4:
                raise ValueError("Tabla de muestras incompleta")
            total = sum(n[0] for n in struct.iter_unpack(">I", sizes[12:]))
        track = {
            "type": handler, "codec": codec.decode("ascii", errors="replace"),
            "duration": seconds, "bitrate": int(total * 8 / seconds),
            "sample_bytes": total,
        }
        if handler == b"vide":
            track["width"], track["height"] = struct.unpack_from(">HH", description, 24)
            track["fps"] = count / seconds
        tracks.append(track)
    videos = [t for t in tracks if t["type"] == b"vide"]
    if not videos or sum(t["sample_bytes"] for t in tracks) > media_bytes:
        raise ValueError("MP4 sin video completo")
    video = videos[0]
    if video["codec"] not in ("avc1", "avc3", "hvc1", "hev1", "av01", "vp09"):
        raise ValueError(f"Códec de video no compatible: {video['codec']}")
    width, height = video["width"], video["height"]
    if not width or not height:
        raise ValueError("Dimensiones MP4 inválidas")
    return {
        "bytes": size, "duration": video["duration"],
        "width": width, "height": height, "pixels": width * height,
        "codec": video["codec"], "fps": round(video["fps"], 3),
        "video_bitrate": video["bitrate"],
        "has_audio": any(t["type"] == b"soun" for t in tracks),
    }


def _tiktok_es_mejor(actual, candidata):
    """Selección técnica orientativa; ningún bitrate certifica calidad visual.

    Resolución y fps primero; bitrate de VIDEO como desempate. Entre códecs
    distintos el desempate es una heurística, no una equivalencia perceptual.
    El tamaño total (audio, carátulas y padding incluidos) nunca decide.
    """
    if actual is None:
        return True
    if abs(actual["duration"] - candidata["duration"]) > max(0.5, actual["duration"] * 0.02):
        return False
    ratio_actual = actual["width"] / actual["height"]
    ratio_candidata = candidata["width"] / candidata["height"]
    if abs(ratio_actual / ratio_candidata - 1) > 0.03:
        return False
    if actual["has_audio"] and not candidata["has_audio"]:
        return False
    # Agrupar fps cercanos evita preferir 30.001 fps frente a 30 por redondeos.
    key = lambda i: (i["pixels"], round(i["fps"]), i["video_bitrate"])
    return key(candidata) > key(actual)


def _tiktok_conservar_candidata(ruta, destino, fuente):
    nueva = _info_mp4(ruta)
    actual = _info_mp4(destino) if os.path.exists(destino) else None
    elegida = _tiktok_es_mejor(actual, nueva)
    print(
        f"TikTok {fuente}: {nueva['width']}x{nueva['height']} {nueva['codec']} "
        f"{nueva['fps']:g} fps {nueva['video_bitrate'] // 1000} kbps video; "
        f"{nueva['duration']:.2f}s; elegida={elegida}", flush=True,
    )
    if elegida:
        os.replace(ruta, destino)
        nueva["source"] = fuente
        with open(_tiktok_ruta_calidad(destino), "w", encoding="utf-8") as file:
            json.dump(nueva, file)
    return elegida


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
            if href and href != "#":
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
        if parsed.path in ("/", "/en", "/es"):
            continue
        if not (
            "download" in desc or "dl" in desc or "without_watermark" in desc
            or host in ("tikcdn.io", "www.tikcdn.io", "ssscdn.io", "www.ssscdn.io")
        ):
            continue
        hd = int(bool(re.search(r"\bhd\b|1080|full.hd|high.quality|dlhd", desc)))
        candidates.append((hd, full_url))
    return [u for _, u in sorted(set(candidates), reverse=True)]


def _ssstik_formulario_hd(html_resultado):
    """El botón HD no es un href: solicita su data-directurl mediante POST."""
    from html.parser import HTMLParser

    class Formulario(HTMLParser):
        def __init__(self):
            super().__init__()
            self.endpoint = None
            self.token = None

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "input" and attrs.get("name") == "tt":
                self.token = attrs.get("value")
            if tag in ("a", "button") and (
                attrs.get("id") == "hd_download"
                or "quality-best" in (attrs.get("class") or "").split()
            ):
                endpoint = attrs.get("data-directurl") or attrs.get("hx-post")
                if endpoint:
                    endpoint = urljoin("https://ssstik.io/", endpoint)
                    parsed = urlsplit(endpoint)
                    if parsed.scheme == "https" and parsed.hostname in ("ssstik.io", "www.ssstik.io"):
                        self.endpoint = endpoint

    form = Formulario()
    form.feed(html_resultado)
    return form.endpoint, form.token


def _ssstik_url_media_permitida(url):
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and (
        host in ("tikcdn.io", "www.tikcdn.io", "ssscdn.io", "www.ssscdn.io")
        or host.endswith((".tiktokcdn.com", ".byteoversea.com", ".tokcdn.com"))
    )


def _descargar_ssstik_mejor(url, destino):
    """SSSTik es una fuente no oficial: cualquier fallo preserva el video actual."""
    print("TikTok: consultando SSSTik para comprobar calidad HD", flush=True)
    nombre_base, _ = os.path.splitext(destino)
    alternativa = nombre_base + "_ssstik.mp4"
    mejoro = False
    validos = 0
    hd_accesible = False
    urls_hd = set()

    with cffi_requests.Session() as session:
        token = None
        endpoint = "abc"
        pagina_usada = None
        # SSSTik cambió el bootstrap: en versiones actuales publica
        # s_tt y s_furl en lugar del antiguo campo "tt:".
        for path in ("/en", "/", "/es"):
            try:
                page = session.get(
                    "https://ssstik.io" + path,
                    impersonate="chrome",
                    headers={
                        "User-Agent": BROWSER_UA,
                        "Accept-Language": "en-US,en;q=0.9",
                        "Referer": "https://ssstik.io/",
                    },
                    timeout=15,
                )
                page.raise_for_status()
                token_match = re.search(
                    r"""(?:s_tt\s*=\s*|tt\s*:\s*)['"]([A-Za-z0-9_-]+)['"]""",
                    page.text,
                )
                print(
                    f"TikTok SSSTik: página {path} HTTP {page.status_code}, "
                    f"HTML {len(page.text)} bytes, token={bool(token_match)}",
                    flush=True,
                )
                if token_match:
                    token = token_match.group(1)
                    endpoint_match = re.search(
                        r"""s_furl\s*=\s*['"]([A-Za-z0-9_/-]+)['"]""",
                        page.text,
                    )
                    if endpoint_match:
                        endpoint = endpoint_match.group(1).strip("/")
                    pagina_usada = path
                    break
            except Exception as e:
                print(f"TikTok SSSTik: página {path} no disponible: {e}", flush=True)

        if not token:
            raise RuntimeError(
                "SSSTik no entregó el token actual de su formulario desde Render"
            )

        response = session.post(
            "https://ssstik.io/" + endpoint + "?url=dl",
            data={"id": url, "locale": "en", "tt": token},
            headers={
                "User-Agent": BROWSER_UA,
                "Origin": "https://ssstik.io",
                "Referer": "https://ssstik.io" + pagina_usada,
                "HX-Request": "true",
                "HX-Current-URL": "https://ssstik.io" + pagina_usada,
                "HX-Target": "target",
                "HX-Trigger": "_gcaptcha_pt",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            impersonate="chrome", timeout=25,
        )
        response.raise_for_status()
        links = _enlaces_ssstik(response.text)
        endpoint_hd, token_hd = _ssstik_formulario_hd(response.text)
        hd_resuelto = False
        if endpoint_hd and token_hd:
            headers_hd = {
                "User-Agent": BROWSER_UA,
                "Origin": "https://ssstik.io",
                "Referer": "https://ssstik.io" + pagina_usada,
                "HX-Request": "true",
                "HX-Current-URL": "https://ssstik.io" + pagina_usada,
                "HX-Target": "hd_download",
                "HX-Trigger": "hd_download",
            }
            try:
                for intento in range(3):
                    hd = session.post(
                        endpoint_hd, data={"tt": token_hd}, headers=headers_hd,
                        impersonate="chrome", timeout=60,
                    )
                    hd.raise_for_status()
                    # HTMX comunica la URL en una cabecera, no en el HTML.
                    redirect = hd.headers.get("HX-Redirect")
                    enlaces_hd = _enlaces_ssstik(hd.text)
                    if redirect and _ssstik_url_media_permitida(redirect):
                        enlaces_hd.insert(0, redirect)
                    if enlaces_hd:
                        urls_hd.update(enlaces_hd)
                        links = enlaces_hd + links
                        hd_resuelto = True
                        break
                    evento = hd.headers.get("HX-Trigger", "").lower()
                    if any(e in evento for e in ("fail", "fakehd", "limit")):
                        break
                    if intento < 2:
                        time.sleep(2)
            except Exception as e:
                print(f"TikTok SSSTik HD: {type(e).__name__}: {e}", flush=True)
        if not hd_resuelto:
            print("TikTok SSSTik: HD no accesible; se comprobarán los enlaces normales", flush=True)
        if not links:
            print(
                f"TikTok SSSTik: respuesta sin videos, "
                f"HTTP {response.status_code}, HTML {len(response.text)} bytes",
                flush=True,
            )
            raise RuntimeError("SSSTik no devolvió enlaces de video")

        for media_url in dict.fromkeys(links):
            limpiar_temporales(alternativa)
            try:
                # curl_cffi Response no admite "with": cerrarla explícitamente.
                stream = session.get(
                    media_url, impersonate="chrome", stream=True, timeout=180,
                    headers={"User-Agent": BROWSER_UA, "Referer": "https://ssstik.io/"},
                )
                try:
                    stream.raise_for_status()
                    with open(alternativa, "wb") as file:
                        for block in stream.iter_content(chunk_size=1024 * 1024):
                            if not block:
                                continue
                            file.write(block)
                finally:
                    stream.close()
                if not _es_mp4_valido(alternativa):
                    raise RuntimeError("SSSTik no entregó un MP4 válido")
                elegida = _tiktok_conservar_candidata(alternativa, destino, "SSSTik")
                validos += 1
                hd_accesible = hd_accesible or media_url in urls_hd
                mejoro = mejoro or elegida
            except Exception as e:
                print(f"TikTok: enlace SSSTik no utilizable: {e}", flush=True)
            finally:
                limpiar_temporales(alternativa)
    if not validos:
        raise RuntimeError("SSSTik no entregó ningún video verificable")
    return {"mejoro": mejoro, "hd_accesible": hd_accesible}


def _comprobar_calidad_alternativa(url, nombre_archivo, perfil_player=None):
    """Consultar siempre todas las fuentes; 1080p no es un techo de calidad."""
    errores = []
    if not _tiktok_ffmpeg():
        errores.append("FFmpeg no disponible para combinar pistas separadas")
    try:
        estado = _descargar_ssstik_mejor(url, nombre_archivo)
        if not estado["hd_accesible"]:
            errores.append("SSSTik HD no disponible")
    except Exception as e:
        print(f"TikTok SSSTik: {type(e).__name__}: {e}", flush=True)
        errores.append("SSSTik no disponible")
    try:
        descargar_tiktok_ytdlp(url, nombre_archivo)
    except Exception as e:
        print(f"TikTok yt-dlp: {type(e).__name__}: {e}", flush=True)
        errores.append("yt-dlp no disponible")
    return errores


def _tiktok_ruta_calidad(ruta):
    return os.path.splitext(ruta)[0] + "_calidad.json"


def _tiktok_calidad(ruta):
    with open(_tiktok_ruta_calidad(ruta), encoding="utf-8") as file:
        return json.load(file)


def _tiktok_caption(ruta):
    info = _tiktok_calidad(ruta)
    texto = (
        f"Video descargado: {info['width']} × {info['height']} · "
        f"{info['codec']} · {info['fps']:g} fps\n"
        f"Video: {info['video_bitrate'] // 1000} kbps. Sin recodificar."
    )
    if min(info["width"], info["height"]) < 1080:
        texto += "\nMejor versión accesible; no se encontró Full HD."
    if info["warnings"]:
        texto += "\nAlgunas fuentes no ofrecieron una versión superior verificable."
    return texto


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

            info = _tiktok_calidad(archivo)
            response = send_file(
                archivo,
                as_attachment=True,
                download_name="video.mp4",
                mimetype="video/mp4",
            )
            response.headers["X-Video-Width"] = str(info["width"])
            response.headers["X-Video-Height"] = str(info["height"])
            response.headers["X-Video-Codec"] = info["codec"]
            response.headers["X-Video-Bitrate"] = str(info["video_bitrate"])
            response.headers["X-Video-Quality"] = info["status"]
            response.headers["X-Video-Limited"] = str(bool(info["warnings"])).lower()
            # Borrar después de cerrar el stream, también en Windows.
            response.direct_passthrough = False
            response.call_on_close(lambda: limpiar_temporales(archivo))
            return response
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
            descarga_lista = False
            try:
                await asyncio.to_thread(descargar_tiktok, url, archivo)
                descarga_lista = True
                caption = _tiktok_caption(archivo)
                try:
                    with open(archivo, "rb") as f:
                        await update.message.reply_video(
                            video=f, caption=caption, supports_streaming=True,
                            read_timeout=120, write_timeout=180,
                        )
                except Exception:
                    # Conservar HEVC y otros originales que Telegram no pueda
                    # presentar como video; enviar el archivo sin convertirlo.
                    with open(archivo, "rb") as f:
                        await update.message.reply_document(
                            document=f, caption=caption, filename="video.mp4",
                            disable_content_type_detection=True,
                            read_timeout=120, write_timeout=180,
                        )
                try:
                    await esperando.delete()
                except Exception:
                    pass
            except Exception as e:
                print(f"Error Telegram TikTok: {e}", flush=True)
                try:
                    mensaje = (
                        "El video se descargó, pero Telegram no pudo enviarlo. "
                        "Prueba el mismo enlace con el Atajo de iPhone; se conserva la calidad."
                        if descarga_lista else "No pude descargar ese TikTok."
                    )
                    await esperando.edit_text(mensaje)
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
