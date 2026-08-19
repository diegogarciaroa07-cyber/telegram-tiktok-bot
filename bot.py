from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters

import yt_dlp
import os
import uuid
import time
import glob
import threading
import asyncio

from flask import Flask, request, send_file, after_this_request


# =========================================================
# CONFIGURACIÓN
# =========================================================

TOKEN = os.getenv("BOT_TOKEN")

# Opcional.
# NO inventes este valor.
# Solo se usa si algún día tienes un IID real de TikTok.
TIKTOK_IID = os.getenv("TIKTOK_IID")

app_web = Flask(__name__)

# Cola para evitar que varias descargas de Telegram
# se pisen al mismo tiempo.
cola_descargas = asyncio.Lock()


# =========================================================
# RUTAS DE ESTADO PARA RENDER
# =========================================================

@app_web.route("/")
def home():
    return {
        "status": "ok",
        "service": "telegram-tiktok-bot"
    }, 200


@app_web.route("/healthz")
def healthz():
    return {
        "status": "ok"
    }, 200


# =========================================================
# DETECCIÓN DE LINKS
# =========================================================

def es_tiktok(url):
    url = url.lower()

    return (
        "tiktok.com" in url
        or "vt.tiktok.com" in url
        or "vm.tiktok.com" in url
        or "tiktokv.com" in url
    )


def es_instagram(url):
    url = url.lower()

    return (
        "instagram.com/reel/" in url
        or "instagram.com/reels/" in url
        or "instagram.com/p/" in url
    )


def es_link_valido(url):
    return es_tiktok(url) or es_instagram(url)


# =========================================================
# LIMPIEZA DE ARCHIVOS TEMPORALES
# =========================================================

def limpiar_temporales(nombre_archivo):
    """
    Borra restos .part, formatos separados y archivos
    temporales que yt-dlp puede dejar después de un fallo.
    """

    base = os.path.splitext(nombre_archivo)[0]

    for archivo in glob.glob(f"{base}*"):

        try:
            if os.path.isfile(archivo):
                os.remove(archivo)

        except Exception as e:
            print(f"No se pudo borrar temporal {archivo}: {e}")


# =========================================================
# OPCIONES PARA TIKTOK
# =========================================================

def opciones_tiktok(nombre_archivo, metodo=1):

    opciones = {
        # Mantiene la máxima calidad disponible.
        "format": "bestvideo+bestaudio/best",

        "outtmpl": nombre_archivo,
        "merge_output_format": "mp4",

        "quiet": True,
        "noplaylist": True,

        "retries": 5,
        "fragment_retries": 5,
        "extractor_retries": 3,

        "socket_timeout": 30,

        # No forzamos impersonate.
        # Render ya confirmó que chrome no está disponible.
    }


    # =====================================================
    # MÉTODO 1
    # Cookies actuales
    # =====================================================

    if metodo == 1:

        opciones["cookiefile"] = "cookies.txt"


    # =====================================================
    # MÉTODO 2
    # Sin cookies
    #
    # A veces una sesión/cookie puede provocar que TikTok
    # responda diferente.
    # =====================================================

    elif metodo == 2:

        pass


    # =====================================================
    # MÉTODO 3
    # Cookies + navegador móvil iPhone
    # =====================================================

    elif metodo == 3:

        opciones["cookiefile"] = "cookies.txt"

        opciones["http_headers"] = {

            "User-Agent": (
                "Mozilla/5.0 "
                "(iPhone; CPU iPhone OS 17_6 like Mac OS X) "
                "AppleWebKit/605.1.15 "
                "(KHTML, like Gecko) "
                "Version/17.6 "
                "Mobile/15E148 "
                "Safari/604.1"
            ),

            "Referer": "https://www.tiktok.com/",

            "Accept-Language": (
                "es-MX,es;q=0.9,en;q=0.8"
            ),
        }


    # =====================================================
    # MÉTODO 4
    # API móvil de TikTok
    #
    # SOLO se usa si existe TIKTOK_IID.
    # =====================================================

    elif metodo == 4:

        if not TIKTOK_IID:
            raise RuntimeError(
                "No existe TIKTOK_IID"
            )

        opciones["cookiefile"] = "cookies.txt"

        opciones["extractor_args"] = {
            "tiktok": {
                "app_info": [
                    TIKTOK_IID
                ]
            }
        }


    return opciones


# =========================================================
# OPCIONES PARA INSTAGRAM
# =========================================================

def opciones_instagram(nombre_archivo):

    return {
        # Instagram estable.
        # Evitamos forzar la unión de formatos.
        "format": "best",

        "outtmpl": nombre_archivo,

        "quiet": True,
        "noplaylist": True,

        "cookiefile": "cookies.txt",

        "retries": 5,
        "fragment_retries": 5,
        "extractor_retries": 3,

        "socket_timeout": 30,
    }


# =========================================================
# MOTOR DE DESCARGA
# =========================================================

def descargar_con_ytdlp(url, nombre_archivo):

    # =====================================================
    # TIKTOK
    # =====================================================

    if es_tiktok(url):

        ultimo_error = None

        metodos = [1, 2, 3]

        # Si algún día agregas un IID real,
        # automáticamente activa el cuarto método.
        if TIKTOK_IID:
            metodos.append(4)


        for metodo in metodos:

            try:

                # Limpiamos restos del intento anterior.
                limpiar_temporales(nombre_archivo)

                print(
                    f"TikTok: intentando método {metodo}"
                )

                opciones = opciones_tiktok(
                    nombre_archivo,
                    metodo
                )


                with yt_dlp.YoutubeDL(opciones) as ydl:

                    ydl.download([url])


                if not os.path.exists(nombre_archivo):

                    raise RuntimeError(
                        "yt-dlp terminó pero no creó el archivo"
                    )


                print(
                    f"TikTok: método {metodo} funcionó"
                )

                return


            except Exception as e:

                ultimo_error = e

                print(
                    f"TikTok método {metodo} falló: {e}"
                )

                limpiar_temporales(nombre_archivo)

                time.sleep(2)


        if ultimo_error:

            raise ultimo_error


        raise RuntimeError(
            "TikTok falló en todos los métodos"
        )


    # =====================================================
    # INSTAGRAM
    # =====================================================

    if es_instagram(url):

        limpiar_temporales(nombre_archivo)

        opciones = opciones_instagram(
            nombre_archivo
        )


        with yt_dlp.YoutubeDL(opciones) as ydl:

            ydl.download([url])


        if not os.path.exists(nombre_archivo):

            raise RuntimeError(
                "Instagram no creó el archivo"
            )


        return


    raise RuntimeError(
        "URL no compatible"
    )


# =========================================================
# SHORTCUT / API
# =========================================================

@app_web.route("/download", methods=["POST"])
def download_video():

    data = request.get_json(
        silent=True
    )


    if not data:

        return {
            "error": "No se recibió información"
        }, 400


    url = data.get("url")


    if not url:

        return {
            "error": "No URL"
        }, 400


    url = str(url).strip()


    if not es_link_valido(url):

        return {
            "error": "Link no válido"
        }, 400


    nombre_archivo = (
        f"{uuid.uuid4()}.mp4"
    )


    try:

        descargar_con_ytdlp(
            url,
            nombre_archivo
        )


        if not os.path.exists(
            nombre_archivo
        ):

            raise RuntimeError(
                "No se descargó el archivo"
            )


        # El archivo se borra cuando Flask
        # termine de responder al Shortcut.

        @after_this_request
        def borrar_archivo(response):

            try:

                limpiar_temporales(
                    nombre_archivo
                )

            except Exception as e:

                print(
                    f"Error limpiando archivo: {e}"
                )

            return response


        return send_file(
            nombre_archivo,

            as_attachment=True,

            download_name="video.mp4",

            mimetype="video/mp4"
        )


    except Exception as e:

        print(
            f"Error /download: {e}"
        )

        limpiar_temporales(
            nombre_archivo
        )


        return {
            "error": str(e)
        }, 500


# =========================================================
# TELEGRAM
# =========================================================

async def descargar_video(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return


    if not update.message.text:
        return


    mensaje = (
        update.message.text.strip()
    )


    if not es_link_valido(mensaje):

        await update.message.reply_text(
            "📎 Mándame un link válido de TikTok o Instagram."
        )

        return


    async with cola_descargas:


        esperando = (
            await update.message.reply_text(
                "⏳ Descargando video..."
            )
        )


        nombre_archivo = (
            f"{uuid.uuid4()}.mp4"
        )


        try:

            # yt-dlp es bloqueante.
            # Lo ejecutamos aparte para no
            # congelar Telegram.

            await asyncio.to_thread(
                descargar_con_ytdlp,
                mensaje,
                nombre_archivo
            )


            if not os.path.exists(
                nombre_archivo
            ):

                raise RuntimeError(
                    "No se descargó el archivo"
                )


            with open(
                nombre_archivo,
                "rb"
            ) as video:

                await update.message.reply_video(
                    video=video,
                    caption="✅ Video descargado"
                )


            await esperando.delete()


        except Exception as e:

            print(
                f"Error Telegram: {e}"
            )


            try:

                await esperando.edit_text(
                    "❌ No pude descargar ese video."
                )

            except Exception:

                pass


        finally:

            limpiar_temporales(
                nombre_archivo
            )


# =========================================================
# SERVIDOR WEB
# =========================================================

def iniciar_web():

    puerto = int(
        os.environ.get(
            "PORT",
            8080
        )
    )


    app_web.run(
        host="0.0.0.0",
        port=puerto,
        threaded=True,
        use_reloader=False
    )


# =========================================================
# BOT TELEGRAM
# =========================================================

def main():

    if not TOKEN:

        raise RuntimeError(
            "Falta la variable BOT_TOKEN"
        )


    app = (
        Application
        .builder()
        .token(TOKEN)
        .build()
    )


    app.add_handler(

        MessageHandler(

            filters.TEXT
            & ~filters.COMMAND,

            descargar_video
        )

    )


    print(
        "Bot encendido..."
    )


    app.run_polling(
        drop_pending_updates=False
    )


# =========================================================
# INICIO
# =========================================================

if __name__ == "__main__":

    threading.Thread(
        target=iniciar_web,
        daemon=True
    ).start()


    main()
