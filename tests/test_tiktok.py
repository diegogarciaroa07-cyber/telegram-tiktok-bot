"""Regresiones de TikTok. Sin red, tokens reales ni envíos a Telegram.

Ejecutar: python -m pytest -q (requiere pytest además de requirements.txt).
Los MP4 de prueba se generan con FFmpeg en el directorio temporal de pytest.
"""
import asyncio
import json
import shutil
import struct
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import bot
from yt_dlp.postprocessor.ffmpeg import FFmpegMergerPP


@pytest.fixture(scope="session")
def media(tmp_path_factory):
    folder = tmp_path_factory.mktemp("mp4")
    paths = {}
    for name, resolution in (("low", "64x96"), ("high", "128x192")):
        path = folder / f"{name}.mp4"
        subprocess.run([
            bot._tiktok_ffmpeg(), "-v", "error", "-y", "-f", "lavfi",
            "-i", f"testsrc2=size={resolution}:rate=15", "-f", "lavfi",
            "-i", "sine=frequency=880:sample_rate=44100", "-t", "1",
            "-c:v", "libx264", "-crf", "18", "-c:a", "aac",
            "-movflags", "+faststart", str(path),
        ], check=True, capture_output=True)
        paths[name] = path
    return paths


def test_python_impersonation_api_and_read_only_cookies(tmp_path, monkeypatch):
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n.tiktok.com\tTRUE\t/\tFALSE\t2147483647\ttest_cookie\tfixture\n")
    monkeypatch.setattr(bot, "TIKTOK_COOKIEFILE", str(cookies))
    before = cookies.read_bytes()
    options = bot._opciones_tiktok_ytdlp("unused.mp4", 2)
    assert isinstance(options["impersonate"], bot.ImpersonateTarget)
    assert "cookiefile" not in options
    with bot._abrir_tiktok_ytdlp(options, 2) as ydl:
        assert any(c.name == "test_cookie" for c in ydl.cookiejar)
    assert cookies.read_bytes() == before


def test_probe_tracks_and_rejects_truncation(media, tmp_path):
    info = bot._info_mp4(media["high"])
    assert (info["width"], info["height"], info["fps"]) == (128, 192, 15)
    assert info["codec"] == "avc1" and info["has_audio"]
    assert info["duration"] == 1
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(media["high"].read_bytes()[:-100])
    with pytest.raises(ValueError):
        bot._info_mp4(broken)
    broken.write_bytes(b"\x00\x00\x00\x18ftyp" + b"x" * 16)
    with pytest.raises(ValueError):
        bot._info_mp4(broken)


def test_container_padding_never_decides_quality(media, tmp_path):
    padded = tmp_path / "padded.mp4"
    padded.write_bytes(media["low"].read_bytes() + struct.pack(">I4s", 1000008, b"free") + b"\0" * 1000000)
    small, big = bot._info_mp4(media["low"]), bot._info_mp4(padded)
    assert big["bytes"] > small["bytes"] * 10
    assert big["video_bitrate"] == small["video_bitrate"]
    assert not bot._tiktok_es_mejor(small, big)
    assert not bot._tiktok_es_mejor(bot._info_mp4(media["high"]), big)


@pytest.mark.parametrize("changes", [
    {"duration": 10}, {"has_audio": False}, {"width": 500, "height": 64},
])
def test_wrong_duration_audio_or_aspect_cannot_replace(media, changes):
    actual = bot._info_mp4(media["low"])
    candidate = dict(bot._info_mp4(media["high"]), **changes)
    assert not bot._tiktok_es_mejor(actual, candidate)


def test_hevc_not_excluded_and_same_codec_bitrate_tiebreak(media):
    low = bot._info_mp4(media["low"])
    high = dict(bot._info_mp4(media["high"]), codec="hvc1")
    assert bot._tiktok_es_mejor(low, high)
    assert bot._tiktok_es_mejor(low, dict(low, video_bitrate=low["video_bitrate"] * 2))


def test_ssstik_parser_ignores_placeholder_music_and_external_post():
    html = '<a href="#" class="download_link">Download</a><a href="https://tikcdn.io/ssstik/m/audio" class="music">Download MP3</a>'
    assert bot._enlaces_ssstik(html) == []
    endpoint, token = bot._ssstik_formulario_hd(
        '<a id="hd_download" data-directurl="/abc?url=signed"></a><input name="tt" value="result-token">')
    assert endpoint == "https://ssstik.io/abc?url=signed" and token == "result-token"
    assert bot._ssstik_formulario_hd('<a id="hd_download" data-directurl="https://example.org/"></a>')[0] is None
    assert not bot._ssstik_url_media_permitida("https://tikcdn.io.evil.example/file")


class Response:
    def __init__(self, text="", content=b"", headers=None):
        self.text, self.content = text, content
        self.headers = headers or {}
        self.status_code = 200
        self.closed = False

    def raise_for_status(self):
        pass

    def iter_content(self, **kwargs):
        yield self.content

    def close(self):
        self.closed = True


@pytest.mark.parametrize("hd_available", [True, False])
def test_ssstik_hd_post_hx_redirect_and_normal_fallback(media, tmp_path, monkeypatch, hd_available):
    calls, streams = [], []
    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, url, **kwargs):
            if url == "https://ssstik.io/en":
                return Response("s_tt='bootstrap'; s_furl='abc';")
            response = Response(content=media["high" if url.endswith("high") else "low"].read_bytes())
            streams.append(response)
            return response

        def post(self, url, **kwargs):
            calls.append((url, kwargs))
            if url.endswith("?url=dl"):
                return Response('<a id="hd_download" data-directurl="/abc?url=hd"></a><input name="tt" value="result-token"><a href="https://tikcdn.io/low" class="download_link">Download</a>')
            assert kwargs["data"] == {"tt": "result-token"}
            return Response(headers={"HX-Redirect": "https://tikcdn.io/high"} if hd_available else {"HX-Trigger": "sssrapidapifail"})
    monkeypatch.setattr(bot.cffi_requests, "Session", Session)
    destination = str(tmp_path / "selected.mp4")
    result = bot._descargar_ssstik_mejor("https://www.tiktok.com/@_/video/7705284346378243349", destination)
    assert Path(destination).read_bytes() == media["high" if hd_available else "low"].read_bytes()
    assert result["hd_accesible"] is hd_available
    assert len(calls) == 2 and all(r.closed for r in streams)


def test_source_failure_preserves_player_and_reports_limit(media, tmp_path, monkeypatch):
    destination = str(tmp_path / "selected.mp4")
    monkeypatch.setattr(bot, "descargar_tiktok_player", lambda u, p: shutil.copyfile(media["low"], p))
    def failed(*args):
        raise RuntimeError("source unavailable")
    monkeypatch.setattr(bot, "_descargar_ssstik_mejor", failed)
    monkeypatch.setattr(bot, "descargar_tiktok_ytdlp", failed)
    bot.descargar_tiktok("https://www.tiktok.com/@_/video/7705284346378243349", destination)
    assert Path(destination).read_bytes() == media["low"].read_bytes()
    assert "SSSTik no disponible" in bot._tiktok_calidad(destination)["warnings"]
    assert "no se encontró Full HD" in bot._tiktok_caption(destination)


def test_all_sources_checked_even_after_good_player(media, tmp_path, monkeypatch):
    calls = []
    destination = str(tmp_path / "selected.mp4")
    monkeypatch.setattr(bot, "descargar_tiktok_player", lambda u, p: shutil.copyfile(media["high"], p))
    monkeypatch.setattr(bot, "_descargar_ssstik_mejor", lambda *args: calls.append("ssstik") or {"hd_accesible": True})
    monkeypatch.setattr(bot, "descargar_tiktok_ytdlp", lambda *args: calls.append("ytdlp"))
    bot.descargar_tiktok("https://www.tiktok.com/@_/video/7705284346378243349", destination)
    assert calls == ["ssstik", "ytdlp"]


def test_ssstik_works_when_player_unavailable(media, tmp_path, monkeypatch):
    def failed(*args):
        raise RuntimeError("Player unavailable")
    def ssstik(url, destination):
        candidate = str(tmp_path / "candidate.mp4")
        shutil.copyfile(media["high"], candidate)
        bot._tiktok_conservar_candidata(candidate, destination, "SSSTik")
        return {"hd_accesible": True}
    monkeypatch.setattr(bot, "descargar_tiktok_player", failed)
    monkeypatch.setattr(bot, "descargar_tiktok_ytdlp", failed)
    monkeypatch.setattr(bot, "_descargar_ssstik_mejor", ssstik)
    destination = str(tmp_path / "selected.mp4")
    bot.descargar_tiktok("https://vt.tiktok.com/test/", destination)
    assert Path(destination).read_bytes() == media["high"].read_bytes()
    assert bot._tiktok_calidad(destination)["source"] == "SSSTik"


def test_ytdlp_compares_all_methods_instead_of_first_success(media, tmp_path, monkeypatch):
    calls = []
    class Downloader:
        def __init__(self, options, method):
            self.options, self.method = options, method
        def __enter__(self):
            calls.append(self.method)
            return self
        def __exit__(self, *args):
            pass
        def extract_info(self, *args, **kwargs):
            shutil.copyfile(media["high" if self.method == 3 else "low"], self.options["outtmpl"])
            return {"id": "fixture"}
    monkeypatch.setattr(bot, "_abrir_tiktok_ytdlp", Downloader)
    destination = str(tmp_path / "selected.mp4")
    bot.descargar_tiktok_ytdlp("https://vt.tiktok.com/test/", destination)
    assert calls == [1, 2, 3]
    assert Path(destination).read_bytes() == media["high"].read_bytes()


def install_download_fixture(monkeypatch, media):
    def download(url, destination):
        shutil.copyfile(media["high"], destination)
        info = dict(bot._info_mp4(destination), status="best-accessible", warnings=[])
        Path(bot._tiktok_ruta_calidad(destination)).write_text(json.dumps(info))
        return destination
    monkeypatch.setattr(bot, "descargar_tiktok", download)


def test_shortcut_response_is_exact_file_with_headers_and_cleanup(media, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bot.app_web, "root_path", str(tmp_path))
    install_download_fixture(monkeypatch, media)
    with bot.app_web.test_client() as client:
        response = client.post("/download", json={"url": "https://vt.tiktok.com/test/"})
        assert response.status_code == 200
        assert response.data == media["high"].read_bytes()
        assert response.headers["X-Video-Width"] == "128"
        assert response.headers["Content-Type"] == "video/mp4"
        response.close()
    assert not list(tmp_path.glob("*.mp4"))
    assert not list(tmp_path.glob("*_calidad.json"))


@pytest.mark.parametrize("video_fails", [False, True])
def test_telegram_receives_exact_bytes_and_document_fallback(media, tmp_path, monkeypatch, video_fails):
    monkeypatch.chdir(tmp_path)
    install_download_fixture(monkeypatch, media)
    payloads = []
    async def video(**kwargs):
        if video_fails:
            raise RuntimeError("Unsupported video")
        payloads.append(kwargs["video"].read())
    async def document(**kwargs):
        payloads.append(kwargs["document"].read())
    waiting = SimpleNamespace(delete=AsyncMock(), edit_text=AsyncMock())
    message = SimpleNamespace(text="https://vt.tiktok.com/test/", reply_text=AsyncMock(return_value=waiting), reply_video=video, reply_document=document)
    monkeypatch.setattr(bot, "cola_descargas", asyncio.Lock())
    asyncio.run(bot.procesar_enlace(SimpleNamespace(message=message), None))
    assert payloads == [media["high"].read_bytes()]
    assert not list(tmp_path.glob("*.mp4"))


def test_telegram_upload_failure_is_not_reported_as_download_failure(media, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    install_download_fixture(monkeypatch, media)
    waiting = SimpleNamespace(delete=AsyncMock(), edit_text=AsyncMock())
    message = SimpleNamespace(
        text="https://vt.tiktok.com/test/", reply_text=AsyncMock(return_value=waiting),
        reply_video=AsyncMock(side_effect=RuntimeError("upload failed")),
        reply_document=AsyncMock(side_effect=RuntimeError("upload failed")),
    )
    monkeypatch.setattr(bot, "cola_descargas", asyncio.Lock())
    asyncio.run(bot.procesar_enlace(SimpleNamespace(message=message), None))
    assert "Telegram no pudo enviarlo" in waiting.edit_text.call_args.args[0]
    assert not list(tmp_path.glob("*.mp4"))


def test_actual_ytdlp_merger_copies_video_packets(media, tmp_path):
    ffmpeg = bot._tiktok_ffmpeg()
    video, audio, output = (tmp_path / n for n in ("video.mp4", "audio.m4a", "merged.mp4"))
    for target, mapping in ((video, "0:v:0"), (audio, "0:a:0")):
        subprocess.run([ffmpeg, "-v", "error", "-i", str(media["high"]), "-map", mapping, "-c", "copy", str(target)], check=True)
    with bot.yt_dlp.YoutubeDL({"quiet": True, "ffmpeg_location": ffmpeg}) as ydl:
        FFmpegMergerPP(ydl).run({
            "filepath": str(output), "ext": "mp4",
            "__files_to_merge": [str(video), str(audio)],
            "requested_formats": [
                {"vcodec": "h264", "acodec": "none", "protocol": "https"},
                {"vcodec": "none", "acodec": "aac", "protocol": "https"},
            ],
        })
    def stream_hash(path):
        r = subprocess.run([ffmpeg, "-v", "error", "-i", str(path), "-map", "0:v:0", "-c", "copy", "-f", "hash", "-"], capture_output=True, check=True)
        return r.stdout
    assert stream_hash(media["high"]) == stream_hash(output)
    assert bot._info_mp4(output)["has_audio"]
