from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters
import yt_dlp
import os
import uuid
from flask import Flask, request, send_file
import threading
import asyncio

TOKEN = os.getenv("BOT_TOKEN")
app_web = Flask(__name__)

# Cola de descargas
cola_descargas = asyncio.Lock()


def es_link_valido(link):
    return (
        "tiktok.com" in link
        or "vt.tiktok.com" in link
        or "instagram.com/reel/" in link
        or "instagram.com/p/" in link
    )


def obtener_opciones(nombre_archivo, url):
    # TikTok = máxima calidad
    if "tiktok.com" in url or "vt.tiktok.com" in url:
        return {
            "format": "bestvideo+bestaudio/best",
            "outtmpl": nombre_archivo,
            "merge_output_format": "mp4",
            "quiet": True,
            "noplaylist": True,
            "cookiefile": "cookies.txt",
        }

    # Instagram = estable (sin ffmpeg)
    return {
        "format": "best",
        "outtmpl": nombre_archivo,
        "quiet": True,
        "noplaylist": True,
        "cookiefile": "cookies.txt",
    }


@app_web.route("/download", methods=["POST"])
def download_video():
    data = request.get_json()

    if not data or "url" not in data:
        return {"error": "No URL"}, 400

    url = data["url"]

    if not es_link_valido(url):
        return {"error": "Link no válido"}, 400

    nombre_archivo = f"{uuid.uuid4()}.mp4"
    opciones = obtener_opciones(nombre_archivo, url)

    try:
        with yt_dlp.YoutubeDL(opciones) as ydl:
            ydl.download([url])

        if not os.path.exists(nombre_archivo):
            return {"error": "No se descargó el archivo"}, 500

        return send_file(
            nombre_archivo,
            as_attachment=True,
            download_name="video.mp4",
            mimetype="video/mp4"
        )

    except Exception as e:
        return {"error": str(e)}, 500


def iniciar_web():
    puerto = int(os.environ.get("PORT", 8080))
    app_web.run(host="0.0.0.0", port=puerto)


async def descargar_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mensaje = update.message.text.strip()

    if not es_link_valido(mensaje):
        await update.message.reply_text(
            "📎 Mándame un link válido de TikTok o Instagram."
        )
        return

    async with cola_descargas:

        esperando = await update.message.reply_text(
            "⏳ Descargando video..."
        )

        nombre_archivo = f"{uuid.uuid4()}.mp4"
        opciones = obtener_opciones(nombre_archivo, mensaje)

        try:
            with yt_dlp.YoutubeDL(opciones) as ydl:
                ydl.download([mensaje])

            if not os.path.exists(nombre_archivo):
                raise Exception("No se descargó el archivo")

            with open(nombre_archivo, "rb") as video:
                await update.message.reply_video(
                    video=video,
                    caption="✅ Video descargado"
                )

            await esperando.delete()

        except Exception:
            await esperando.edit_text(
                "❌ No pude descargar ese video."
            )

        finally:
            if os.path.exists(nombre_archivo):
                os.remove(nombre_archivo)


def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            descargar_video
        )
    )

    print("Bot encendido...")
    app.run_polling()


if __name__ == "__main__":
    threading.Thread(target=iniciar_web).start()
    main()
