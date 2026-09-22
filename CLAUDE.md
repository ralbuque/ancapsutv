# CLAUDE.md — TV ANCAPSU

Contexto do projeto para o Claude (e humanos). Construído em conversa com
Ricardo (Peter Turguniev, canal ANCAPSU) entre ago-set/2026. Leia antes de
mexer em qualquer coisa.

## O que é

Automação completa de distribuição de conteúdo do canal ANCAPSU (notícias,
8–10 vídeos/dia) e canais associados. Um único script (`loop_live.py`) roda
24/7 num **VPS Windows** (8 vCPU/32 GB, caminho `C:\Git\ancapsutv`) e faz:

1. **Live 24/7 no YouTube**: loop dos últimos 20 vídeos do ANCAPSU + canais
   convidados em posições fixas + shorts promocionais do Palácio Assombrado
   + "Classic do dia" (sorteio). Lower third dinâmico com relógio, barra
   AGORA e ticker de últimos vídeos/alerta de lançamento.
2. **X (Twitter)**: cada vídeo novo → post com vídeo (corte em 10 min com
   tela final "veja no YouTube, link no primeiro reply") + reply com link.
   Contas separadas por canal (convidados podem usar a conta principal).
3. **Instagram/TikTok/YouTube Shorts via Ayrshare**: versão vertical
   1080x1920 (layout: faixa superior com título 3 linhas + thumb grande,
   vídeo com 1/3 direito cortado, legenda re-queimada centrada, faixa
   inferior "Canal ANCAPSU"), corte em ~177 s, 1º comentário com link.
4. **Substack**: artigo por vídeo (IA reescreve a transcrição em 1ª pessoa,
   web-only no principal, COM e-mail nos convidados) + "Resumo do dia" às
   22h com e-mail, links inline para os artigos e arte com data carimbada.

## Arquitetura (threads no loop_live.py)

- **streamer** (thread principal): FFmpeg lê `playlist.txt` (concat, SEM
  -stream_loop) e transmite ao RTMP. Re-codifica quando `LOWER_THIRD=True`.
  Ao fim do ciclo/queda/playlist nova → religa retomando posição
  (`resume.json`, rotação da playlist). Watchdog: sem avanço >120 s religa.
  Prioridade de processo ACIMA do normal.
- **watcher** (detecção, a cada 300 s): lista canais (janela top-53 p/
  atravessar filas de members-only), detecta novos ("novo" = acima do
  primeiro id conhecido), dispara alerta no ticker na hora, enfileira jobs,
  poda arquivos (nunca os referenciados na playlist ativa/pendente,
  histerese top-23), sincroniza playlist/grade, agenda classic/resumo.
- **worker** (fila serial, PriorityQueue): downloads, Whisper, conversões e
  publicações. Prioridade 0 = vídeos novos/convidados/classic; 1 = shorts e
  reposições ("silent": sem alerta/sem repostar). Dedupe por chave; backoff
  progressivo de falhas (5→30 min) em `titles.json["fail"]`.
- **ticker**: escreve `ticker/alert/agora/data/hora.txt` (drawtext reload=1
  relê a cada frame). Escrita SEM rename e SEM truncar a zero (ver Lições).
- **progress reader**: lê `-progress` do FFmpeg → posição de exibição →
  AGORA (com `AGORA_DELAY` p/ latência do YouTube) e retomada.

## Pipeline de um vídeo novo (canal principal)

download (yt-dlp+cookies, título/refs via .info.json UTF-8) → rejeita
vertical → transcrição faster-whisper `medium` (word timestamps, blocos ≤2
linhas/7 s, vocabulario.txt + correcoes.txt) → normalização FFmpeg (1080p30
CFR, corte 10 s finais, legenda queimada, banner 30 s com thumb+título) →
entra na playlist (restart com cooldown 90 s) → X → vertical/Ayrshare →
artigo Substack → registra p/ resumo do dia. `mark_posted` ANTES de postar
(máx. 1 tentativa histórica; últimos 300 ids em `titles.json["posted"]`).

## Autenticações (tudo no config.py, fora do Git)

- **YouTube (leitura)**: `cookies.txt` de conta secundária NÃO-membro (para
  members-only falhar até virar público — comportamento desejado).
- **X**: OAuth 1.0a (4 chaves por conta), tweepy; >2 min exige
  `amplify_video` (conta Premium); limite da conta 10 min (fallback lê o
  limite do erro). Pay-per-use: post ~US$0,015; com link ~US$0,20 (por isso
  link vai no reply).
- **Ayrshare**: API key; YouTube em chamada SEPARADA de instagram/tiktok
  (título ≤80 chars, a Ayrshare anexa #Shorts; visibility public!).
  Comentário TikTok falha (restrição da plataforma) — aviso esperado.
- **Substack**: cookie `substack.sid` (login/senha cai em captcha no VPS).
  Cliente próprio: POST /api/v1/drafts (ProseMirror; links via marks),
  /prepublish, /publish {send}; imagem via /api/v1/image (URL ou data URI).
- **Anthropic**: `ANTHROPIC_API_KEY`, modelo padrão claude-haiku-4-5.

## Estado em disco (gitignored)

`titles.json` = ordem/títulos, shorts, guests {slug:{id,title}}, posted,
skip (verticais banidos), fail (backoff), digest_pending/date,
classic_pick/hist, ayr_sent. `schedule.json`(+_new) = grade do AGORA
(durações frame-exatas via contagem de pacotes). `durations.json` cache.
`resume.json` ponto de retomada (âncora sempre em item ÚNICO — vinhetas
repetem). `playlist.txt`(+_new): troca só com FFmpeg parado (lock Windows).
`log.txt` (rotação 5 MB).

## Lições aprendidas (NÃO reaprender do jeito difícil)

- **Windows + arquivos**: não renomear sobre arquivo aberto pelo FFmpeg
  (playlist → troca com processo parado; ticker → escrita direta r+b sem
  truncar a zero, "vazio" = "\n"; CreateFileMapping falha com 0 bytes).
  Apagar com `safe_unlink` (WinError 32).
- **-stream_loop -1 + concat**: loopava só o PRIMEIRO arquivo. Nunca
  reintroduzir; o ciclo religa a cada volta (e ressincroniza o AGORA).
- **Encoding**: título NUNCA do stdout do yt-dlp.exe (cp1252) — sempre do
  .info.json. PYTHONUTF8 não afeta exe congelado.
- **Anti-realimentação**: shorts verticais do próprio canal viravam "vídeo
  novo" (o corte em 180 s cravados virou 3:01 → vídeo comum → loop de
  repostagem com #Shorts acumulando). Proteções: corte 177 s, skip de
  verticais na detecção E no download, registro posted, envio único Ayrshare.
- **Fila members-only**: até 26 agendados já ocuparam o topo da listagem —
  janela de 53, ciclo = 20 mais recentes DISPONÍVEIS, backoff de falhas.
- **CPU**: transcrição medium + normalização + live re-codificando saturou
  4 vCPU e derrubou a live (40 min de stall → -10053). Prioridades de
  processo + WHISPER_THREADS=2 + watchdog. FFmpeg órfão após Ctrl+C:
  kill_stream no finally; se suspeitar, `taskkill /IM ffmpeg.exe /F`.
- **drawtext**: altura da caixa varia com o texto → "Áp" invisível atrás do
  badge normaliza; multilinha do FFmpeg 9 espaça demais → uma drawtext por
  linha com y explícito; %{localtime} tem escaping frágil → relógio via
  arquivos data.txt/hora.txt (fuso convertido em Python, UTC-3 fixo).
- **Não-monotonic DTS / "Failed to update header"**: ruído benigno.
- **Instagram**: não mencionar "veja no YouTube" NA IMAGEM (algoritmo pune);
  Reels >3 min não recomendados a não-seguidores (daí o corte 177 s).
- **Título do artigo** = literal do vídeo, só caixa normalizada pela IA
  (validação: mesmas palavras ou usa o original).

## Operação

Deploy: commit/push aqui → `git pull` no VPS → reiniciar o script
(`C:\...\Python312\python.exe loop_live.py`; usar SEMPRE o Python312 — há
um 3.9 fantasma na máquina). Na partida, conferir a linha "Integrações:"
(mostra ON/OFF de X/Ayrshare/Substack/convidados/classic/promo). Config
nova tem default — basta acrescentar linhas ao config.py do VPS. Vinhetas
(.mp4 na pasta) reconvertem sozinhas quando o arquivo muda; `bumper.ts` etc.
são derivados. NÃO renomear "name" de canais convidados (muda o slug e
repostaria — migração manual se precisar).

## Pendências / futuro

- **Rumble**: API oficial simple-upload (thumbnail + srt!) — aguarda token
  via bd@rumble.com. Integração pequena no worker quando chegar.
- **Projeto separado**: legado do Mundo em Revolução → Substack (~2.000
  vídeos, 1/dia de guerra). Plano: transcrever no PC Windows com RTX 4090
  do Ricardo (no Brasil, disponível fim do mês); testar retrodatação de
  posts via API (UI permite; senão, gerar export WordPress e importar).
  Repositório próprio quando começar.
- **Bilibili**: em observação (expansão no Brasil); upload via biliup se
  vingar. **Ayrshare comentário TikTok**: limitação da plataforma.
- Substack dos convidados (MeR/Safesrc): blocos "substack" por canal no
  GUEST_CHANNELS — Ricardo ativa quando quiser.

## Estilo de trabalho combinado

Respostas e commits em pt-BR. Mudanças pequenas e incrementais, sempre com
`python -m py_compile` e testes de lógica/render no sandbox antes de
entregar; frames de vídeo verificados visualmente. Erros novos: pedir o
trecho do log.txt. O Ricardo edita config.py direto no VPS; código só via
git. Custos de API importam (reply com link no X = 13x mais caro; artigos
IA = centavos). Tom: direto, sem cerimônia.
