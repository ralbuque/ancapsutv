#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
loop_live.py — Live 24/7 no YouTube com os últimos vídeos do seu canal em loop.

Como funciona:
  1. A cada CHECK_INTERVAL segundos, consulta o canal (yt-dlp) e pega os IDs
     dos últimos MAX_VIDEOS vídeos publicados.
  2. Baixa os que ainda não tem e normaliza (mesma resolução/fps/codec)
     para que a emenda entre vídeos seja perfeita.
  3. Mantém playlist.txt com os vídeos em ordem cronológica (mais antigo primeiro).
  4. Um processo FFmpeg transmite a playlist em loop infinito (-stream_loop -1)
     para o RTMP do YouTube usando "-c copy" (quase zero CPU).
  5. Quando entra vídeo novo, o FFmpeg é reiniciado com a playlist atualizada
     (interrupção de ~2 a 5 segundos, o YouTube segura a live no ar).
  6. Vídeos que saíram da lista dos últimos MAX_VIDEOS são apagados do disco.

Requisitos no Windows: Python 3.9+, ffmpeg.exe e yt-dlp.exe no PATH
(ou na mesma pasta deste script). Veja o README.md.
"""

import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

# ============================ CONFIGURAÇÃO ============================

# URL da aba "Vídeos" do seu canal (use /videos para pegar só uploads, sem lives)
CHANNEL_URL = "https://www.youtube.com/@SEU_CANAL/videos"

# Chave de transmissão da sua live persistente (YouTube Studio > Transmitir ao vivo)
STREAM_KEY = "xxxx-xxxx-xxxx-xxxx-xxxx"

MAX_VIDEOS = 20          # quantos vídeos ficam no loop
CHECK_INTERVAL = 300     # segundos entre verificações do canal (300 = 5 min)

# Formato padrão de normalização (todos os vídeos são convertidos para isso)
WIDTH, HEIGHT = 1920, 1080
FPS = 30
VIDEO_BITRATE = "4500k"  # recomendação do YouTube para 1080p30
AUDIO_BITRATE = "160k"
X264_PRESET = "veryfast"  # use "faster"/"fast" se o servidor tiver CPU sobrando

# ======================================================================

BASE_DIR = Path(__file__).resolve().parent
VIDEO_DIR = BASE_DIR / "videos"
TMP_DIR = BASE_DIR / "tmp"
PLAYLIST = BASE_DIR / "playlist.txt"
RTMP_URL = f"rtmp://a.rtmp.youtube.com/live2/{STREAM_KEY}"

restart_event = threading.Event()   # avisa o streamer que a playlist mudou
playlist_lock = threading.Lock()


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def run(cmd: list, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", **kw)


# ---------------------------- MONITOR ---------------------------------

def latest_video_ids() -> list:
    """IDs dos últimos MAX_VIDEOS uploads do canal, do mais novo ao mais antigo."""
    r = run([
        "yt-dlp", "--flat-playlist", "--playlist-end", str(MAX_VIDEOS),
        "--print", "%(id)s", CHANNEL_URL,
    ])
    if r.returncode != 0:
        log(f"ERRO ao consultar o canal: {r.stderr.strip()[:400]}")
        return []
    return [l.strip() for l in r.stdout.splitlines() if l.strip()]


def download_and_normalize(video_id: str) -> bool:
    """Baixa o vídeo e converte para .ts padronizado em VIDEO_DIR."""
    out_file = VIDEO_DIR / f"{video_id}.ts"
    if out_file.exists():
        return True

    TMP_DIR.mkdir(exist_ok=True)
    raw = TMP_DIR / f"{video_id}.mp4"

    log(f"Baixando {video_id}...")
    r = run([
        "yt-dlp",
        "-f", "bv*[height<=1080]+ba/b[height<=1080]/b",
        "--merge-output-format", "mp4",
        "-o", str(raw),
        f"https://www.youtube.com/watch?v={video_id}",
    ])
    if r.returncode != 0 or not raw.exists():
        log(f"ERRO no download de {video_id}: {r.stderr.strip()[:400]}")
        return False

    log(f"Normalizando {video_id}...")
    vf = (f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
          f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2,fps={FPS},format=yuv420p")
    tmp_out = TMP_DIR / f"{video_id}.ts"
    r = run([
        "ffmpeg", "-y", "-i", str(raw),
        "-vf", vf,
        "-c:v", "libx264", "-preset", X264_PRESET,
        "-b:v", VIDEO_BITRATE, "-maxrate", VIDEO_BITRATE, "-bufsize", "9000k",
        "-g", str(FPS * 2), "-keyint_min", str(FPS * 2), "-sc_threshold", "0",
        "-c:a", "aac", "-b:a", AUDIO_BITRATE, "-ar", "44100", "-ac", "2",
        "-f", "mpegts", str(tmp_out),
    ])
    raw.unlink(missing_ok=True)
    if r.returncode != 0 or not tmp_out.exists():
        log(f"ERRO na normalização de {video_id}: {r.stderr.strip()[:400]}")
        tmp_out.unlink(missing_ok=True)
        return False

    shutil.move(str(tmp_out), str(out_file))
    log(f"Pronto: {out_file.name}")
    return True


def write_playlist(ids_newest_first: list) -> None:
    """Grava playlist.txt em ordem cronológica (mais antigo primeiro)."""
    available = [i for i in ids_newest_first if (VIDEO_DIR / f"{i}.ts").exists()]
    lines = [f"file '{(VIDEO_DIR / f'{i}.ts').as_posix()}'"
             for i in reversed(available)]
    tmp = PLAYLIST.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with playlist_lock:
        tmp.replace(PLAYLIST)


def prune_old(keep_ids: list) -> None:
    keep = {f"{i}.ts" for i in keep_ids}
    for f in VIDEO_DIR.glob("*.ts"):
        if f.name not in keep:
            log(f"Removendo antigo: {f.name}")
            f.unlink(missing_ok=True)


def watcher() -> None:
    VIDEO_DIR.mkdir(exist_ok=True)
    while True:
        try:
            ids = latest_video_ids()
            if ids:
                new = [i for i in ids if not (VIDEO_DIR / f"{i}.ts").exists()]
                changed = False
                for vid in new:
                    if download_and_normalize(vid):
                        changed = True
                prune_old(ids)
                if changed or not PLAYLIST.exists():
                    write_playlist(ids)
                    log("Playlist atualizada — reiniciando transmissão no próximo ciclo.")
                    restart_event.set()
        except Exception as e:
            log(f"ERRO inesperado no monitor: {e}")
        time.sleep(CHECK_INTERVAL)


# ---------------------------- STREAMER --------------------------------

def streamer() -> None:
    while True:
        if not PLAYLIST.exists() or not PLAYLIST.read_text(encoding="utf-8").strip():
            log("Aguardando vídeos na playlist...")
            time.sleep(10)
            continue

        restart_event.clear()
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning",
            "-re", "-stream_loop", "-1",
            "-f", "concat", "-safe", "0", "-i", str(PLAYLIST),
            "-c", "copy", "-bsf:a", "aac_adtstoasc",
            "-f", "flv", RTMP_URL,
        ]
        log("Iniciando transmissão...")
        proc = subprocess.Popen(cmd)

        # espera o processo cair ou a playlist mudar
        while proc.poll() is None and not restart_event.is_set():
            time.sleep(2)

        if proc.poll() is None:
            log("Reiniciando FFmpeg com a playlist nova...")
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        else:
            log(f"FFmpeg caiu (código {proc.returncode}). Reiniciando em 5s...")
            time.sleep(5)


# ------------------------------ MAIN ----------------------------------

def check_tools() -> None:
    for tool in ("ffmpeg", "yt-dlp"):
        if shutil.which(tool) is None:
            sys.exit(f"ERRO: '{tool}' não encontrado no PATH. Veja o README.md.")


def main() -> None:
    if "SEU_CANAL" in CHANNEL_URL or STREAM_KEY.startswith("xxxx"):
        sys.exit("Configure CHANNEL_URL e STREAM_KEY no topo do arquivo antes de rodar.")
    check_tools()
    log(f"Canal: {CHANNEL_URL}")
    log(f"Loop com os últimos {MAX_VIDEOS} vídeos | verificação a cada {CHECK_INTERVAL}s")
    threading.Thread(target=watcher, daemon=True, name="watcher").start()
    try:
        streamer()
    except KeyboardInterrupt:
        log("Encerrado pelo usuário.")


if __name__ == "__main__":
    main()
