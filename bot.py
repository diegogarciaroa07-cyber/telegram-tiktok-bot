from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters
import yt_dlp
import os
import uuid
import time
import threading
import asyncio

from flask import Flask, request, send_file, after_this_request


TOKEN = os.getenv("BOT_TOKEN")

app_web = Flask(__name__)

# Cola de descargas de Telegram
cola_descargas = asyncio.Lock()


# =========================================================
# RUTA PRINCIPAL PARA RENDER
# =========================================================

@app_web.route("/")
def home():
    return {
        "status": "ok",
        "service": "telegram-tiktok-bot"
    }, 200


# =========================================================
# DETECCIÓN DE LINKS
# =========================================================

def es_tiktok(url):
    return (
        "tiktok.com" in url
        or "vt.tiktok.com" in url
        or "vm.tiktok.com" in url
    )


def es_instagram(url):
    return (
        "instagram.com/reel/" in url
        or "instagram.com/reels/" in url
        or "instagram.com/p/" in url
    )


def es_link_valido(url):
    return es_tiktok(url) or es_instagram(url)


# =========================================================
# OPCIONES GENERALES
# =========================================================

def opciones_tiktok(nombre_archivo, metodo=1):

    opciones = {
        # Máxima calidad posible
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

    # -----------------------------------------------------
    # MÉTODO 1
    # Cookies + navegador Chrome
    # -----------------------------------------------------

    if metodo == 1:
        opciones["cookiefile"] = "cookies.txt"
        opciones["impersonate"] = "chrome"

    # -----------------------------------------------------
    # MÉTODO 2
    # Chrome sin cookies
    # Algunas veces TikTok rechaza cookies viejas
    # -----------------------------------------------------

    elif metodo == 2:
        opciones["impersonate"] = "chrome"

    # -----------------------------------------------------
    # MÉTODO 3
    # Cookies sin impersonación
    # Fallback al método clásico que ya te funcionaba
    # -----------------------------------------------------

    elif metodo == 3:
        opciones["cookiefile"] = "cookies.txt"

    return opciones


def opciones_instagram(nombre_archivo):

    return {
        # Instagram estable:
        # evita obligar a unir video + audio
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

    # -----------------------------------------------------
    # TIKTOK
    # -----------------------------------------------------

    if es_tiktok(url):

        ultimo_error = None

        # Intentamos tres métodos distintos
        for metodo in (1, 2, 3):

            try:

                print(
                    f"TikTok: intento con método {metodo}"
                )

                opciones = opciones_tiktok(
                    nombre_archivo,
                    metodo
                )

                with yt_dlp.YoutubeDL(opciones) as ydl:
                    ydl.download([url])

                # Si llegó aquí, funcionó
                return

            except Exception as e:

                ultimo_error = e

                print(
                    f"TikTok método {metodo} falló: {e}"
                )

                # Pequeña espera antes del siguiente método
                time.sleep(2)

        # Fallaron los tres
        raise ultimo_error


    # -----------------------------------------------------
    # INSTAGRAM
    # -----------------------------------------------------

    elif es_instagram(url):

        opciones = opciones_instagram(
            nombre_archivo
        )

        with yt_dlp.YoutubeDL(opciones) as ydl:
            ydl.download([url])

        return


    raise Exception("URL no compatible")


# =========================================================
# SHORTCUT / API
# =========================================================

@app_web.route("/download", methods=["POST"])
def download_video():

    data = request.get_json(silent=True)

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


    nombre_archivo = f"{uuid.uuid4()}.mp4"

    try:

        descargar_con_ytdlp(
            url,
            nombre_archivo
        )

        if not os.path.exists(nombre_archivo):
            return {
                "error": "No se descargó el archivo"
            }, 500


        # Borrar el archivo DESPUÉS de que Flask lo envíe.
        # No lo borramos antes porque el Shortcut todavía
        # necesita recibirlo completo.

        @after_this_request
        def borrar_archivo(response):

            try:

                if os.path.exists(nombre_archivo):
                    os.remove(nombre_archivo)

            except Exception as e:

                print(
                    f"No se pudo borrar archivo temporal: {e}"
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

        if os.path.exists(nombre_archivo):

            try:
                os.remove(nombre_archivo)

            except Exception:
                pass

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

    mensaje = update.message.text.strip()

    if not es_link_valido(mensaje):

        await update.message.reply_text(
            "📎 Mándame un link válido de TikTok o Instagram."
        )

        return


    # Esto hace que si mandas varios enlaces,
    # se procesen ordenadamente y no se pisen.

    async with cola_descargas:

        esperando = await update.message.reply_text(
            "⏳ Descargando video..."
        )

        nombre_archivo = f"{uuid.uuid4()}.mp4"

        try:

            # yt-dlp es bloqueante, así que lo mandamos
            # a un thread para no congelar Telegram.

            await asyncio.to_thread(
                descargar_con_ytdlp,
                mensaje,
                nombre_archivo
            )


            if not os.path.exists(nombre_archivo):

                raise Exception(
                    "No se descargó el archivo"
                )


            with open(nombre_archivo, "rb") as video:

                await update.message.reply_video(
                    video=video,
                    caption="✅ Video descargado"
                )


            await esperando.delete()


        except Exception as e:

            print(
                f"Error Telegram: {e}"
            )

            await esperando.edit_text(
                "❌ No pude descargar ese video."
            )


        finally:

            if os.path.exists(nombre_archivo):

                try:
                    os.remove(nombre_archivo)

                except Exception:
                    pass


# =========================================================
# SERVIDOR FLASK
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
        threaded=True
    )


# =========================================================
# BOT
# =========================================================

def main():

    if not TOKEN:

        raise RuntimeError(
            "Falta la variable de entorno BOT_TOKEN"
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
