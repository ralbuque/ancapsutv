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

import itertools
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import textwrap
import threading
import time
from datetime import datetime, timedelta, timezone
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
# vídeos members-only entram e saem da listagem e fazem a borda do top-N
# oscilar; a margem evita apagar/reprocessar vídeos nessa flutuação
PRUNE_MARGIN = _get("PRUNE_MARGIN", 3)
# a janela de busca precisa atravessar filas longas de vídeos exclusivos
# (já houve 26 agendados) para o ciclo continuar com 20 vídeos públicos
FETCH_WINDOW_EXTRA = _get("FETCH_WINDOW_EXTRA", 30)
CHECK_INTERVAL = _get("CHECK_INTERVAL", 300)
BUMPER_SOURCE_NAME = _get("BUMPER_SOURCE_NAME", "vinheta.mp4")
CUT_END_SECONDS = _get("CUT_END_SECONDS", 10)
BURN_SUBTITLES = _get("BURN_SUBTITLES", True)
WHISPER_MODEL = _get("WHISPER_MODEL", "small")
WHISPER_THREADS = _get("WHISPER_THREADS", 2)  # limita a CPU da transcrição
SUBTITLE_STYLE = _get("SUBTITLE_STYLE",
                      "FontName=Arial,FontSize=16,PrimaryColour=&H00FFFFFF,"
                      "OutlineColour=&H00000000,BorderStyle=1,Outline=2,"
                      "Shadow=0,MarginV=60")  # 60 ≈ 225px do rodapé em 1080p
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

# Instagram/TikTok via Ayrshare: versão vertical (1080x1920) de cada vídeo
# novo do canal principal, com legenda re-queimada e 1º comentário com o link
AYRSHARE_ENABLED = _get("AYRSHARE_ENABLED", False)
AYRSHARE_API_KEY = _get("AYRSHARE_API_KEY", "")
AYR_PLATFORMS = _get("AYR_PLATFORMS", ["instagram", "tiktok"])
AYR_CAPTION_TEMPLATE = _get("AYR_CAPTION_TEMPLATE", "{title}")
AYR_COMMENT_TEMPLATE = _get("AYR_COMMENT_TEMPLATE",
                            "Veja o vídeo completo em {url}")
AYR_MAX_SECONDS = _get("AYR_MAX_SECONDS", 1190)  # <20min (teto do Reels)
VERT_CROP = _get("VERT_CROP", 0.667)      # mantém os 2/3 esquerdos do vídeo
VERT_BG = _get("VERT_BG", "0xD35A25")     # cor das faixas (laranja do mock)
VERT_TEXTCOL = _get("VERT_TEXTCOL", "0x1F1F1F")
VERT_BITRATE = _get("VERT_BITRATE", "3500k")
VERT_CHANNEL_TEXT = _get("VERT_CHANNEL_TEXT", "Canal ANCAPSU")
# Sem CTA na imagem: o Instagram penaliza vídeos com "veja o resto em..."
# (o link do YouTube vai só no primeiro comentário)
VERT_CTA_LINES = _get("VERT_CTA_LINES", [])
VERT_VIDEO_Y = _get("VERT_VIDEO_Y", 770)  # posição vertical do vídeo
VERT_SUB_STYLE = _get("VERT_SUB_STYLE",
                      "FontName=Arial,FontSize=16,PrimaryColour=&H00FFFFFF,"
                      "OutlineColour=&H00000000,BorderStyle=1,Outline=2,"
                      "Shadow=0,MarginV=30")

# Bloco promocional do outro canal (alterna com a vinheta nos intervalos):
# chamada.mp4 -> um short do outro canal -> continuidade.mp4
PROMO_ENABLED = _get("PROMO_ENABLED", False)
PROMO_CHANNEL_URL = _get("PROMO_CHANNEL_URL", "")   # ex.: .../@canal/shorts
PROMO_INTRO_NAME = _get("PROMO_INTRO_NAME", "chamada.mp4")
PROMO_OUTRO_NAME = _get("PROMO_OUTRO_NAME", "continuidade.mp4")
PROMO_MAX_SHORTS = _get("PROMO_MAX_SHORTS", 10)  # tamanho do rodízio de shorts
PROMO_EVERY = _get("PROMO_EVERY", 2)  # 2 = alterna vinheta/promo nos intervalos

# Canais convidados: o vídeo mais recente de cada um entra no ciclo numa
# posição fixa, com vinhetas próprias de entrada/saída e X em conta separada.
# Lista de dicts — veja config.example.py para o formato.
GUEST_CHANNELS = _get("GUEST_CHANNELS", [])
X_TARGET_SIZE_MB = _get("X_TARGET_SIZE_MB", 500)  # teto da API é ~512 MB

# Banner de abertura (thumbnail + título no topo da tela)
INTRO_BANNER = _get("INTRO_BANNER", True)
INTRO_SECONDS = _get("INTRO_SECONDS", 30)
INTRO_FONT = _get("INTRO_FONT", "C:/Windows/Fonts/arialbd.ttf")
INTRO_LINE_HEIGHT = _get("INTRO_LINE_HEIGHT", 44)  # altura de linha do título

# Lower third (sobreposto na transmissão — exige re-codificação contínua!)
LOWER_THIRD = _get("LOWER_THIRD", False)
LOWER_THIRD_NAME = _get("LOWER_THIRD_NAME", "Peter Turguniev")
LOWER_THIRD_CHANNEL = _get("LOWER_THIRD_CHANNEL", "ANCAPSU")
LOWER_THIRD_PRESET = _get("LOWER_THIRD_PRESET", "veryfast")
TICKER_COUNT = _get("TICKER_COUNT", 3)      # quantos títulos ciclam na barra
TICKER_SECONDS = _get("TICKER_SECONDS", 8)  # segundos que cada título fica
ALERT_MINUTES = _get("ALERT_MINUTES", 10)   # duração do aviso de vídeo novo
# Arte: PNG transparente com o emblema (nome+canal); se existir, substitui
# os textos da esquerda e as barras começam depois dele
LOWER_THIRD_BADGE = _get("LOWER_THIRD_BADGE", "badge.png")
# A barra atravessa a tela inteira (por trás do emblema); os espaços iniciais
# empurram o texto para depois do emblema — ajuste fino se o PNG mudar de largura
TICKER_LEAD_SPACES = _get("TICKER_LEAD_SPACES", 56)
TICKER_PREFIX = _get("TICKER_PREFIX", "")
ALERT_PREFIX = _get("ALERT_PREFIX", "LANÇADO: ")
TICKER_BOX = _get("TICKER_BOX", "0xF2B705")     # barra dourada
TICKER_TEXTCOL = _get("TICKER_TEXTCOL", "0x1F1F1F")
ALERT_BOX = _get("ALERT_BOX", "0xEF7B6D")       # barra salmão (alerta)
ALERT_TEXTCOL = _get("ALERT_TEXTCOL", "0x1F1F1F")

# Barra "AGORA" (título do item em exibição, acima da barra de títulos)
AGORA_ENABLED = _get("AGORA_ENABLED", True)
AGORA_PREFIX = _get("AGORA_PREFIX", "AGORA: ")
AGORA_BUMPER_LABEL = _get("AGORA_BUMPER_LABEL", "Vinheta")  # texto nas vinhetas
AGORA_SHORT_TEMPLATE = _get("AGORA_SHORT_TEMPLATE",
                            "Short do palácio assombrado {title}")
AGORA_FONTSIZE = _get("AGORA_FONTSIZE", 26)
# fonte menor -> espaços mais estreitos: recuo próprio para alinhar com a barra
AGORA_LEAD_SPACES = _get("AGORA_LEAD_SPACES", 64)
AGORA_LIFT = _get("AGORA_LIFT", 76)  # altura da barra AGORA (maior = mais alta)
AGORA_BOX = _get("AGORA_BOX", "0xD9D9D9")       # barra cinza clara
AGORA_TEXTCOL = _get("AGORA_TEXTCOL", "0x1F1F1F")
# o espectador vê a live com atraso; o AGORA desconta esse atraso para trocar
# junto com o que aparece na tela (ajuste fino: aumente se o AGORA "adianta")
AGORA_DELAY = _get("AGORA_DELAY", 20)
RESTART_COOLDOWN = _get("RESTART_COOLDOWN", 90)  # seg. mínimos entre reinícios
STALL_RESTART = _get("STALL_RESTART", 120)  # seg. sem avanço => religa (0=off)

# Relógio (escrito pelo script já convertido para o fuso configurado)
LT_CLOCK = _get("LT_CLOCK", True)
CLOCK_UTC_OFFSET = _get("CLOCK_UTC_OFFSET", -3)  # Brasília = UTC-3
CLOCK_COLOR = _get("CLOCK_COLOR", "0xF2B705")
CLOCK_DATE_X = _get("CLOCK_DATE_X", 190)
CLOCK_DATE_Y = _get("CLOCK_DATE_Y", "h-110")
CLOCK_DATE_SIZE = _get("CLOCK_DATE_SIZE", 30)
CLOCK_TIME_X = _get("CLOCK_TIME_X", 190)
CLOCK_TIME_Y = _get("CLOCK_TIME_Y", "h-64")
CLOCK_TIME_SIZE = _get("CLOCK_TIME_SIZE", 44)

# ======================================================================

BASE_DIR = Path(__file__).resolve().parent
VIDEO_DIR = BASE_DIR / "videos"
TMP_DIR = BASE_DIR / "tmp"
PLAYLIST = BASE_DIR / "playlist.txt"
PLAYLIST_PENDING = BASE_DIR / "playlist_new.txt"
BUMPER_SOURCE = BASE_DIR / BUMPER_SOURCE_NAME
BUMPER_TS = BASE_DIR / "bumper.ts"
SHORTS_DIR = BASE_DIR / "shorts"
GUESTS_DIR = BASE_DIR / "guests"


def _slug(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").lower()
    return s or "canal"
PROMO_INTRO_SRC = BASE_DIR / PROMO_INTRO_NAME
PROMO_OUTRO_SRC = BASE_DIR / PROMO_OUTRO_NAME
PROMO_INTRO_TS = BASE_DIR / "chamada.ts"
PROMO_OUTRO_TS = BASE_DIR / "continuidade.ts"

# parâmetros de codificação compartilhados (todos os .ts precisam ser idênticos
# para a emenda por cópia direta funcionar)
def _encode_args() -> list:
    return ["-c:v", "libx264", "-preset", X264_PRESET,
            "-b:v", VIDEO_BITRATE, "-maxrate", VIDEO_BITRATE,
            "-bufsize", "9000k",
            "-g", str(FPS * 2), "-keyint_min", str(FPS * 2),
            "-sc_threshold", "0",
            "-c:a", "aac", "-b:a", AUDIO_BITRATE, "-ar", "44100", "-ac", "2",
            "-f", "mpegts"]
COOKIES_FILE = BASE_DIR / "cookies.txt"
VOCAB_FILE = BASE_DIR / "vocabulario.txt"    # nomes próprios e siglas, 1 por linha
FIXES_FILE = BASE_DIR / "correcoes.txt"      # linhas "errado => certo"
TICKER_FILE = BASE_DIR / "ticker.txt"        # texto da barra (lido pelo ffmpeg)
ALERT_FILE = BASE_DIR / "alert.txt"          # texto do alerta "NOVO VÍDEO"
AGORA_FILE = BASE_DIR / "agora.txt"          # título do item em exibição
CLOCK_DATE_FILE = BASE_DIR / "data.txt"      # data (fuso convertido)
CLOCK_TIME_FILE = BASE_DIR / "hora.txt"      # hora (fuso convertido)
TITLES_FILE = BASE_DIR / "titles.json"       # títulos recentes p/ o ticker
SCHEDULE_FILE = BASE_DIR / "schedule.json"       # grade de exibição ativa
SCHEDULE_PENDING = BASE_DIR / "schedule_new.json"
DURATIONS_FILE = BASE_DIR / "durations.json"     # cache de durações
RESUME_FILE = BASE_DIR / "resume.json"           # item em exibição (retomada)
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
_stream_proc = None                 # ffmpeg da transmissão em execução
_state_lock = threading.Lock()      # protege titles.json (ler-modificar-gravar)
_pl_lock = threading.Lock()         # protege a escrita da playlist/grade
_last_ids = []                      # última lista de IDs do canal principal

# Fila de processamento pesado: a detecção enfileira, um worker único executa
# (serial de propósito — evita várias transcrições disputando CPU)
_job_q = queue.PriorityQueue()
_job_seq = itertools.count()
_queued = set()
_queued_lock = threading.Lock()


def enqueue_job(kind: str, vid: str, cfg: dict = None, prio: int = 1) -> bool:
    """Enfileira um trabalho, sem duplicar o que já está na fila/execução.
    Retorna True se realmente entrou na fila."""
    slug = _slug(cfg.get("name", "")) if cfg else ""
    key = f"{kind}:{slug}:{vid}"
    with _queued_lock:
        if key in _queued:
            return False
        _queued.add(key)
    _job_q.put((prio, next(_job_seq),
                {"kind": kind, "id": vid, "cfg": cfg, "key": key}))
    return True
_play = {"pos": 0.0, "at": 0.0}     # posição de exibição (via -progress)


def _progress_reader(proc) -> None:
    """Lê o -progress do ffmpeg e atualiza a posição de exibição.
    Periodicamente grava a retomada (cobre quedas e Ctrl+C)."""
    n = 0
    try:
        for line in proc.stdout:
            if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                try:
                    _play["pos"] = int(line.strip().split("=", 1)[1]) / 1_000_000
                    _play["at"] = time.time()
                except ValueError:
                    continue
                n += 1
                if n % 30 == 0:  # ~a cada 15s (progress reporta 2 linhas/s)
                    save_resume()
    except Exception:
        pass


def kill_stream() -> None:
    """Encerra o ffmpeg da transmissão (para ele não sobreviver ao script)."""
    p = _stream_proc
    if p is not None and p.poll() is None:
        p.terminate()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# Prioridades (Windows): a transmissão roda ACIMA do normal e todo o
# processamento em lote ABAIXO — quando disputam CPU, a live vence.
_PRIO_LOW = (subprocess.BELOW_NORMAL_PRIORITY_CLASS
             if os.name == "nt" else 0)
_PRIO_HIGH = (subprocess.ABOVE_NORMAL_PRIORITY_CLASS
              if os.name == "nt" else 0)


def run(cmd: list, **kw) -> subprocess.CompletedProcess:
    # PYTHONUTF8 força o yt-dlp a emitir UTF-8 no Windows (senão títulos com
    # acentos saem na codificação regional e viram caracteres inválidos)
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env,
                          creationflags=_PRIO_LOW, **kw)


def safe_unlink(p: Path) -> None:
    """Apaga sem estourar erro se o Windows ainda segura o arquivo aberto."""
    try:
        p.unlink(missing_ok=True)
    except OSError:
        pass


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
                                      compute_type="int8",
                                      cpu_threads=WHISPER_THREADS)
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
              thumb: Path = None, title_lines: list = None) -> bool:
    """Converte src para o formato padrão .ts em dst.

    t_limit: duração máxima em segundos (corta o final).
    srt: arquivo .srt para queimar no vídeo.
    thumb/title_lines: se presentes, desenha o banner de abertura (faixa no
        topo com a thumbnail e o título — um arquivo de texto POR LINHA, com
        posição vertical explícita: o espaçamento multilinha do drawtext
        varia entre versões do FFmpeg) nos primeiros INTRO_SECONDS segundos.
    Todos os arquivos auxiliares devem estar na MESMA pasta de src — o ffmpeg
    roda com cwd nessa pasta para evitar problemas de escape de caminhos do
    Windows nos filtros.
    """
    workdir = src.parent
    base = (f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
            f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2,fps={FPS}")
    tail = (f"subtitles={srt.name}:force_style='{SUBTITLE_STYLE}',"
            if srt is not None else "")

    # "./" evita que nomes começando com "-" (IDs do YouTube) virem opções
    tmp_out = workdir / (dst.stem + ".part.ts")
    cmd = ["ffmpeg", "-y", "-i", f"./{src.name}"]

    banner = (INTRO_BANNER and thumb is not None and thumb.exists()
              and title_lines
              and all(lf.exists() for lf in title_lines))
    if banner:
        bar_h, th_w, th_h, pad = 168, 214, 120, 24
        lh = INTRO_LINE_HEIGHT
        show = f"enable='lt(t,{INTRO_SECONDS})'"
        font = (f"fontfile='{INTRO_FONT.replace(':', chr(92) + ':')}':"
                if Path(INTRO_FONT).exists() else "")
        n = len(title_lines)
        y0 = max(10, (bar_h - lh * n + (lh - 34)) // 2)
        fc = (f"[0:v]{base}[b0];"
              f"[b0]drawbox=x=0:y=0:w=iw:h={bar_h}:color=black@0.55:"
              f"t=fill:{show}[b1];"
              f"[1:v]scale={th_w}:{th_h}[th];"
              f"[b1][th]overlay=x={pad}:y={(bar_h - th_h) // 2}:{show}[b2]")
        cur = "b2"
        for i, lf in enumerate(title_lines):
            fc += (f";[{cur}]drawtext={font}textfile={lf.name}:fontsize=34:"
                   f"fontcolor=white:x={pad * 2 + th_w}:"
                   f"y={y0 + i * lh}:{show}[t{i}]")
            cur = f"t{i}"
        fc += f";[{cur}]{tail}format=yuv420p[vout]"
        cmd += ["-i", f"./{thumb.name}", "-filter_complex", fc,
                "-map", "[vout]", "-map", "0:a?"]
    else:
        cmd += ["-vf", f"{base},{tail}format=yuv420p"]

    if t_limit is not None:
        cmd += ["-t", f"{t_limit:.3f}"]
    cmd += _encode_args() + [f"./{tmp_out.name}"]
    r = run(cmd, cwd=str(workdir))
    if r.returncode != 0 or not tmp_out.exists():
        log(f"ERRO na normalização de {src.name}: {r.stderr.strip()[-400:]}")
        tmp_out.unlink(missing_ok=True)
        return False
    shutil.move(str(tmp_out), str(dst))
    return True


def normalize_short(src: Path, dst: Path) -> bool:
    """Converte um short vertical para o formato da live, centralizado sobre
    o próprio vídeo desfocado preenchendo as laterais."""
    workdir = src.parent
    tmp_out = workdir / (dst.stem + ".part.ts")
    fc = (f"[0:v]split=2[a][b];"
          f"[a]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
          f"crop={WIDTH}:{HEIGHT},boxblur=20:5[bg];"
          f"[b]scale=-2:{HEIGHT}[fg];"
          f"[bg][fg]overlay=(W-w)/2:(H-h)/2,fps={FPS},format=yuv420p[vout]")
    cmd = (["ffmpeg", "-y", "-i", f"./{src.name}", "-filter_complex", fc,
            "-map", "[vout]", "-map", "0:a?"]
           + _encode_args() + [f"./{tmp_out.name}"])
    r = run(cmd, cwd=str(workdir))
    if r.returncode != 0 or not tmp_out.exists():
        log(f"ERRO na normalização do short {src.name}: "
            f"{r.stderr.strip()[-300:]}")
        tmp_out.unlink(missing_ok=True)
        return False
    shutil.move(str(tmp_out), str(dst))
    return True


def prepare_static(src: Path, dst: Path, label: str) -> None:
    """(Re)normaliza um vídeo fixo (vinheta/chamada/continuidade) se mudou,
    com legendas queimadas como nos vídeos normais."""
    if not src.exists():
        if not dst.exists():
            log(f"AVISO: '{src.name}' não encontrado — seguindo sem {label}.")
        return
    if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
        return
    srt = src.with_suffix(".srt")
    srt_ok = None
    if BURN_SUBTITLES:
        try:
            log(f"Transcrevendo {label} ({src.name})...")
            if generate_srt(src, srt):
                srt_ok = srt
        except Exception as e:
            log(f"AVISO: falha ao legendar {label} ({e}) — seguindo sem legenda.")
    log(f"Normalizando {label} ({src.name})...")
    if normalize(src, dst, srt=srt_ok):
        log(f"Pronto: {label}.")
        restart_event.set()
    safe_unlink(srt)


def prepare_bumper() -> None:
    prepare_static(BUMPER_SOURCE, BUMPER_TS, "a vinheta")
    if PROMO_ENABLED:
        prepare_static(PROMO_INTRO_SRC, PROMO_INTRO_TS,
                       "a chamada do outro canal")
        prepare_static(PROMO_OUTRO_SRC, PROMO_OUTRO_TS,
                       "a vinheta de continuidade")
    for g in GUEST_CHANNELS:
        slug = _slug(g.get("name", ""))
        if g.get("intro"):
            prepare_static(BASE_DIR / g["intro"],
                           BASE_DIR / f"{slug}_intro.ts",
                           f"a vinheta de entrada de {g.get('name')}")
        if g.get("outro"):
            prepare_static(BASE_DIR / g["outro"],
                           BASE_DIR / f"{slug}_outro.ts",
                           f"a vinheta de saída de {g.get('name')}")


def detect_guests() -> None:
    """Detecção leve: enfileira vídeo novo de cada canal convidado e limpa
    antigos (só depois que o atual está pronto, para não furar a playlist)."""
    for g in GUEST_CHANNELS:
        try:
            name = g.get("name", "?")
            url = g.get("url", "")
            if not url:
                continue
            slug = _slug(name)
            gdir = GUESTS_DIR / slug
            gdir.mkdir(parents=True, exist_ok=True)
            r = run(ytdlp_cmd() + ["--flat-playlist", "--playlist-end", "1",
                                   "--print", "%(id)s", url])
            if r.returncode != 0:
                log(f"ERRO ao consultar {name}: {r.stderr.strip()[-200:]}")
                continue
            ids = [l.strip() for l in r.stdout.splitlines() if l.strip()]
            if not ids:
                continue
            vid = ids[0]
            if not (gdir / f"{vid}.ts").exists():
                if enqueue_job("guest", vid, cfg=g, prio=0):
                    log(f"Vídeo novo em {name}: {vid} — "
                        "na fila de processamento.")
            else:
                # atual pronto: limpa anteriores (nunca os ainda na playlist)
                refs = _referenced_files()
                for f in list(gdir.glob("*.ts")) + list(gdir.glob("*.jpg")):
                    if f.stem != vid and f.name not in refs:
                        log(f"Removendo antigo de {name}: {f.name}")
                        safe_unlink(f)
        except Exception as e:
            log(f"ERRO no canal convidado {g.get('name', '?')}: {e}")


# -------------------- INSTAGRAM/TIKTOK (AYRSHARE) ---------------------

def build_vertical(video_id: str, title: str):
    """Monta a versão vertical 1080x1920 conforme o layout: faixa superior
    (título + thumbnail + chamada), vídeo com o 1/3 direito cortado e legenda
    re-queimada centrada no novo corte, faixa inferior com o canal.
    Usa o mp4 bruto e o .srt mantidos no tmp/. Retorna o mp4 ou None."""
    raw = TMP_DIR / f"{video_id}.mp4"
    srt = TMP_DIR / f"{video_id}.srt"
    thumb = VIDEO_DIR / f"{video_id}.jpg"
    out = TMP_DIR / f"{video_id}_vert.mp4"
    if not raw.exists():
        log(f"Vertical: mp4 bruto de {video_id} não disponível — pulando.")
        return None
    font = (f"fontfile='{INTRO_FONT.replace(':', chr(92) + ':')}':"
            if Path(INTRO_FONT).exists() else "")

    # título em arquivos por linha (mesma técnica do banner)
    tfiles = []
    for i, ln in enumerate(textwrap.wrap(title or "", width=36)[:3]):
        lf = TMP_DIR / f"{video_id}.v{i}.txt"
        lf.write_text(ln, encoding="utf-8")
        tfiles.append(lf)

    subs = (f",subtitles={srt.name}:force_style='{VERT_SUB_STYLE}'"
            if srt.exists() else "")
    fc = (f"color=c={VERT_BG}:s=1080x1920:r={FPS}[bg];"
          f"[0:v]crop=trunc(iw*{VERT_CROP}/2)*2:ih:0:0,"
          f"scale=1080:-2{subs},fps={FPS}[v0];"
          f"[bg][v0]overlay=0:{VERT_VIDEO_Y}:shortest=1[c0]")
    cur = "c0"
    n = 1
    if thumb.exists():
        fc = f"[1:v]scale=900:-2[tb];" + fc.replace("[c0]", "[cpre]")
        fc += f";[cpre][tb]overlay=90:250[c0]"
        n = 2
    for i, lf in enumerate(tfiles):
        fc += (f";[{cur}]drawtext={font}textfile={lf.name}:fontsize=52:"
               f"fontcolor={VERT_TEXTCOL}:x=40:y={40 + i * 66}[d{i}]")
        cur = f"d{i}"
    y = 250
    for i, ln in enumerate(VERT_CTA_LINES):
        txt = ln.replace("'", "").replace(":", "\\:")
        if txt.strip():
            fc += (f";[{cur}]drawtext={font}text='{txt}':fontsize=34:"
                   f"fontcolor={VERT_TEXTCOL}:x=480:y={y}[e{i}]")
            cur = f"e{i}"
        y += 46
    chan = VERT_CHANNEL_TEXT.replace("'", "").replace(":", "\\:")
    fc += (f";[{cur}]drawtext={font}text='{chan}':fontsize=56:"
           f"fontcolor={VERT_TEXTCOL}:x=(w-text_w)/2:y=1750[vout]")

    cmd = ["ffmpeg", "-y", "-i", f"./{raw.name}"]
    if n == 2:
        cmd += ["-i", str(thumb)]
    cmd += ["-filter_complex", fc, "-map", "[vout]", "-map", "0:a?",
            "-t", str(AYR_MAX_SECONDS),
            "-c:v", "libx264", "-preset", X264_PRESET,
            "-b:v", VERT_BITRATE, "-maxrate", VERT_BITRATE,
            "-bufsize", "7000k", "-g", str(FPS * 2),
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
            "-movflags", "+faststart", f"./{out.name}"]
    r = run(cmd, cwd=str(TMP_DIR))
    for lf in tfiles:
        safe_unlink(lf)
    if r.returncode != 0 or not out.exists():
        log(f"ERRO na versão vertical de {video_id}: "
            f"{r.stderr.strip()[-300:]}")
        return None
    return out


def ayrshare_post(video_path: Path, caption: str, comment: str) -> None:
    """Envia o vídeo à Ayrshare e publica nas plataformas configuradas,
    com o primeiro comentário em seguida."""
    import requests
    hdr = {"Authorization": f"Bearer {AYRSHARE_API_KEY}"}
    r = requests.get("https://api.ayrshare.com/api/media/uploadUrl",
                     params={"contentType": "video/mp4",
                             "fileName": video_path.name},
                     headers=hdr, timeout=60)
    r.raise_for_status()
    j = r.json()
    log(f"Ayrshare: enviando {video_path.name} "
        f"({video_path.stat().st_size / 1e6:.0f} MB)...")
    with open(video_path, "rb") as f:
        r2 = requests.put(j["uploadUrl"], data=f,
                          headers={"Content-Type": "video/mp4"}, timeout=3600)
    r2.raise_for_status()
    r3 = requests.post("https://api.ayrshare.com/api/post",
                       json={"post": caption[:2200],
                             "platforms": AYR_PLATFORMS,
                             "mediaUrls": [j["accessUrl"]],
                             "isVideo": True},
                       headers=hdr, timeout=300)
    r3.raise_for_status()
    resp = r3.json()
    pid = resp.get("id")
    log(f"Ayrshare: publicado em {', '.join(AYR_PLATFORMS)} (id {pid}).")
    if comment and pid:
        for attempt in range(3):  # o post pode demorar a ficar ativo
            rc = requests.post("https://api.ayrshare.com/api/comments",
                               json={"id": pid, "comment": comment[:500]},
                               headers=hdr, timeout=120)
            if rc.ok:
                log("Ayrshare: primeiro comentário publicado.")
                return
            time.sleep(90)
        log(f"AVISO: comentário Ayrshare falhou: {rc.text[:200]}")


def post_to_socials(video_id: str, title: str) -> None:
    """Versão vertical -> Ayrshare. Limpa os temporários ao final."""
    raw = TMP_DIR / f"{video_id}.mp4"
    srt = TMP_DIR / f"{video_id}.srt"
    vert = None
    try:
        if not AYRSHARE_ENABLED or not AYRSHARE_API_KEY:
            return
        vert = build_vertical(video_id, title)
        if vert:
            url = f"https://youtu.be/{video_id}"
            ayrshare_post(vert,
                          AYR_CAPTION_TEMPLATE.format(title=title, url=url),
                          AYR_COMMENT_TEMPLATE.format(title=title, url=url))
    except Exception as e:
        log(f"ERRO ao publicar via Ayrshare ({video_id}): {e}")
    finally:
        safe_unlink(raw)
        safe_unlink(srt)
        if vert:
            safe_unlink(vert)


# ------------------------- POST NO X ----------------------------------

def make_endcard(video_id: str, out_ts: Path, thumb_dir: Path = None) -> bool:
    """Tela final de 10s para posts cortados no X: fundo escuro, thumbnail,
    'Veja o vídeo completo no YouTube' e o link escrito na imagem."""
    thumb = (thumb_dir or VIDEO_DIR) / f"{video_id}.jpg"
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
            f"({r.stderr.strip()[-200:]}) — cortando sem aviso.")
        return False
    return True


def build_x_mp4(video_id: str, limit: float, src_dir: Path = None):
    """Monta o mp4 para o X. Se precisar cortar, os últimos 10s viram a tela
    'Veja o vídeo completo no YouTube'. Retorna (mp4, duração) ou None."""
    src_dir = src_dir or VIDEO_DIR
    src = src_dir / f"{video_id}.ts"
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
        has_card = X_ENDCARD and make_endcard(video_id, card,
                                              thumb_dir=src_dir)
        cut_t = max(limit - 10, 1) if has_card else limit
        r = run(["ffmpeg", "-y", "-i", str(src), "-t", f"{cut_t:.3f}",
                 "-c", "copy", str(main)])
        if r.returncode != 0 or not main.exists():
            log(f"ERRO ao cortar {video_id} para o X: "
                f"{r.stderr.strip()[-200:]}")
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
        safe_unlink(f)
    if r.returncode != 0 or not mp4.exists():
        log(f"ERRO ao preparar mp4 para o X: {r.stderr.strip()[-300:]}")
        return None
    return mp4, eff_dur


def post_to_x(video_id: str, title: str, src_dir: Path = None,
              creds: dict = None) -> None:
    """Posta o vídeo processado no X. Falhas não interrompem o pipeline.
    creds permite postar em outra conta (canais convidados)."""
    if creds is None:
        if not X_ENABLED:
            return
        creds = {"api_key": X_API_KEY, "api_secret": X_API_SECRET,
                 "access_token": X_ACCESS_TOKEN,
                 "access_token_secret": X_ACCESS_TOKEN_SECRET}
    try:
        import tweepy
    except ImportError:
        log("AVISO: tweepy não instalado (python -m pip install tweepy) — "
            "post no X pulado.")
        return

    src_dir = src_dir or VIDEO_DIR
    src = src_dir / f"{video_id}.ts"
    dur = video_duration(src) or 0
    if dur > X_MAX_VIDEO_SECONDS and X_IF_TOO_LONG == "skip":
        log(f"X: {video_id} tem {dur:.0f}s (limite {X_MAX_VIDEO_SECONDS}s) "
            "— post pulado.")
        return

    built = build_x_mp4(video_id, X_MAX_VIDEO_SECONDS, src_dir=src_dir)
    if not built:
        return
    mp4, eff_dur = built
    try:
        auth = tweepy.OAuth1UserHandler(
            creds["api_key"], creds["api_secret"],
            creds["access_token"], creds["access_token_secret"])
        api = tweepy.API(auth)
        client = tweepy.Client(
            consumer_key=creds["api_key"], consumer_secret=creds["api_secret"],
            access_token=creds["access_token"],
            access_token_secret=creds["access_token_secret"])
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

        def send_with_retry(path, category, attempts=3):
            """Refaz o envio em caso de falha de rede (upload longo cai às vezes)."""
            for i in range(attempts):
                try:
                    return send(path, category)
                except Exception as e:
                    transient = any(s in str(e) for s in (
                        "Failed to send request", "Connection", "SSL",
                        "timed out", "Timeout", "Temporarily"))
                    if transient and i < attempts - 1:
                        log(f"X: falha de rede no envio "
                            f"({str(e)[:120]}) — nova tentativa em 60s...")
                        time.sleep(60)
                    else:
                        raise

        # vídeos >2min via API exigem a categoria amplify_video (mesmo Premium)
        category = "amplify_video" if eff_dur > 140 else "tweet_video"
        try:
            send_with_retry(mp4, category)
            log(f"X: postado {video_id}.")
        except Exception as e:
            m = re.search(r"longer than (\d+) minutes?", str(e))
            if category == "amplify_video" and m:
                new_limit = int(m.group(1)) * 60 - 1  # 1s de margem
                log(f"X: a conta aceita no máximo {m.group(1)} min via API — "
                    f"reenviando cortado em {new_limit}s.")
                built = build_x_mp4(video_id, new_limit, src_dir=src_dir)
                if not built:
                    raise
                mp4, eff2 = built
                send_with_retry(mp4,
                                "amplify_video" if eff2 > 140 else "tweet_video")
                log(f"X: postado {video_id} (cortado em {new_limit}s).")
            else:
                raise
    except Exception as e:
        log(f"ERRO ao postar no X ({video_id}): {e}")
    finally:
        safe_unlink(mp4)


# ---------------------------- MONITOR ---------------------------------

def latest_video_ids() -> list:
    """IDs dos últimos MAX_VIDEOS uploads do canal, do mais novo ao mais antigo."""
    r = run(ytdlp_cmd() + [
        "--flat-playlist",
        "--playlist-end", str(MAX_VIDEOS + PRUNE_MARGIN + FETCH_WINDOW_EXTRA),
        "--print", "%(id)s", CHANNEL_URL,
    ])
    if r.returncode != 0:
        log(f"ERRO ao consultar o canal: {r.stderr.strip()[-400:]}")
        return []
    return [l.strip() for l in r.stdout.splitlines() if l.strip()]


def download_and_normalize(video_id: str, out_dir: Path = None, cut_end=None,
                           keep_temp: bool = False):
    """Baixa, corta o final, legenda e converte para .ts padronizado.
    Retorna (ok, título). out_dir/cut_end permitem canais convidados.
    keep_temp mantém o mp4 bruto e o .srt no tmp/ (p/ a versão vertical)."""
    out_dir = out_dir or VIDEO_DIR
    cut_end = CUT_END_SECONDS if cut_end is None else cut_end
    out_file = out_dir / f"{video_id}.ts"
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
        log(f"ERRO no download de {video_id}: {r.stderr.strip()[-400:]}")
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

    # banner de abertura: thumbnail (baixada acima) + título, um arquivo
    # por linha (posicionamento vertical manual no filtro)
    thumb = TMP_DIR / f"{video_id}.jpg"
    title_lines = []
    if INTRO_BANNER and title:
        for i, ln in enumerate(textwrap.wrap(title, width=80)[:3]):
            lf = TMP_DIR / f"{video_id}.t{i}.txt"
            lf.write_text(ln, encoding="utf-8")
            title_lines.append(lf)

    # corte dos segundos finais
    t_limit = None
    dur = video_duration(raw)
    if cut_end > 0 and dur and dur > cut_end + 5:
        t_limit = dur - cut_end
    elif cut_end > 0 and dur:
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
                   thumb=thumb, title_lines=title_lines)
    if not keep_temp:
        safe_unlink(raw)
        safe_unlink(srt)
    for lf in title_lines:
        safe_unlink(lf)
    if ok and thumb.exists():
        # guarda a thumbnail para a tela final dos posts cortados no X
        shutil.move(str(thumb), str(out_dir / f"{video_id}.jpg"))
    else:
        thumb.unlink(missing_ok=True)
    if ok:
        log(f"Pronto: {out_file.name}")
    return ok, title


def detect_shorts() -> None:
    """Detecção leve: enfileira shorts que faltam, busca títulos e limpa
    os que saíram do rodízio."""
    if not PROMO_ENABLED or not PROMO_CHANNEL_URL:
        return
    SHORTS_DIR.mkdir(exist_ok=True)
    r = run(ytdlp_cmd() + [
        "--flat-playlist", "--playlist-end", str(PROMO_MAX_SHORTS),
        "--print", "%(id)s", PROMO_CHANNEL_URL,
    ])
    if r.returncode != 0:
        log(f"ERRO ao consultar o canal do promo: {r.stderr.strip()[-200:]}")
        return
    ids = [l.strip() for l in r.stdout.splitlines() if l.strip()]
    for sid in [i for i in ids if not (SHORTS_DIR / f"{i}.ts").exists()]:
        enqueue_job("short", sid, prio=1)  # prioridade menor que vídeos
    # títulos dos shorts (para o "AGORA"), incluindo os já convertidos
    known = _load_titles().get("shorts", {})
    for sid in [i for i in ids if (SHORTS_DIR / f"{i}.ts").exists()
                and i not in known][:5]:
        t = fetch_title(sid)
        if t:
            save_short_title(sid, t)
    keep = {f"{i}.ts" for i in ids} | _referenced_files()
    for f in SHORTS_DIR.glob("*.ts"):
        if f.name not in keep:
            log(f"Removendo short antigo: {f.name}")
            safe_unlink(f)


def process_short(sid: str) -> bool:
    """Baixa e converte um short (executado pelo worker)."""
    TMP_DIR.mkdir(exist_ok=True)
    raw = TMP_DIR / f"s_{sid}.mp4"
    log(f"Baixando short {sid}...")
    r = run(ytdlp_cmd() + [
        "-f", "bv*[height<=1920]+ba/b",
        "--merge-output-format", "mp4",
        "-o", str(raw),
        f"https://www.youtube.com/watch?v={sid}",
    ])
    if r.returncode != 0 or not raw.exists():
        log(f"ERRO no download do short {sid}: {r.stderr.strip()[-200:]}")
        return False
    ok = normalize_short(raw, SHORTS_DIR / f"{sid}.ts")
    safe_unlink(raw)
    if ok:
        log(f"Short {sid} pronto.")
    return ok


def worker() -> None:
    """Consumidor único da fila: downloads, transcrições e conversões rodam
    aqui, em série, sem travar a detecção de vídeos novos."""
    last_dl = 0.0
    while True:
        _prio, _seq, job = _job_q.get()
        try:
            wait = DOWNLOAD_PAUSE - (time.time() - last_dl)
            if wait > 0:
                time.sleep(wait)  # não martelar o YouTube
            last_dl = time.time()
            kind, vid = job["kind"], job["id"]
            if kind == "main":
                ok, title = download_and_normalize(
                    vid, keep_temp=AYRSHARE_ENABLED)
                if not ok:
                    mark_fail(vid)
                else:
                    clear_fail(vid)
                    save_title(vid, title or "")
                    sync_playlist(_last_ids)
                    log(f"{vid} entrou no loop.")
                    silent = bool(job.get("cfg") and job["cfg"].get("silent"))
                    if silent or was_posted(vid):
                        mark_posted(vid)
                        log(f"{vid} é reposição de vídeo antigo — "
                            "sem alerta e sem repostar nas redes.")
                        safe_unlink(TMP_DIR / f"{vid}.mp4")
                        safe_unlink(TMP_DIR / f"{vid}.srt")
                    else:
                        mark_posted(vid)
                        post_to_x(vid, title or "")
                        post_to_socials(vid, title or "")
            elif kind == "guest":
                g = job["cfg"]
                slug = _slug(g.get("name", ""))
                gdir = GUESTS_DIR / slug
                ok, title = download_and_normalize(
                    vid, out_dir=gdir, cut_end=g.get("cut_end"))
                if ok:
                    save_guest_state(slug, vid, title or "")
                    sync_playlist(_last_ids)
                    log(f"{g.get('name')}: {vid} entrou no ciclo.")
                    ck = [g.get("x_api_key"), g.get("x_api_secret"),
                          g.get("x_access_token"),
                          g.get("x_access_token_secret")]
                    if all(ck):
                        post_to_x(vid, title or "", src_dir=gdir,
                                  creds={"api_key": ck[0],
                                         "api_secret": ck[1],
                                         "access_token": ck[2],
                                         "access_token_secret": ck[3]})
            elif kind == "short":
                if process_short(vid):
                    sync_playlist(_last_ids)
        except Exception as e:
            log(f"ERRO no processamento ({job['kind']} {job['id']}): {e}")
        finally:
            with _queued_lock:
                _queued.discard(job["key"])


def build_playlist(ids_newest_first: list):
    """Playlist em ordem cronológica + grade de exibição (duração e rótulo
    'AGORA' de cada item). Nos intervalos, alterna a vinheta com o bloco
    promocional (chamada -> short do outro canal -> continuidade)."""
    available = [i for i in ids_newest_first
                 if (VIDEO_DIR / f"{i}.ts").exists()][:MAX_VIDEOS]
    shorts = (sorted(SHORTS_DIR.glob("*.ts"),
                     key=lambda p: p.stat().st_mtime, reverse=True)
              if SHORTS_DIR.exists() else [])
    promo_ok = (PROMO_ENABLED and PROMO_EVERY > 0 and shorts
                and PROMO_INTRO_TS.exists() and PROMO_OUTRO_TS.exists())
    data = _load_titles()
    vtitles = data.get("titles", {})
    stitles = data.get("shorts", {})
    gstate = data.get("guests", {})

    # sequência de vídeos: canal principal em ordem cronológica + convidados
    # inseridos nas posições configuradas
    seq = []
    for i in reversed(available):
        t = vtitles.get(i, "")
        seq.append({"path": VIDEO_DIR / f"{i}.ts",
                    "label": f"{AGORA_PREFIX}{t}" if t else "",
                    "guest": None})
    for g in sorted(GUEST_CHANNELS, key=lambda x: x.get("position", 999)):
        slug = _slug(g.get("name", ""))
        st = gstate.get(slug)
        if not st:
            continue
        p = GUESTS_DIR / slug / f"{st['id']}.ts"
        if not p.exists():
            continue
        gt = st.get("title", "")
        pos = max(0, min(int(g.get("position", 999)) - 1, len(seq)))
        seq.insert(pos, {"path": p,
                         "label": f"{AGORA_PREFIX}{gt}" if gt else "",
                         "guest": slug})
    if not seq:
        return "", {"total": 0, "entries": []}

    lines, entries, slot, normal_break = [], [], 0, 0

    def add(path: Path, label: str) -> None:
        lines.append(f"file '{path.as_posix()}'")
        entries.append({"d": get_duration_cached(path), "label": label,
                        "f": path.as_posix()})

    n = len(seq)
    for k, item in enumerate(seq):
        add(item["path"], item["label"])
        nxt = seq[(k + 1) % n]
        if item["guest"] or nxt["guest"]:
            # intervalo de convidado: vinhetas específicas, nunca shorts
            added = False
            if item["guest"]:
                o = BASE_DIR / f"{item['guest']}_outro.ts"
                if o.exists():
                    add(o, AGORA_BUMPER_LABEL)
                    added = True
            if nxt["guest"]:
                i_ts = BASE_DIR / f"{nxt['guest']}_intro.ts"
                if i_ts.exists():
                    add(i_ts, AGORA_BUMPER_LABEL)
                    added = True
            if not added and BUMPER_TS.exists():
                add(BUMPER_TS, AGORA_BUMPER_LABEL)
            continue
        # intervalo normal: alterna vinheta/bloco promocional
        if promo_ok and normal_break % PROMO_EVERY == PROMO_EVERY - 1:
            s = shorts[slot % len(shorts)]
            slot += 1
            st = stitles.get(s.stem, "")
            add(PROMO_INTRO_TS, AGORA_BUMPER_LABEL)
            add(s, f"{AGORA_PREFIX}"
                   f"{AGORA_SHORT_TEMPLATE.format(title=st)}" if st else "")
            add(PROMO_OUTRO_TS, AGORA_BUMPER_LABEL)
        elif BUMPER_TS.exists():
            # também entre o último e o primeiro do loop
            add(BUMPER_TS, AGORA_BUMPER_LABEL)
        normal_break += 1
    text = "\n".join(lines) + "\n" if lines else ""
    sched = {"total": round(sum(e["d"] for e in entries), 3),
             "entries": entries}
    return text, sched


def _find_rotation(canonical: list, current: list):
    """k tal que canonical rotacionado por k == current; None se não houver."""
    if len(canonical) != len(current):
        return None
    if not canonical:
        return 0
    for k in range(len(canonical)):
        if (canonical[k] == current[0]
                and canonical[k:] + canonical[:k] == current):
            return k
    return None


def sync_playlist(ids_newest_first: list) -> None:
    """Versão com trava (chamada pelo monitor e pelo worker)."""
    if not ids_newest_first:
        return
    with _pl_lock:
        _sync_playlist_inner(ids_newest_first)


def _sync_playlist_inner(ids_newest_first: list) -> None:
    """Se o CONTEÚDO da playlist mudou, grava em playlist_new.txt e pede
    reinício (o streamer troca o arquivo só com o FFmpeg parado). A playlist
    ativa pode estar rotacionada pela retomada de posição — rotação não conta
    como mudança. A grade do AGORA acompanha a playlist correspondente."""
    desired, sched = build_playlist(ids_newest_first)
    if not desired:
        return
    if PLAYLIST_PENDING.exists():
        current = PLAYLIST_PENDING.read_text(encoding="utf-8")
    elif PLAYLIST.exists():
        current = PLAYLIST.read_text(encoding="utf-8")
    else:
        current = ""
    d_lines = [l for l in desired.splitlines() if l.strip()]
    c_lines = [l for l in current.splitlines() if l.strip()]
    k = _find_rotation(d_lines, c_lines)
    if k is None:
        # conteúdo realmente mudou -> playlist canônica + grade pendentes
        SCHEDULE_PENDING.write_text(json.dumps(sched, ensure_ascii=False),
                                    encoding="utf-8")
        tmp = PLAYLIST_PENDING.with_suffix(".tmp")
        tmp.write_text(desired, encoding="utf-8")
        tmp.replace(PLAYLIST_PENDING)
        restart_event.set()
    elif not PLAYLIST_PENDING.exists():
        # mesmo ciclo (talvez rotacionado); se rótulos mudaram, atualiza a
        # grade ativa alinhada à rotação atual (só o nosso thread a lê)
        ents = sched["entries"]
        sched["entries"] = ents[k:] + ents[:k]
        sched_json = json.dumps(sched, ensure_ascii=False)
        try:
            cur_sched = SCHEDULE_FILE.read_text(encoding="utf-8")
        except OSError:
            cur_sched = ""
        if sched_json != cur_sched:
            SCHEDULE_FILE.write_text(sched_json, encoding="utf-8")


def _referenced_files() -> set:
    """Nomes de arquivos que a playlist ativa ou a pendente ainda usam —
    apagar um desses derruba a transmissão."""
    refs = set()
    for pl in (PLAYLIST, PLAYLIST_PENDING):
        try:
            for l in pl.read_text(encoding="utf-8").splitlines():
                m = re.match(r"file '(.+)'", l.strip())
                if m:
                    refs.add(Path(m.group(1)).name)
        except OSError:
            pass
    return refs


def prune_old(keep_ids: list) -> None:
    keep = ({f"{i}.ts" for i in keep_ids} | {f"{i}.jpg" for i in keep_ids}
            | _referenced_files())
    for f in list(VIDEO_DIR.glob("*.ts")) + list(VIDEO_DIR.glob("*.jpg")):
        if f.name not in keep:
            log(f"Removendo antigo: {f.name}")
            safe_unlink(f)  # se estiver em uso, tenta no próximo ciclo


def watcher() -> None:
    """Só DETECÇÃO (rápida): novidades geram aviso imediato e entram na fila;
    o worker faz o trabalho pesado sem atrasar o próximo ciclo."""
    global _last_ids
    VIDEO_DIR.mkdir(exist_ok=True)
    while True:
        try:
            prepare_bumper()
            ids_all = latest_video_ids()
            if ids_all:
                now = time.time()
                # o ciclo da live = os MAX_VIDEOS mais recentes DISPONÍVEIS
                # (members-only agendados não ocupam vaga)
                playable = [i for i in ids_all
                            if (VIDEO_DIR / f"{i}.ts").exists()]
                ids = playable[:MAX_VIDEOS]
                _last_ids = ids_all
                # estoque já processado = já publicado (proteção contra
                # repostagem quando um vídeo antigo reentra no ciclo)
                for vid in ids:
                    if not was_posted(vid):
                        mark_posted(vid)
                prev_known = _load_titles().get("order", [])
                # NOVO de verdade = está ACIMA do primeiro vídeo conhecido na
                # lista (que vem do mais novo para o mais velho); tudo abaixo
                # de algo conhecido é conteúdo antigo
                known = set(prev_known) | set(playable)
                new_top = []
                for i in ids_all:
                    if i in known:
                        break
                    new_top.append(i)
                new_top = [i for i in new_top[:MAX_VIDEOS + PRUNE_MARGIN]
                           if not (VIDEO_DIR / f"{i}.ts").exists()
                           and _fail_ok(i, now)]
                # reposição: vídeos ANTIGOS só se faltar gente no ciclo —
                # modo silencioso (sem alerta e sem repostar nas redes)
                backfill = []
                if len(playable) < MAX_VIDEOS:
                    need = MAX_VIDEOS - len(playable)
                    backfill = [i for i in ids_all
                                if i not in new_top
                                and not (VIDEO_DIR / f"{i}.ts").exists()
                                and _fail_ok(i, now)][:need]
                if not prev_known:
                    # primeira execução: nada é "novo", tudo é carga silenciosa
                    backfill = new_top + backfill
                    new_top = []
                # aviso imediato no ticker: o vídeo JÁ está no canal, mesmo
                # que o processamento (legendas etc.) ainda vá demorar
                for vid in new_top:
                    # alerta uma única vez: título já salvo = já alertado
                    if (not was_posted(vid)
                            and vid not in _load_titles().get("titles", {})):
                        t = fetch_title(vid)
                        if t:
                            save_title(vid, t)
                            set_alert(t)
                            log(f"Vídeo novo no canal — aviso no ticker: {t[:60]}")
                update_titles_order(ids, keep_titles=new_top)
                for vid in new_top:
                    enqueue_job("main", vid, prio=0)
                for vid in backfill:
                    enqueue_job("main", vid, prio=1, cfg={"silent": True})
                # margem de histerese: mantém os N+3 disponíveis mais recentes
                prune_old(playable[:MAX_VIDEOS + PRUNE_MARGIN])
                backfill_titles(ids)
                detect_guests()
                detect_shorts()
                sync_playlist(ids)  # cobre remoções e recupera atualizações perdidas
        except Exception as e:
            log(f"ERRO inesperado no monitor: {e}")
        time.sleep(CHECK_INTERVAL)


# --------------------------- LOWER THIRD ------------------------------

_alert_lock = threading.Lock()
_alert = {"text": "", "until": 0.0}


def set_alert(title: str) -> None:
    """Ativa o aviso 'NOVO VÍDEO' na barra por ALERT_MINUTES."""
    if not title:
        return
    with _alert_lock:
        _alert["text"] = title
        _alert["until"] = time.time() + ALERT_MINUTES * 60


def _load_titles() -> dict:
    try:
        return json.loads(TITLES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"order": [], "titles": {}}


def _save_titles(data: dict) -> None:
    tmp = TITLES_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    for _ in range(10):
        try:
            tmp.replace(TITLES_FILE)
            return
        except OSError:
            time.sleep(0.05)  # leitor concorrente segura o arquivo no Windows


def save_title(video_id: str, title: str) -> None:
    if not title:
        return
    with _state_lock:
        data = _load_titles()
        data["titles"][video_id] = title
        _save_titles(data)


def save_short_title(short_id: str, title: str) -> None:
    if not title:
        return
    with _state_lock:
        data = _load_titles()
        data.setdefault("shorts", {})[short_id] = title
        _save_titles(data)


def save_guest_state(slug: str, vid: str, title: str) -> None:
    with _state_lock:
        data = _load_titles()
        data.setdefault("guests", {})[slug] = {"id": vid, "title": title}
        _save_titles(data)


def _fail_ok(vid: str, now: float) -> bool:
    """Recuo progressivo: depois de falhas (ex.: members-only), tenta com
    intervalo crescente (5min -> 30min no máximo)."""
    f = _load_titles().get("fail", {}).get(vid)
    if not f:
        return True
    cnt, last = f
    return now - last >= min(cnt, 6) * CHECK_INTERVAL


def mark_fail(vid: str) -> None:
    with _state_lock:
        data = _load_titles()
        f = data.setdefault("fail", {})
        cnt = f.get(vid, [0, 0])[0] + 1
        f[vid] = [cnt, time.time()]
        _save_titles(data)


def clear_fail(vid: str) -> None:
    with _state_lock:
        data = _load_titles()
        if vid in data.get("fail", {}):
            del data["fail"][vid]
            _save_titles(data)


def was_posted(vid: str) -> bool:
    return vid in _load_titles().get("posted", [])


def mark_posted(vid: str) -> None:
    """Registro permanente (últimos 300) do que já foi publicado nas redes —
    vídeos que oscilam para fora e voltam ao ciclo não são repostados."""
    with _state_lock:
        data = _load_titles()
        p = data.setdefault("posted", [])
        if vid not in p:
            p.append(vid)
            data["posted"] = p[-300:]
            _save_titles(data)


_dur_cache = None


def get_duration_cached(p: Path) -> float:
    """Duração do arquivo, com cache em disco (chave: nome + mtime)."""
    global _dur_cache
    if _dur_cache is None:
        try:
            _dur_cache = json.loads(DURATIONS_FILE.read_text(encoding="utf-8"))
        except Exception:
            _dur_cache = {}
    try:
        mt = p.stat().st_mtime
    except OSError:
        return 0.0
    key = f"v2:{p.parent.name}/{p.name}"
    ent = _dur_cache.get(key)
    if ent and abs(ent[0] - mt) < 1:
        return ent[1]
    # duração frame-exata (conta pacotes de vídeo; nossos .ts são CFR):
    # a estimativa do ffprobe erra frações de segundo que acumulam no ciclo
    d = None
    if p.suffix == ".ts":
        r = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-count_packets", "-show_entries", "stream=nb_read_packets",
                 "-of", "csv=p=0", str(p)])
        try:
            # mpegts pode listar o stream duas vezes (program + stream)
            first = next((l for l in r.stdout.splitlines() if l.strip()), "")
            n = int(first.strip())
            if n > 0:
                d = n / FPS
        except ValueError:
            pass
    if d is None:
        d = video_duration(p) or 0.0
    _dur_cache[key] = [mt, d]
    try:
        DURATIONS_FILE.write_text(json.dumps(_dur_cache), encoding="utf-8")
    except OSError:
        pass
    return d


def fetch_title(vid: str) -> str:
    """Busca só o título de um vídeo (sem baixar), sempre em UTF-8."""
    TMP_DIR.mkdir(exist_ok=True)
    out = TMP_DIR / f"t_{vid}.info.json"
    run(ytdlp_cmd() + ["--skip-download", "--write-info-json",
                       "-o", str(TMP_DIR / f"t_{vid}"),
                       f"https://www.youtube.com/watch?v={vid}"])
    if out.exists():
        try:
            return json.loads(out.read_text(encoding="utf-8")).get("title", "")
        except Exception:
            return ""
        finally:
            safe_unlink(out)
    return ""


def backfill_titles(ids: list) -> None:
    """Busca (sem re-baixar) os títulos de vídeos processados antes do ticker
    existir, para a barra não ficar vazia."""
    if not LOWER_THIRD:
        return
    data = _load_titles()
    missing = [i for i in ids if i not in data.get("titles", {})
               and (VIDEO_DIR / f"{i}.ts").exists()]
    for vid in missing[:5]:  # poucos por ciclo, para não pesar
        t = fetch_title(vid)
        if t:
            save_title(vid, t)
            log(f"Título recuperado para o ticker: {t[:60]}")


def update_titles_order(ids_newest_first: list, keep_titles: list = None) -> None:
    keep = set(ids_newest_first) | set(keep_titles or [])
    with _state_lock:
        data = _load_titles()
        data["order"] = ids_newest_first
        data["titles"] = {i: t for i, t in data["titles"].items()
                          if i in keep}
        _save_titles(data)


def _write_text_file(path: Path, text: str) -> None:
    """Escrita segura para arquivos que o drawtext mapeia a cada frame:
    - sem renomeio (trava a leitura no Windows -> FFmpeg cai);
    - sem truncar para zero (CreateFileMapping falha com arquivo vazio);
    - 'vazio' vira uma quebra de linha: 1 byte, nenhum glifo, barra some.
    Pior caso: um único frame com texto misturado — imperceptível."""
    data = (text if text else "\n").encode("utf-8")
    for _ in range(5):
        try:
            with open(path, "r+b" if path.exists() else "wb") as f:
                f.write(data)
                f.truncate(len(data))
            return
        except OSError:
            time.sleep(0.05)


_sched_cache = {"mtime": 0.0, "data": None}


def _load_schedule():
    try:
        mt = SCHEDULE_FILE.stat().st_mtime
    except OSError:
        return None
    if mt != _sched_cache["mtime"]:
        try:
            _sched_cache["data"] = json.loads(
                SCHEDULE_FILE.read_text(encoding="utf-8"))
            _sched_cache["mtime"] = mt
        except Exception:
            pass
    return _sched_cache["data"]


def current_entry(now: float = None):
    """Item em exibição, cruzando a posição (via -progress do ffmpeg) com a
    grade de durações da playlist. Retorna o dict da grade ou None."""
    sched = _load_schedule()
    if not sched or not sched.get("total") or not _play["at"]:
        return None
    now = now or time.time()
    pos = (_play["pos"] + min(now - _play["at"], 5.0)
           - AGORA_DELAY) % sched["total"]
    for e in sched.get("entries", []):
        pos -= e.get("d", 0)
        if pos < 0:
            return e
    return None


def current_label(now: float) -> str:
    e = current_entry(now)
    return e.get("label", "") if e else ""


def save_resume() -> None:
    """Grava o ponto de retomada. Vinhetas repetem na playlist e tornariam a
    rotação ambígua — então ancora no próximo item ÚNICO (vídeo/short) a
    partir da posição atual."""
    sched = _load_schedule()
    e = current_entry()
    if not e or not sched:
        return
    entries = sched.get("entries", [])
    counts = {}
    for x in entries:
        f = x.get("f")
        counts[f] = counts.get(f, 0) + 1
    try:
        idx = entries.index(e)
    except ValueError:
        idx = 0
    n = len(entries)
    for k in range(n):
        cand = entries[(idx + k) % n]
        f = cand.get("f")
        if f and counts.get(f) == 1:
            try:
                RESUME_FILE.write_text(json.dumps({"file": f}),
                                       encoding="utf-8")
            except OSError:
                pass
            return


def _mark_resume_playlist_head() -> None:
    """No fim natural do ciclo, a continuação correta é o TOPO da playlist
    atual (que pode estar rotacionada) — não o último item tocado."""
    try:
        first = next((l for l in
                      PLAYLIST.read_text(encoding="utf-8").splitlines()
                      if l.strip()), "")
        m = re.match(r"file '(.+)'", first)
        if m:
            RESUME_FILE.write_text(json.dumps({"file": m.group(1)}),
                                   encoding="utf-8")
    except Exception:
        pass


def rotate_for_resume() -> None:
    """Com o FFmpeg parado, rotaciona playlist.txt + grade para o loop
    continuar do item que estava no ar, em vez de voltar ao início."""
    try:
        target = json.loads(
            RESUME_FILE.read_text(encoding="utf-8")).get("file")
    except Exception:
        return
    if not target or not PLAYLIST.exists() or not SCHEDULE_FILE.exists():
        return
    try:
        lines = [l for l in PLAYLIST.read_text(encoding="utf-8").splitlines()
                 if l.strip()]
        sched = json.loads(SCHEDULE_FILE.read_text(encoding="utf-8"))
        entries = sched.get("entries", [])
        if len(lines) != len(entries):
            return
        idx = next((i for i, e in enumerate(entries)
                    if e.get("f") == target), None)
        if not idx:  # None (saiu do ciclo) ou 0 (já é o primeiro)
            return
        PLAYLIST.write_text("\n".join(lines[idx:] + lines[:idx]) + "\n",
                            encoding="utf-8")
        sched["entries"] = entries[idx:] + entries[:idx]
        SCHEDULE_FILE.write_text(json.dumps(sched, ensure_ascii=False),
                                 encoding="utf-8")
        log(f"Retomando o loop a partir de: {Path(target).name}")
    except Exception as e:
        log(f"AVISO: não consegui retomar a posição ({e}) — "
            "começando do início.")


def _bar_text(lead_spaces: int, content: str) -> str:
    """Monta o texto de uma barra: 'Áp' no início força TODAS as linhas a
    terem a mesma altura (senão a caixa varia com acentos/descendentes e o
    vão entre as barras fica oscilando). Esses 2 caracteres ficam escondidos
    atrás do emblema; os espaços posicionam o texto e o ljust estica a barra
    até além da borda direita."""
    if not content:
        return ""
    return ("Áp" + " " * max(lead_spaces - 2, 0) + content).ljust(340)


def ticker_thread() -> None:
    """Alimenta ticker.txt/alert.txt/agora.txt/data.txt/hora.txt, relidos
    a cada frame pelo drawtext."""
    last_t, last_a, last_g, last_d, last_h = None, None, None, None, None
    tz = timezone(timedelta(hours=CLOCK_UTC_OFFSET))
    while True:
        try:
            now = time.time()
            if LT_CLOCK:
                loc = datetime.now(tz)
                d_txt, h_txt = f"{loc:%Y-%m-%d}", f"{loc:%H:%M}"
                if d_txt != last_d:
                    _write_text_file(CLOCK_DATE_FILE, d_txt)
                    last_d = d_txt
                if h_txt != last_h:
                    _write_text_file(CLOCK_TIME_FILE, h_txt)
                    last_h = h_txt
            if AGORA_ENABLED:
                g = _bar_text(AGORA_LEAD_SPACES, current_label(now))
                if g != last_g:
                    _write_text_file(AGORA_FILE, g)
                    last_g = g
            with _alert_lock:
                a_text, a_until = _alert["text"], _alert["until"]
            if a_text and now < a_until:
                t_text = ""
                al = _bar_text(TICKER_LEAD_SPACES, f"{ALERT_PREFIX}{a_text}")
            else:
                data = _load_titles()
                titles = [data["titles"][i] for i in data.get("order", [])
                          if i in data.get("titles", {})][:TICKER_COUNT]
                if titles:
                    idx = int(now // TICKER_SECONDS) % len(titles)
                    t_text = _bar_text(TICKER_LEAD_SPACES,
                                       f"{TICKER_PREFIX}{titles[idx]}")
                else:
                    t_text = ""
                al = ""
            if t_text != last_t:
                _write_text_file(TICKER_FILE, t_text)
                last_t = t_text
            if al != last_a:
                _write_text_file(ALERT_FILE, al)
                if al and not last_a:
                    log(f"Barra de alerta NO AR: {al.strip()[:70]}")
                elif last_a and not al:
                    log("Barra de alerta encerrada (voltou ao ticker).")
                last_a = al
        except Exception as e:
            log(f"ERRO no ticker: {e}")
        time.sleep(1)


def lower_third_filter(include_identity: bool = True) -> str:
    """Filtro drawtext do lower third. Os arquivos de texto são relidos a cada
    frame (reload=1); com texto vazio, a barra é movida para fora da tela.
    include_identity=False quando o emblema (badge.png) cobre nome/canal."""
    font = (f"fontfile='{INTRO_FONT.replace(':', chr(92) + ':')}':"
            if Path(INTRO_FONT).exists() else "")
    # visível: barra colada na borda esquerda (o texto começa depois do emblema
    # via espaços) e no rodapé (y=h-th-16); vazio: fora da tela
    offx = "x=if(gt(text_w\\,2)\\,0\\,w+50)"
    parts = []
    if include_identity:
        name = LOWER_THIRD_NAME.replace("'", "").replace(":", "\\:")
        chan = LOWER_THIRD_CHANNEL.replace("'", "").replace(":", "\\:")
        parts += [
            f"drawtext={font}text='{name}':fontsize=30:fontcolor=white:"
            f"x=24:y=h-121:box=1:boxcolor=0x101010@0.85:boxborderw=12",
            f"drawtext={font}text='{chan}':fontsize=26:fontcolor=0xFFD75E:"
            f"x=24:y=h-68:box=1:boxcolor=0x101010@0.85:boxborderw=12",
        ]
    if AGORA_ENABLED:
        parts += [
            f"drawtext={font}textfile=agora.txt:reload=1:expansion=none:"
            f"fontsize={AGORA_FONTSIZE}:fontcolor={AGORA_TEXTCOL}:{offx}:"
            f"y=h-th-{AGORA_LIFT}:box=1:boxcolor={AGORA_BOX}:boxborderw=12",
        ]
    parts += [
        f"drawtext={font}textfile=ticker.txt:reload=1:expansion=none:"
        f"fontsize=28:fontcolor={TICKER_TEXTCOL}:{offx}:y=h-th-16:"
        f"box=1:boxcolor={TICKER_BOX}:boxborderw=16",
        f"drawtext={font}textfile=alert.txt:reload=1:expansion=none:"
        f"fontsize=28:fontcolor={ALERT_TEXTCOL}:{offx}:y=h-th-16:"
        f"box=1:boxcolor={ALERT_BOX}:boxborderw=16",
    ]
    return ",".join(parts)


def clock_filter() -> str:
    """Relógio (data + hora) desenhado por cima do emblema."""
    if not LT_CLOCK:
        return ""
    font = (f"fontfile='{INTRO_FONT.replace(':', chr(92) + ':')}':"
            if Path(INTRO_FONT).exists() else "")
    return (
        f"drawtext={font}textfile=data.txt:reload=1:expansion=none:"
        f"fontsize={CLOCK_DATE_SIZE}:fontcolor={CLOCK_COLOR}:"
        f"x={CLOCK_DATE_X}:y={CLOCK_DATE_Y},"
        f"drawtext={font}textfile=hora.txt:reload=1:expansion=none:"
        f"fontsize={CLOCK_TIME_SIZE}:fontcolor={CLOCK_COLOR}:"
        f"x={CLOCK_TIME_X}:y={CLOCK_TIME_Y}"
    )


# ---------------------------- STREAMER --------------------------------

def apply_pending_playlist() -> None:
    """Troca playlist_new.txt -> playlist.txt (chamar só com o FFmpeg parado)."""
    if not PLAYLIST_PENDING.exists():
        return
    for _ in range(10):
        try:
            PLAYLIST_PENDING.replace(PLAYLIST)
            if SCHEDULE_PENDING.exists():
                SCHEDULE_PENDING.replace(SCHEDULE_FILE)
            log("Playlist atualizada.")
            return
        except PermissionError:
            time.sleep(1)  # Windows pode segurar o arquivo por um instante
    log("AVISO: não consegui atualizar playlist.txt (arquivo em uso); "
        "tento de novo no próximo reinício.")


def streamer() -> None:
    while True:
        apply_pending_playlist()
        rotate_for_resume()
        if not PLAYLIST.exists() or not PLAYLIST.read_text(encoding="utf-8").strip():
            log("Aguardando vídeos na playlist...")
            time.sleep(10)
            continue

        restart_event.clear()
        # sem -stream_loop: cada volta completa termina e recomeça, o que
        # ressincroniza a posição do AGORA (durações estimadas acumulam erro)
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning",
            "-re",
            "-f", "concat", "-safe", "0", "-i", str(PLAYLIST),
        ]
        if LOWER_THIRD:
            badge = BASE_DIR / LOWER_THIRD_BADGE
            clock = clock_filter()
            if badge.exists():
                # barras via drawtext + emblema PNG + relógio por cima dele
                tail = f";[vb]{clock}[vout]" if clock else ""
                out_v = "[vb]" if clock else "[vout]"
                fc = (f"[0:v]{lower_third_filter(include_identity=False)}[v0];"
                      f"[v0][1:v]overlay=0:main_h-overlay_h{out_v}{tail}")
                cmd += ["-loop", "1", "-i", badge.name,
                        "-filter_complex", fc,
                        "-map", "[vout]", "-map", "0:a"]
            else:
                vf = lower_third_filter()
                if clock:
                    vf += f",{clock}"
                cmd += ["-vf", vf]
            cmd += ["-c:v", "libx264", "-preset", LOWER_THIRD_PRESET,
                    "-b:v", VIDEO_BITRATE, "-maxrate", VIDEO_BITRATE,
                    "-bufsize", "9000k", "-g", str(FPS * 2),
                    "-c:a", "copy"]
            log("Iniciando transmissão (lower third ATIVO — re-codificando; "
                "acompanhe o uso de CPU).")
        else:
            cmd += ["-c", "copy"]
            log("Iniciando transmissão...")
        cmd += ["-bsf:a", "aac_adtstoasc", "-f", "flv", RTMP_URL]
        # -progress alimenta o AGORA e a retomada de posição (sempre ativo)
        cmd += ["-progress", "pipe:1", "-stats_period", "1"]
        global _stream_proc
        _play["pos"], _play["at"] = 0.0, time.time()
        proc = subprocess.Popen(
            cmd, cwd=str(BASE_DIR), creationflags=_PRIO_HIGH,
            stdout=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace")
        _stream_proc = proc
        threading.Thread(target=_progress_reader, args=(proc,),
                         daemon=True, name="progress").start()

        # espera o fim do ciclo, uma queda, ou a playlist mudar (com um
        # intervalo mínimo entre reinícios voluntários, para não encadear)
        started = time.time()
        while proc.poll() is None:
            now = time.time()
            if (restart_event.is_set()
                    and now - started >= RESTART_COOLDOWN):
                break
            # watchdog: transmissão congelada (CPU/disco saturados) — religa
            # em vez de acumular atraso até o YouTube derrubar a conexão
            if (STALL_RESTART > 0 and _play["at"]
                    and now - _play["at"] > STALL_RESTART
                    and now - started > STALL_RESTART):
                log(f"AVISO: transmissão sem avanço há "
                    f"{int(now - _play['at'])}s — religando o FFmpeg.")
                break
            time.sleep(2)

        if proc.poll() is None:
            save_resume()  # guarda o item no ar para retomar dali
            log("Reiniciando FFmpeg com a playlist nova...")
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        elif proc.returncode == 0:
            log("Ciclo completo — recomeçando a fila.")
            _mark_resume_playlist_head()
        else:
            save_resume()
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
    if LOWER_THIRD:
        # os arquivos precisam existir E ter ao menos 1 byte antes do primeiro
        # ffmpeg com drawtext (arquivo de 0 bytes falha no CreateFileMapping)
        for f in (TICKER_FILE, ALERT_FILE, AGORA_FILE,
                  CLOCK_DATE_FILE, CLOCK_TIME_FILE):
            if not f.exists() or f.stat().st_size == 0:
                f.write_text("\n", encoding="utf-8")
        threading.Thread(target=ticker_thread, daemon=True,
                         name="ticker").start()
    threading.Thread(target=worker, daemon=True, name="worker").start()
    threading.Thread(target=watcher, daemon=True, name="watcher").start()
    try:
        streamer()
    except KeyboardInterrupt:
        log("Encerrado pelo usuário.")
    finally:
        kill_stream()


if __name__ == "__main__":
    main()
