from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters

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
from flask import Flask, after_this_request, request, send_file

TOKEN = os.getenv("BOT_TOKEN")
TIKTOK_IID = os.getenv("TIKTOK_IID")
TIKTOK_COOKIEFILE = "cookies.txt"
INSTAGRAM_COOKIEFILE = "instagram_cookies.txt"

app_web = Flask(__name__)
cola_descargas = asyncio.Lock()


@app_web.route("/")
def home():
    return {"status": "ok", "service": "telegram-tiktok-bot"}, 200


@app_web.route("/healthz")
def healthz():
    return {"status": "ok"}, 200


def es_tiktok(url):
    url = url.lower()
    return any(x in url for x in (
        "tiktok.com", "vt.tiktok.com", "vm.tiktok.com", "tiktokv.com"
    ))


def es_instagram(url):
    url = url.lower()
    return any(x in url for x in (
        "instagram.com/reel/",
        "instagram.com/reels/",
        "instagram.com/p/",
        "instagram.com/tv/",
        "instagram.com/stories/",
    ))


def es_story(url):
    return "instagram.com/stories/" in url.lower()


def es_link_valido(url):
    return es_tiktok(url) or es_instagram(url)


def limpiar_temporales(nombre_archivo):
    base = os.path.splitext(nombre_archivo)[0]
    for archivo in glob.glob(f"{base}*"):
        try:
            if os.path.isfile(archivo):
                os.remove(archivo)
        except Exception as e:
            print(f"No se pudo borrar {archivo}: {e}")


def limpiar_directorio(ruta):
    if ruta and os.path.isdir(ruta):
        shutil.rmtree(ruta, ignore_errors=True)


# ========================= TIKTOK =========================

def opciones_tiktok(nombre_archivo, metodo=1):
    opciones = {
        "format": "bestvideo+bestaudio/best",
        "outtmpl": nombre_archivo,
        "merge_output_format": "mp4",
        "quiet": True,
        "noplaylist": True,
        "retries": 5,
        "fragment_retries": 5,
        "extractor_retries": 3,
        "socket_timeout": 30,
    }

    if metodo == 1:
        opciones["cookiefile"] = TIKTOK_COOKIEFILE
    elif metodo == 2:
        pass
    elif metodo == 3:
        opciones["cookiefile"] = TIKTOK_COOKIEFILE
        opciones["http_headers"] = {
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_6 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                "Version/17.6 Mobile/15E148 Safari/604.1"
            ),
            "Referer": "https://www.tiktok.com/",
            "Accept-Language": "es-MX,es;q=0.9,en;q=0.8",
        }
    elif metodo == 4:
        if not TIKTOK_IID:
            raise RuntimeError("No existe TIKTOK_IID")
        opciones["cookiefile"] = TIKTOK_COOKIEFILE
        opciones["extractor_args"] = {"tiktok": {"app_info": [TIKTOK_IID]}}

    return opciones


def descargar_tiktok(url, nombre_archivo):
    ultimo_error = None
    metodos = [1, 2, 3]
    if TIKTOK_IID:
        metodos.append(4)

    for metodo in metodos:
        try:
            limpiar_temporales(nombre_archivo)
            print(f"TikTok: intentando método {metodo}")
            with yt_dlp.YoutubeDL(opciones_tiktok(nombre_archivo, metodo)) as ydl:
                ydl.download([url])
            if not os.path.exists(nombre_archivo):
                raise RuntimeError("yt-dlp terminó pero no creó el archivo")
            print(f"TikTok: método {metodo} funcionó")
            return nombre_archivo
        except Exception as e:
            ultimo_error = e
            print(f"TikTok método {metodo} falló: {e}")
            limpiar_temporales(nombre_archivo)
            time.sleep(2)

    raise ultimo_error or RuntimeError("TikTok falló en todos los métodos")


# ======================== INSTAGRAM =======================

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
        max_connection_attempts=3,
        request_timeout=60.0,
    )
    loader.context.load_session("cookie_session", cookies)
    if cookies.get("ds_user_id"):
        loader.context.user_id = cookies["ds_user_id"]

    try:
        usuario = loader.test_login()
        if usuario:
            loader.context.username = usuario
            print(f"Instagram: sesión válida como @{usuario}")
        else:
            print("Instagram: sesión cargada")
    except Exception as e:
        print(f"Instagram: no se pudo verificar la sesión: {e}")

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


def shortcode_instagram(url):
    m = re.search(r"instagram\.com/(?:p|reel|reels|tv)/([^/?#]+)", url, re.I)
    return m.group(1) if m else None


def story_mediaid(url):
    m = re.search(r"instagram\.com/stories/[^/?#]+/(\d+)", url, re.I)
    return int(m.group(1)) if m else None


def guardar_media(url, video, indice, directorio, cookies):
    ext = ".mp4" if video else ".jpg"
    tipo = "video" if video else "foto"
    ruta = os.path.join(directorio, f"instagram_{indice:02d}{ext}")
    descargar_directo(url, ruta, cookies)
    return {"ruta": ruta, "tipo": tipo}


def descargar_instagram(url, directorio):
    os.makedirs(directorio, exist_ok=True)
    loader = None
    try:
        loader, cookies = crear_instaloader()
        archivos = []

        if es_story(url):
            media_id = story_mediaid(url)
            if not media_id:
                raise RuntimeError("No pude identificar la historia")

            print(f"Instagram Story: media_id {media_id}")
            item = instaloader.StoryItem.from_mediaid(loader.context, media_id)
            if item.is_video:
                media_url = item.video_url
                if not media_url:
                    raise RuntimeError("Instagram no devolvió el video de la historia")
                archivos.append(guardar_media(media_url, True, 1, directorio, cookies))
            else:
                media_url = item.url
                if not media_url:
                    raise RuntimeError("Instagram no devolvió la foto de la historia")
                archivos.append(guardar_media(media_url, False, 1, directorio, cookies))
            return archivos

        shortcode = shortcode_instagram(url)
        if not shortcode:
            raise RuntimeError("No pude identificar la publicación")

        print(f"Instagram: cargando {shortcode}")
        post = instaloader.Post.from_shortcode(loader.context, shortcode)

        if post.typename == "GraphSidecar":
            nodos = list(post.get_sidecar_nodes())
            if not nodos:
                raise RuntimeError("Instagram devolvió un carrusel vacío")

            for i, nodo in enumerate(nodos, start=1):
                if nodo.is_video:
                    if not nodo.video_url:
                        raise RuntimeError(f"No pude obtener el video {i}")
                    archivos.append(guardar_media(nodo.video_url, True, i, directorio, cookies))
                else:
                    if not nodo.display_url:
                        raise RuntimeError(f"No pude obtener la foto {i}")
                    archivos.append(guardar_media(nodo.display_url, False, i, directorio, cookies))

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

        print(f"Instagram: {len(archivos)} archivo(s) descargado(s)")
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


# ========================== API ===========================

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
            print(f"Error /download TikTok: {e}")
            limpiar_temporales(archivo)
            return {"error": str(e)}, 500

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
        print(f"Error /download Instagram: {e}")
        limpiar_directorio(directorio)
        return {"error": str(e)}, 500


# ======================== TELEGRAM ========================

async def enviar_instagram(update, archivos):
    total = len(archivos)
    for i, item in enumerate(archivos, start=1):
        caption = f"✅ Instagram {i}/{total}" if total > 1 else "✅ Instagram descargado"
        try:
            if item["tipo"] == "foto":
                with open(item["ruta"], "rb") as f:
                    await update.message.reply_photo(photo=f, caption=caption)
            else:
                with open(item["ruta"], "rb") as f:
                    await update.message.reply_video(video=f, caption=caption, supports_streaming=True)
        except Exception as e:
            print(f"Telegram no pudo enviar como {item['tipo']}: {e}")
            with open(item["ruta"], "rb") as f:
                await update.message.reply_document(document=f, caption=caption)


async def procesar_enlace(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    url = update.message.text.strip()
    if not es_link_valido(url):
        await update.message.reply_text("📎 Mándame un link válido de TikTok o Instagram.")
        return

    async with cola_descargas:
        esperando = await update.message.reply_text("⏳ Descargando...")

        if es_tiktok(url):
            archivo = f"{uuid.uuid4()}.mp4"
            try:
                await asyncio.to_thread(descargar_tiktok, url, archivo)
                with open(archivo, "rb") as f:
                    await update.message.reply_video(video=f, caption="✅ Video descargado")
                await esperando.delete()
            except Exception as e:
                print(f"Error Telegram TikTok: {e}")
                try:
                    await esperando.edit_text("❌ No pude descargar ese TikTok.")
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
            print(f"Error Telegram Instagram: {e}")
            try:
                await esperando.edit_text("❌ No pude descargar ese contenido de Instagram.")
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
    print("Bot encendido...")
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    threading.Thread(target=iniciar_web, daemon=True).start()
    main()
