#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
loop_live.py — Live 24/7 no YouTube com os últimos vídeos do seu canal em loop.

Como funciona:
  1. A cada CHECK_INTERVAL segundos, consulta o canal (yt-dlp) e pega os IDs
     dos últimos MAX_VIDEOS vídeos publicados.
  2. Baixa os que ainda não tem, corta os CUT_END_SECONDS segundos finais,
     gera legendas com Whisper (local) e as queima no vídeo, e normaliza
     (mesma resolução/fps/codec) para a emenda entre vídeos ser perfeita.
  3. Mantém playlist.txt em ordem cronológica, com a vinheta (vinheta.mp4)
     intercalada entre todos os vídeos.
  4. Um processo FFmpeg transmite a playlist em loop infinito (-stream_loop -1)
     para o RTMP do YouTube usando "-c copy" (quase zero CPU).
  5. Quando entra vídeo novo, o FFmpeg é reiniciado com a playlist atualizada
     (interrupção de ~2 a 5 segundos, o YouTube segura a live no ar).
  6. Vídeos que saíram da lista dos últimos MAX_VIDEOS são apagados do disco.

Requisitos no Windows: Python 3.9+, ffmpeg.exe e yt-dlp.exe no PATH,
e "pip install faster-whisper" para as legendas. Veja o README.md.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import threading
import time
from datetime import datetime
from pathlib import Path

# ============================ CONFIGURAÇÃO ============================
# As configurações ficam em config.py (fora do versionamento).
# Copie config.example.py para config.py e preencha seus dados.

try:
    import config as _cfg
except ImportError:
    sys.exit("ERRO: arquivo config.py não encontrado.\n"
             "Copie config.example.py para config.py e preencha "
             "CHANNEL_URL e STREAM_KEY.")


def _get(name, default=None, required=False):
    val = getattr(_cfg, name, default)
    if required and (val is None or "SEU_CANAL" in str(val) or str(val).startswith("xxxx")):
        sys.exit(f"ERRO: configure {name} no config.py.")
    return val


CHANNEL_URL = _get("CHANNEL_URL", required=True)
STREAM_KEY = _get("STREAM_KEY", required=True)
MAX_VIDEOS = _get("MAX_VIDEOS", 20)
CHECK_INTERVAL = _get("CHECK_INTERVAL", 300)
BUMPER_SOURCE_NAME = _get("BUMPER_SOURCE_NAME", "vinheta.mp4")
CUT_END_SECONDS = _get("CUT_END_SECONDS", 10)
BURN_SUBTITLES = _get("BURN_SUBTITLES", True)
WHISPER_MODEL = _get("WHISPER_MODEL", "small")
SUBTITLE_STYLE = _get("SUBTITLE_STYLE",
                      "FontName=Arial,FontSize=16,PrimaryColour=&H00FFFFFF,"
                      "OutlineColour=&H00000000,BorderStyle=1,Outline=2,"
                      "Shadow=0,MarginV=30")
WIDTH = _get("WIDTH", 1920)
HEIGHT = _get("HEIGHT", 1080)
FPS = _get("FPS", 30)
VIDEO_BITRATE = _get("VIDEO_BITRATE", "4500k")
AUDIO_BITRATE = _get("AUDIO_BITRATE", "160k")
X264_PRESET = _get("X264_PRESET", "veryfast")

# Publicação automática no X (ex-Twitter)
X_ENABLED = _get("X_ENABLED", False)
X_API_KEY = _get("X_API_KEY", "")
X_API_SECRET = _get("X_API_SECRET", "")
X_ACCESS_TOKEN = _get("X_ACCESS_TOKEN", "")
X_ACCESS_TOKEN_SECRET = _get("X_ACCESS_TOKEN_SECRET", "")
X_TEXT_TEMPLATE = _get("X_TEXT_TEMPLATE", "{title}")
X_MAX_VIDEO_SECONDS = _get("X_MAX_VIDEO_SECONDS", 140)
X_IF_TOO_LONG = _get("X_IF_TOO_LONG", "trim")  # "trim" corta / "skip" não posta
X_ENDCARD = _get("X_ENDCARD", True)  # tela "veja no YouTube" nos posts cortados
# Reply automático ao post com o link do YouTube ("" desativa)
X_REPLY_TEMPLATE = _get("X_REPLY_TEMPLATE", "Veja o vídeo completo em {url}")
X_TARGET_SIZE_MB = _get("X_TARGET_SIZE_MB", 500)  # teto da API é ~512 MB

# Banner de abertura (thumbnail + título no topo da tela)
INTRO_BANNER = _get("INTRO_BANNER", True)
INTRO_SECONDS = _get("INTRO_SECONDS", 30)
INTRO_FONT = _get("INTRO_FONT", "C:/Windows/Fonts/arialbd.ttf")

# ======================================================================

BASE_DIR = Path(__file__).resolve().parent
VIDEO_DIR = BASE_DIR / "videos"
TMP_DIR = BASE_DIR / "tmp"
PLAYLIST = BASE_DIR / "playlist.txt"
PLAYLIST_PENDING = BASE_DIR / "playlist_new.txt"
BUMPER_SOURCE = BASE_DIR / BUMPER_SOURCE_NAME
BUMPER_TS = BASE_DIR / "bumper.ts"
COOKIES_FILE = BASE_DIR / "cookies.txt"
VOCAB_FILE = BASE_DIR / "vocabulario.txt"    # nomes próprios e siglas, 1 por linha
FIXES_FILE = BASE_DIR / "correcoes.txt"      # linhas "errado => certo"
DOWNLOAD_PAUSE = _get("DOWNLOAD_PAUSE", 20)  # segundos entre downloads seguidos
RTMP_URL = f"rtmp://a.rtmp.youtube.com/live2/{STREAM_KEY}"


def ytdlp_cmd() -> list:
    """Comando base do yt-dlp, com cookies (se existirem) e ritmo reduzido
    para não disparar o bloqueio anti-robô do YouTube em IPs de datacenter."""
    cmd = ["yt-dlp", "--sleep-requests", "1", "--retries", "5",
           "--retry-sleep", "10"]
    if COOKIES_FILE.exists():
        cmd += ["--cookies", str(COOKIES_FILE)]
    return cmd

restart_event = threading.Event()   # avisa o streamer que a playlist mudou
_whisper_model = None


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def run(cmd: list, **kw) -> subprocess.CompletedProcess:
    # PYTHONUTF8 força o yt-dlp a emitir UTF-8 no Windows (senão títulos com
    # acentos saem na codificação regional e viram caracteres inválidos)
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env, **kw)


def video_duration(path: Path):
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)])
    try:
        return float(r.stdout.strip())
    except ValueError:
        return None


# ---------------------------- LEGENDAS --------------------------------

def get_whisper():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        log(f"Carregando modelo Whisper '{WHISPER_MODEL}' "
            "(na primeira execução o modelo é baixado)...")
        _whisper_model = WhisperModel(WHISPER_MODEL, device="cpu",
                                      compute_type="int8")
    return _whisper_model


def srt_time(t: float) -> str:
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int((t % 1) * 1000):03d}"


def load_vocab() -> list:
    """Nomes próprios/siglas de vocabulario.txt (1 por linha, # comenta)."""
    if not VOCAB_FILE.exists():
        return []
    return [l.strip() for l in
            VOCAB_FILE.read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.strip().startswith("#")]


def load_fixes() -> list:
    """Pares (errado, certo) de correcoes.txt, formato: errado => certo."""
    fixes = []
    if FIXES_FILE.exists():
        for l in FIXES_FILE.read_text(encoding="utf-8").splitlines():
            if "=>" in l and not l.strip().startswith("#"):
                wrong, right = l.split("=>", 1)
                if wrong.strip():
                    fixes.append((wrong.strip(), right.strip()))
    return fixes


def apply_fixes(text: str, fixes: list) -> str:
    for wrong, right in fixes:
        text = re.sub(re.escape(wrong), right, text, flags=re.IGNORECASE)
    return text


def generate_srt(video_path: Path, srt_path: Path) -> bool:
    """Transcreve o vídeo e grava um .srt. Retorna False se não houver fala."""
    model = get_whisper()
    kw = {}
    terms = load_vocab()
    if terms:
        joined = ", ".join(terms)
        kw["hotwords"] = joined
        kw["initial_prompt"] = ("Transcrição de noticiário em português do "
                                f"Brasil. Vocabulário: {joined}.")
    segments, _info = model.transcribe(str(video_path), language="pt",
                                       vad_filter=True, **kw)
    fixes = load_fixes()
    entries = []
    n = 0
    for seg in segments:
        text = apply_fixes(seg.text.strip(), fixes)
        if not text:
            continue
        n += 1
        wrapped = "\n".join(textwrap.wrap(text, width=45))
        entries.append(f"{n}\n{srt_time(seg.start)} --> {srt_time(seg.end)}\n"
                       f"{wrapped}\n")
    if not entries:
        return False
    srt_path.write_text("\n".join(entries), encoding="utf-8")
    return True


# --------------------------- NORMALIZAÇÃO -----------------------------

def normalize(src: Path, dst: Path, t_limit=None, srt: Path = None,
              thumb: Path = None, title_txt: Path = None) -> bool:
    """Converte src para o formato padrão .ts em dst.

    t_limit: duração máxima em segundos (corta o final).
    srt: arquivo .srt para queimar no vídeo.
    thumb/title_txt: se presentes, desenha o banner de abertura (faixa no topo
        com a thumbnail e o título) nos primeiros INTRO_SECONDS segundos.
    Todos os arquivos auxiliares devem estar na MESMA pasta de src — o ffmpeg
    roda com cwd nessa pasta para evitar problemas de escape de caminhos do
    Windows nos filtros.
    """
    workdir = src.parent
    base = (f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
            f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2,fps={FPS}")
    tail = (f"subtitles={srt.name}:force_style='{SUBTITLE_STYLE}',"
            if srt is not None else "")

    tmp_out = workdir / (dst.stem + ".part.ts")
    cmd = ["ffmpeg", "-y", "-i", src.name]

    banner = (INTRO_BANNER and thumb is not None and thumb.exists()
              and title_txt is not None and title_txt.exists())
    if banner:
        bar_h, th_w, th_h, pad = 168, 214, 120, 24
        show = f"enable='lt(t,{INTRO_SECONDS})'"
        font = (f"fontfile='{INTRO_FONT.replace(':', chr(92) + ':')}':"
                if Path(INTRO_FONT).exists() else "")
        fc = (
            f"[0:v]{base}[b0];"
            f"[b0]drawbox=x=0:y=0:w=iw:h={bar_h}:color=black@0.55:"
            f"t=fill:{show}[b1];"
            f"[1:v]scale={th_w}:{th_h}[th];"
            f"[b1][th]overlay=x={pad}:y={(bar_h - th_h) // 2}:{show}[b2];"
            f"[b2]drawtext={font}textfile={title_txt.name}:fontsize=34:"
            f"fontcolor=white:line_spacing=10:x={pad * 2 + th_w}:"
            f"y=({bar_h}-th)/2:{show}[b3];"
            f"[b3]{tail}format=yuv420p[vout]"
        )
        cmd += ["-i", thumb.name, "-filter_complex", fc,
                "-map", "[vout]", "-map", "0:a?"]
    else:
        cmd += ["-vf", f"{base},{tail}format=yuv420p"]

    if t_limit is not None:
        cmd += ["-t", f"{t_limit:.3f}"]
    cmd += [
        "-c:v", "libx264", "-preset", X264_PRESET,
        "-b:v", VIDEO_BITRATE, "-maxrate", VIDEO_BITRATE, "-bufsize", "9000k",
        "-g", str(FPS * 2), "-keyint_min", str(FPS * 2), "-sc_threshold", "0",
        "-c:a", "aac", "-b:a", AUDIO_BITRATE, "-ar", "44100", "-ac", "2",
        "-f", "mpegts", tmp_out.name,
    ]
    r = run(cmd, cwd=str(workdir))
    if r.returncode != 0 or not tmp_out.exists():
        log(f"ERRO na normalização de {src.name}: {r.stderr.strip()[:400]}")
        tmp_out.unlink(missing_ok=True)
        return False
    shutil.move(str(tmp_out), str(dst))
    return True


def prepare_bumper() -> None:
    """(Re)normaliza a vinheta quando o arquivo fonte é novo ou mudou."""
    if not BUMPER_SOURCE.exists():
        if not BUMPER_TS.exists():
            log(f"AVISO: '{BUMPER_SOURCE_NAME}' não encontrado — "
                "o loop rodará SEM vinheta entre os vídeos.")
        return
    if BUMPER_TS.exists() and BUMPER_TS.stat().st_mtime >= BUMPER_SOURCE.stat().st_mtime:
        return
    log("Normalizando a vinheta...")
    if normalize(BUMPER_SOURCE, BUMPER_TS):
        log("Vinheta pronta.")
        restart_event.set()


# ------------------------- POST NO X ----------------------------------

def make_endcard(video_id: str, out_ts: Path) -> bool:
    """Tela final de 10s para posts cortados no X: fundo escuro, thumbnail,
    'Veja o vídeo completo no YouTube' e o link escrito na imagem."""
    thumb = VIDEO_DIR / f"{video_id}.jpg"
    font = (f"fontfile='{INTRO_FONT.replace(':', chr(92) + ':')}':"
            if Path(INTRO_FONT).exists() else "")
    cmd = ["ffmpeg", "-y", "-f", "lavfi",
           "-i", f"color=c=0x111111:s={WIDTH}x{HEIGHT}:r={FPS}:d=10"]
    if thumb.exists():
        cmd += ["-loop", "1", "-t", "10", "-i", str(thumb)]
        fc = ("[1:v]scale=1120:630[t];"
              "[0:v][t]overlay=(W-w)/2:(H-h)/2+20[v1];")
        base, a_idx = "[v1]", 2
    else:
        fc, base, a_idx = "", "[0:v]", 1
    cmd += ["-f", "lavfi", "-t", "10", "-i", "anullsrc=r=44100:cl=stereo"]
    fc += (f"{base}drawtext={font}text='Veja o vídeo completo no YouTube':"
           f"fontsize=58:fontcolor=white:x=(w-text_w)/2:y=80[v2];"
           f"[v2]drawtext={font}text='O link está no primeiro reply':"
           f"fontsize=42:fontcolor=0xCCCCCC:x=(w-text_w)/2:y=165[v3];"
           f"[v3]drawtext={font}text='youtu.be/{video_id}':fontsize=48:"
           f"fontcolor=0x3EA6FF:x=(w-text_w)/2:y=h-150,format=yuv420p[vout]")
    cmd += ["-filter_complex", fc, "-map", "[vout]", "-map", f"{a_idx}:a",
            "-c:v", "libx264", "-preset", X264_PRESET,
            "-b:v", VIDEO_BITRATE, "-maxrate", VIDEO_BITRATE,
            "-bufsize", "9000k",
            "-g", str(FPS * 2), "-keyint_min", str(FPS * 2),
            "-sc_threshold", "0",
            "-c:a", "aac", "-b:a", AUDIO_BITRATE, "-ar", "44100", "-ac", "2",
            "-f", "mpegts", str(out_ts)]
    r = run(cmd)
    if r.returncode != 0 or not out_ts.exists():
        log(f"AVISO: falha ao gerar a tela final "
            f"({r.stderr.strip()[:200]}) — cortando sem aviso.")
        return False
    return True


def build_x_mp4(video_id: str, limit: float):
    """Monta o mp4 para o X. Se precisar cortar, os últimos 10s viram a tela
    'Veja o vídeo completo no YouTube'. Retorna (mp4, duração) ou None."""
    src = VIDEO_DIR / f"{video_id}.ts"
    mp4 = TMP_DIR / f"{video_id}_x.mp4"
    TMP_DIR.mkdir(exist_ok=True)
    dur = video_duration(src) or 0
    work, eff_dur, aux = src, dur, []

    if limit and dur > limit:
        log(f"X: {video_id} tem {dur:.0f}s — cortando para {limit:.0f}s.")
        main = TMP_DIR / f"{video_id}_xmain.ts"
        card = TMP_DIR / f"{video_id}_xcard.ts"
        comb = TMP_DIR / f"{video_id}_xcomb.ts"
        lst = TMP_DIR / f"{video_id}_xlist.txt"
        aux = [main, card, comb, lst]
        has_card = X_ENDCARD and make_endcard(video_id, card)
        cut_t = max(limit - 10, 1) if has_card else limit
        r = run(["ffmpeg", "-y", "-i", str(src), "-t", f"{cut_t:.3f}",
                 "-c", "copy", str(main)])
        if r.returncode != 0 or not main.exists():
            log(f"ERRO ao cortar {video_id} para o X: "
                f"{r.stderr.strip()[:200]}")
            for f in aux:
                f.unlink(missing_ok=True)
            return None
        work = main
        if has_card:
            lst.write_text(f"file '{main.as_posix()}'\n"
                           f"file '{card.as_posix()}'\n", encoding="utf-8")
            r = run(["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                     "-i", str(lst), "-c", "copy", str(comb)])
            if r.returncode == 0 and comb.exists():
                work = comb
        eff_dur = limit

    # cabe no teto de tamanho da API? senão, recomprime em 720p para caber
    budget = X_TARGET_SIZE_MB * 1024 * 1024
    cmd = ["ffmpeg", "-y", "-i", str(work)]
    if work.stat().st_size <= budget:
        cmd += ["-c", "copy", "-bsf:a", "aac_adtstoasc"]
    else:
        v_kbps = max(400, int(budget * 8 / 1000 / eff_dur) - 128)
        log(f"X: {video_id} é grande demais — recomprimindo em 720p @ "
            f"{v_kbps}k para caber em {X_TARGET_SIZE_MB} MB "
            "(leva alguns minutos).")
        cmd += ["-vf", "scale=1280:720", "-c:v", "libx264",
                "-preset", X264_PRESET, "-b:v", f"{v_kbps}k",
                "-maxrate", f"{v_kbps}k", "-bufsize", f"{v_kbps * 2}k",
                "-c:a", "aac", "-b:a", "128k"]
    cmd += ["-movflags", "+faststart", str(mp4)]
    r = run(cmd)
    for f in aux:
        f.unlink(missing_ok=True)
    if r.returncode != 0 or not mp4.exists():
        log(f"ERRO ao preparar mp4 para o X: {r.stderr.strip()[:300]}")
        return None
    return mp4, eff_dur


def post_to_x(video_id: str, title: str) -> None:
    """Posta o vídeo processado no X. Falhas não interrompem o pipeline."""
    if not X_ENABLED:
        return
    try:
        import tweepy
    except ImportError:
        log("AVISO: tweepy não instalado (python -m pip install tweepy) — "
            "post no X pulado.")
        return

    src = VIDEO_DIR / f"{video_id}.ts"
    dur = video_duration(src) or 0
    if dur > X_MAX_VIDEO_SECONDS and X_IF_TOO_LONG == "skip":
        log(f"X: {video_id} tem {dur:.0f}s (limite {X_MAX_VIDEO_SECONDS}s) "
            "— post pulado.")
        return

    built = build_x_mp4(video_id, X_MAX_VIDEO_SECONDS)
    if not built:
        return
    mp4, eff_dur = built
    try:
        auth = tweepy.OAuth1UserHandler(X_API_KEY, X_API_SECRET,
                                        X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET)
        api = tweepy.API(auth)
        client = tweepy.Client(
            consumer_key=X_API_KEY, consumer_secret=X_API_SECRET,
            access_token=X_ACCESS_TOKEN,
            access_token_secret=X_ACCESS_TOKEN_SECRET)
        text = X_TEXT_TEMPLATE.format(
            title=title, url=f"https://youtu.be/{video_id}")[:280]

        def send(path, category):
            log(f"X: enviando vídeo {video_id} ({category})...")
            media = api.media_upload(str(path), chunked=True,
                                     media_category=category)
            resp = client.create_tweet(text=text, media_ids=[media.media_id])
            if X_REPLY_TEMPLATE:
                reply = X_REPLY_TEMPLATE.format(
                    title=title, url=f"https://youtu.be/{video_id}")[:280]
                try:
                    client.create_tweet(text=reply,
                                        in_reply_to_tweet_id=resp.data["id"])
                    log("X: reply com o link publicado.")
                except Exception as e:
                    log(f"AVISO: post ok, mas o reply com o link falhou: {e}")

        # vídeos >2min via API exigem a categoria amplify_video (mesmo Premium)
        category = "amplify_video" if eff_dur > 140 else "tweet_video"
        try:
            send(mp4, category)
            log(f"X: postado {video_id}.")
        except Exception as e:
            m = re.search(r"longer than (\d+) minutes?", str(e))
            if category == "amplify_video" and m:
                new_limit = int(m.group(1)) * 60 - 1  # 1s de margem
                log(f"X: a conta aceita no máximo {m.group(1)} min via API — "
                    f"reenviando cortado em {new_limit}s.")
                built = build_x_mp4(video_id, new_limit)
                if not built:
                    raise
                mp4, eff2 = built
                send(mp4, "amplify_video" if eff2 > 140 else "tweet_video")
                log(f"X: postado {video_id} (cortado em {new_limit}s).")
            else:
                raise
    except Exception as e:
        log(f"ERRO ao postar no X ({video_id}): {e}")
    finally:
        mp4.unlink(missing_ok=True)


# ---------------------------- MONITOR ---------------------------------

def latest_video_ids() -> list:
    """IDs dos últimos MAX_VIDEOS uploads do canal, do mais novo ao mais antigo."""
    r = run(ytdlp_cmd() + [
        "--flat-playlist", "--playlist-end", str(MAX_VIDEOS),
        "--print", "%(id)s", CHANNEL_URL,
    ])
    if r.returncode != 0:
        log(f"ERRO ao consultar o canal: {r.stderr.strip()[:400]}")
        return []
    return [l.strip() for l in r.stdout.splitlines() if l.strip()]


def download_and_normalize(video_id: str):
    """Baixa, corta o final, legenda e converte para .ts padronizado.
    Retorna (ok, título)."""
    out_file = VIDEO_DIR / f"{video_id}.ts"
    if out_file.exists():
        return True, None

    TMP_DIR.mkdir(exist_ok=True)
    raw = TMP_DIR / f"{video_id}.mp4"
    srt = TMP_DIR / f"{video_id}.srt"

    log(f"Baixando {video_id}...")
    r = run(ytdlp_cmd() + [
        "-f", "bv*[height<=1080]+ba/b[height<=1080]/b",
        "--merge-output-format", "mp4",
        "--write-info-json",
        "--write-thumbnail", "--convert-thumbnails", "jpg",
        "-o", str(raw),
        f"https://www.youtube.com/watch?v={video_id}",
    ])
    if r.returncode != 0 or not raw.exists():
        log(f"ERRO no download de {video_id}: {r.stderr.strip()[:400]}")
        return False, None

    # título lido do .info.json (sempre UTF-8 — o stdout do yt-dlp.exe sai na
    # codificação regional do Windows e corrompe acentos)
    title = ""
    info = TMP_DIR / f"{video_id}.info.json"
    if info.exists():
        try:
            title = json.loads(info.read_text(encoding="utf-8")).get("title", "")
        except Exception as e:
            log(f"AVISO: falha ao ler o título de {video_id}: {e}")
        info.unlink(missing_ok=True)

    # banner de abertura: thumbnail (baixada acima) + título em arquivo
    thumb = TMP_DIR / f"{video_id}.jpg"
    title_txt = TMP_DIR / f"{video_id}.txt"
    if INTRO_BANNER and title:
        wrapped = textwrap.wrap(title, width=80)[:3]
        title_txt.write_text("\n".join(wrapped), encoding="utf-8")

    # corte dos segundos finais
    t_limit = None
    dur = video_duration(raw)
    if dur and dur > CUT_END_SECONDS + 5:
        t_limit = dur - CUT_END_SECONDS
    elif dur:
        log(f"AVISO: {video_id} é muito curto ({dur:.0f}s) — não vou cortar o final.")

    # legendas
    srt_ok = None
    if BURN_SUBTITLES:
        try:
            log(f"Transcrevendo {video_id} (Whisper)...")
            if generate_srt(raw, srt):
                srt_ok = srt
            else:
                log(f"AVISO: nenhuma fala detectada em {video_id} — sem legenda.")
        except Exception as e:
            log(f"AVISO: falha ao legendar {video_id} ({e}) — seguindo sem legenda.")

    log(f"Normalizando {video_id}...")
    ok = normalize(raw, out_file, t_limit=t_limit, srt=srt_ok,
                   thumb=thumb, title_txt=title_txt)
    raw.unlink(missing_ok=True)
    srt.unlink(missing_ok=True)
    title_txt.unlink(missing_ok=True)
    if ok and thumb.exists():
        # guarda a thumbnail para a tela final dos posts cortados no X
        shutil.move(str(thumb), str(VIDEO_DIR / f"{video_id}.jpg"))
    else:
        thumb.unlink(missing_ok=True)
    if ok:
        log(f"Pronto: {out_file.name}")
    return ok, title


def build_playlist_text(ids_newest_first: list) -> str:
    """Playlist em ordem cronológica, com a vinheta entre os vídeos."""
    available = [i for i in ids_newest_first if (VIDEO_DIR / f"{i}.ts").exists()]
    bumper = f"file '{BUMPER_TS.as_posix()}'" if BUMPER_TS.exists() else None
    lines = []
    for i in reversed(available):
        lines.append(f"file '{(VIDEO_DIR / f'{i}.ts').as_posix()}'")
        if bumper:
            lines.append(bumper)  # também toca entre o último e o primeiro do loop
    return "\n".join(lines) + "\n" if lines else ""


def sync_playlist(ids_newest_first: list) -> None:
    """Se a playlist desejada mudou, grava em playlist_new.txt e pede reinício.
    O streamer troca o arquivo só com o FFmpeg parado — no Windows não dá para
    substituir o playlist.txt enquanto o FFmpeg o mantém aberto."""
    desired = build_playlist_text(ids_newest_first)
    if not desired:
        return
    if PLAYLIST_PENDING.exists():
        current = PLAYLIST_PENDING.read_text(encoding="utf-8")
    elif PLAYLIST.exists():
        current = PLAYLIST.read_text(encoding="utf-8")
    else:
        current = ""
    if desired != current:
        tmp = PLAYLIST_PENDING.with_suffix(".tmp")
        tmp.write_text(desired, encoding="utf-8")
        tmp.replace(PLAYLIST_PENDING)
        restart_event.set()


def prune_old(keep_ids: list) -> None:
    keep = {f"{i}.ts" for i in keep_ids} | {f"{i}.jpg" for i in keep_ids}
    for f in list(VIDEO_DIR.glob("*.ts")) + list(VIDEO_DIR.glob("*.jpg")):
        if f.name not in keep:
            log(f"Removendo antigo: {f.name}")
            f.unlink(missing_ok=True)


def watcher() -> None:
    VIDEO_DIR.mkdir(exist_ok=True)
    while True:
        try:
            prepare_bumper()
            ids = latest_video_ids()
            if ids:
                new = [i for i in ids if not (VIDEO_DIR / f"{i}.ts").exists()]
                for k, vid in enumerate(new):
                    if k > 0:
                        time.sleep(DOWNLOAD_PAUSE)  # não martelar o YouTube
                    ok, title = download_and_normalize(vid)
                    if ok:
                        # entra no ar imediatamente, sem esperar o lote todo
                        sync_playlist(ids)
                        log(f"{vid} entrou no loop.")
                        post_to_x(vid, title or "")
                prune_old(ids)
                sync_playlist(ids)  # cobre remoções e recupera atualizações perdidas
        except Exception as e:
            log(f"ERRO inesperado no monitor: {e}")
        time.sleep(CHECK_INTERVAL)


# ---------------------------- STREAMER --------------------------------

def apply_pending_playlist() -> None:
    """Troca playlist_new.txt -> playlist.txt (chamar só com o FFmpeg parado)."""
    if not PLAYLIST_PENDING.exists():
        return
    for _ in range(10):
        try:
            PLAYLIST_PENDING.replace(PLAYLIST)
            log("Playlist atualizada.")
            return
        except PermissionError:
            time.sleep(1)  # Windows pode segurar o arquivo por um instante
    log("AVISO: não consegui atualizar playlist.txt (arquivo em uso); "
        "tento de novo no próximo reinício.")


def streamer() -> None:
    while True:
        apply_pending_playlist()
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
    for tool in ("ffmpeg", "ffprobe", "yt-dlp"):
        if shutil.which(tool) is None:
            sys.exit(f"ERRO: '{tool}' não encontrado no PATH. Veja o README.md.")
    if BURN_SUBTITLES:
        try:
            import faster_whisper  # noqa: F401
        except ImportError as e:
            sys.exit(
                f"ERRO ao importar o faster-whisper: {e}\n\n"
                f"Python em uso: {sys.executable}\n"
                "Instale no MESMO Python que executa este script:\n"
                "  python -m pip install faster-whisper\n"
                "(ou desative as legendas com BURN_SUBTITLES = False no config.py)")
    if X_ENABLED:
        try:
            import tweepy  # noqa: F401
        except ImportError as e:
            sys.exit(f"ERRO ao importar o tweepy: {e}\n"
                     f"Python em uso: {sys.executable}\n"
                     "Instale com:  python -m pip install tweepy\n"
                     "(ou desative o X com X_ENABLED = False no config.py)")
        if not all([X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN,
                    X_ACCESS_TOKEN_SECRET]):
            sys.exit("ERRO: X_ENABLED = True, mas faltam credenciais "
                     "X_API_KEY / X_API_SECRET / X_ACCESS_TOKEN / "
                     "X_ACCESS_TOKEN_SECRET no config.py.")


def main() -> None:
    check_tools()
    log(f"Canal: {CHANNEL_URL}")
    log(f"Loop com os últimos {MAX_VIDEOS} vídeos | verificação a cada {CHECK_INTERVAL}s")
    log(f"Corte final: {CUT_END_SECONDS}s | Legendas: "
        f"{'ativadas (' + WHISPER_MODEL + ')' if BURN_SUBTITLES else 'desativadas'}")
    threading.Thread(target=watcher, daemon=True, name="watcher").start()
    try:
        streamer()
    except KeyboardInterrupt:
        log("Encerrado pelo usuário.")


if __name__ == "__main__":
    main()
