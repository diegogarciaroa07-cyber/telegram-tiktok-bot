from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

import asyncio
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