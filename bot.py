from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters
import yt_dlp
import os
import uuid
from flask import Flask
import threading

TOKEN = os.getenv("BOT_TOKEN")
app_web = Flask(__name__)


def iniciar_web():
    puerto = int(os.environ.get("PORT", 8080))
    app_web.run(host="0.0.0.0", port=puerto)


async def descargar_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mensaje = update.message.text.strip()

    if "tiktok.com" not in mensaje:
        await update.message.reply_text(
            "📎 Mándame un link válido de TikTok."
        )
        return

    esperando = await update.message.reply_text(
        "⏳ Descargando video..."
    )

    nombre_archivo = f"{uuid.uuid4()}.mp4"

    opciones = {
        "format": "bestvideo+bestaudio/best",
        "outtmpl": nombre_archivo,
        "merge_output_format": "mp4",
        "quiet": True,
        "noplaylist": True,
        "cookiefile": "cookies.txt",
    }

    try:
        with yt_dlp.YoutubeDL(opciones) as ydl:
            ydl.download([mensaje])

        with open(nombre_archivo, "rb") as video:
            await update.message.reply_video(
                video=video,
                caption="✅ Aquí está tu video sin marca de agua"
            )

        await esperando.delete()

    except Exception:
        await esperando.edit_text(
            "❌ No pude descargar ese TikTok."
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
