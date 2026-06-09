from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters
import yt_dlp
import os
import uuid
from flask import Flask, request, send_file
import threading

TOKEN = os.getenv("BOT_TOKEN")
app_web = Flask(__name__)


def obtener_opciones(nombre_archivo):
    return {
        "format": "best",
        "outtmpl": nombre_archivo,
        "merge_output_format": "mp4",
        "quiet": False,
        "noplaylist": True,

        # cookies TikTok
        "cookiefile": "cookies.txt",

        # reintentos
        "retries": 20,
        "fragment_retries": 20,
        "extractor_retries": 20,
        "socket_timeout": 60,

        # mejor compatibilidad TikTok
        "extractor_args": {
            "tiktok": {
                "api_hostname": "api16-normal-c-useast1a.tiktokv.com"
            }
        },

        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                "Version/17.0 Mobile/15E148 Safari/604.1"
            ),
            "Referer": "https://www.tiktok.com/",
        }
    }


@app_web.route("/download", methods=["POST"])
def download_video():
    data = request.get_json()

    if not data or "url" not in data:
        return {"error": "No URL"}, 400

    url = data["url"]
    nombre_archivo = f"{uuid.uuid4()}.mp4"

    opciones = obtener_opciones(nombre_archivo)

    try:
        try:
            with yt_dlp.YoutubeDL(opciones) as ydl:
                ydl.download([url])

        except Exception:
            print("Primer método falló, intentando alternativo...")

            opciones2 = opciones.copy()

            # fallback
            opciones2.pop("extractor_args", None)

            with yt_dlp.YoutubeDL(opciones2) as ydl:
                ydl.download([url])

        if not os.path.exists(nombre_archivo):
            raise Exception("No se pudo descargar el video.")

        return send_file(
            nombre_archivo,
            as_attachment=True,
            download_name="video.mp4",
            mimetype="video/mp4"
        )

    except Exception as e:
        return {"error": str(e)}, 500

    finally:
        if os.path.exists(nombre_archivo):
            os.remove(nombre_archivo)


def iniciar_web():
    puerto = int(os.environ.get("PORT", 8080))
    app_web.run(host="0.0.0.0", port=puerto)


async def descargar_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mensaje = update.message.text.strip()

    if "tiktok.com" not in mensaje and "vt.tiktok.com" not in mensaje:
        await update.message.reply_text(
            "📎 Mándame un link válido de TikTok."
        )
        return

    esperando = await update.message.reply_text(
        "⏳ Descargando video..."
    )

    nombre_archivo = f"{uuid.uuid4()}.mp4"
    opciones = obtener_opciones(nombre_archivo)

    try:
        try:
            with yt_dlp.YoutubeDL(opciones) as ydl:
                ydl.download([mensaje])

        except Exception:
            print("Primer método falló, intentando alternativo...")

            opciones2 = opciones.copy()

            # fallback si TikTok bloquea
            opciones2.pop("extractor_args", None)

            with yt_dlp.YoutubeDL(opciones2) as ydl:
                ydl.download([mensaje])

        if not os.path.exists(nombre_archivo):
            raise Exception("El archivo no se descargó.")

        with open(nombre_archivo, "rb") as video:
            await update.message.reply_video(
                video=video,
                caption="✅ Aquí está tu video sin marca de agua"
            )

        await esperando.delete()

    except Exception as e:
        await esperando.edit_text(
            f"❌ No pude descargar ese TikTok.\n\n{str(e)[:150]}"
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
