#!/usr/bin/env python3
"""Студия автомонтажа Reels.

Агенты (шаги):
  1. Расшифровка — Whisper (faster-whisper), таймкоды по словам
  2. Сценарист   — убирает эээ/повторы/дубли, строит фразы, придумывает хук
  3. Субтитры    — .ass: по словам, белые, жирные, по центру снизу + хук 3 сек
  4. Звук        — loudnorm в 2 прохода до -14 LUFS
  5. Рендер      — 1080x1920, 30 fps -> папка «Готово»

Команды:
  reels.py prepare <файл>     # шаги 1-2, печатает расшифровку для проверки
  reels.py show <проект>      # показать расшифровку
  reels.py preview <проект>   # быстрое превью 540x960
  reels.py render <проект>    # финальный рендер (кнопка «Готово»)
"""
import json, os, re, shutil, subprocess, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STUDIO = ROOT / "studio"
WORK = ROOT / "work"
DONE = ROOT / "Готово"
SOURCES = ROOT / "Исходники"
CONFIG = json.loads((STUDIO / "config.json").read_text(encoding="utf-8"))
STEPS = ["Расшифровка", "Сценарист", "Субтитры", "Звук", "Рендер"]


# ---------------------------------------------------------------- состояние
def pdir(name):
    return WORK / name


def load_state(name):
    f = pdir(name) / "state.json"
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    return {"steps": {s: {"status": "ждёт", "msg": ""} for s in STEPS}}


def save_state(name, st):
    f = pdir(name) / "state.json"
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(f)


def step(name, s, status, msg=""):
    st = load_state(name)
    st["steps"][s] = {"status": status, "msg": msg, "t": time.time()}
    save_state(name, st)
    print(f"[{s}] {status} {msg}".rstrip(), flush=True)


def run(cmd, cwd=None):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-2000:])
    return r


def project_name(src: Path):
    n = re.sub(r"[^\w\-]+", "_", src.stem, flags=re.U).strip("_")
    return n or "reel"


# ---------------------------------------------------------------- 1. расшифровка
def transcribe(name):
    st = load_state(name)
    src = st["source"]
    step(name, "Расшифровка", "работает", "загружаю модель")
    from faster_whisper import WhisperModel
    mdir = ROOT / "models" / CONFIG["whisper_model"]
    model_ref = str(mdir) if mdir.exists() else CONFIG["whisper_model"]
    model = WhisperModel(model_ref, device="auto", compute_type=CONFIG.get("compute_type", "int8"))
    step(name, "Расшифровка", "работает", "слушаю речь")
    import numpy as np
    pcm = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", src, "-vn", "-ac", "1", "-ar", "16000",
                          "-f", "s16le", "-"], capture_output=True, check=True).stdout
    audio = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0
    segs, info = model.transcribe(
        audio,
        language=CONFIG["language"],
        word_timestamps=True,
        vad_filter=False,
        beam_size=5,
        condition_on_previous_text=False,
        initial_prompt=CONFIG["whisper_prompt"],
    )
    words = []
    for sg in segs:
        for w in sg.words or []:
            t = w.word.strip()
            if t:
                words.append({"w": t, "s": round(w.start, 3), "e": round(w.end, 3), "p": round(w.probability, 3)})
    (pdir(name) / "words.json").write_text(json.dumps(words, ensure_ascii=False, indent=0), encoding="utf-8")
    step(name, "Расшифровка", "готово", f"{len(words)} слов, {info.duration:.0f} сек")


# ---------------------------------------------------------------- 2. сценарист
def norm(w):
    return re.sub(r"[^\w]+", "", w.lower(), flags=re.U)


FILLER_RE = re.compile(CONFIG["filler_regex"], re.U | re.I)


def is_filler(w):
    n = norm(w)
    return n == "" or bool(FILLER_RE.fullmatch(n))


def remove_repeats(ws):
    """Убирает повторы: «я я», «мен айтам мен айтам» — оставляет последний вариант."""
    changed = True
    while changed:
        changed = False
        for n in (4, 3, 2, 1):
            i = 0
            while i + 2 * n <= len(ws):
                a = [norm(x["w"]) for x in ws[i:i + n]]
                b = [norm(x["w"]) for x in ws[i + n:i + 2 * n]]
                if a == b and all(a):
                    del ws[i:i + n]
                    changed = True
                else:
                    i += 1
    return ws


def build_phrases(ws):
    out, cur = [], []
    for w in ws:
        if cur and (w["s"] - cur[-1]["e"] > 0.6 or len(cur) >= CONFIG["phrase_max_words"]):
            out.append(cur); cur = []
        cur.append(w)
        if re.search(r"[.!?…]$", w["w"]) and len(cur) >= 3:
            out.append(cur); cur = []
    if cur:
        out.append(cur)
    return out


def remove_retakes(phrases):
    """Дубли: если фраза начинается так же, как следующая — это неудачный дубль, убираем."""
    res = []
    for i, ph in enumerate(phrases):
        if i + 1 < len(phrases):
            a = [norm(x["w"]) for x in ph[:3]]
            b = [norm(x["w"]) for x in phrases[i + 1][:3]]
            if len(a) >= 2 and a == b[:len(a)]:
                continue
        res.append(ph)
    return res


GREETINGS = {"сәлем", "салем", "салам", "сәлеметсіздер", "сәлеметсіз", "привет", "всем", "здравствуйте",
             "қайырлы", "армысыздар", "hello", "hi"}


def make_hook(phrases):
    """Хук = первое предложение (без приветствия), не длиннее hook_max_words слов."""
    words = [w["w"] for ph in phrases for w in ph]
    sents, cur = [], []
    for w in words:
        cur.append(w)
        if re.search(r"[.!?…]$", w):
            sents.append(cur); cur = []
    if cur:
        sents.append(cur)
    for sn in sents[:4]:
        if norm(sn[0]) in GREETINGS:
            continue
        if len(sn) >= 3:
            hook = " ".join(sn[:CONFIG["hook_max_words"]])
            return strip_emoji(hook).rstrip(",;:—-. ")
    return strip_emoji(" ".join(words[:CONFIG["hook_max_words"]])).rstrip(",;:—-. ")


def script(name):
    step(name, "Сценарист", "работает", "чищу паузы, эээ, повторы")
    words = json.loads((pdir(name) / "words.json").read_text(encoding="utf-8"))
    kept = [w for w in words if not is_filler(w["w"])]
    fillers = len(words) - len(kept)
    kept = remove_repeats(kept)
    reps = len(words) - fillers - len(kept)
    phrases = remove_retakes(build_phrases(kept))
    retakes = len(build_phrases(kept)) - len(phrases)
    data = {"hook": make_hook(phrases),
            "phrases": [{"id": i + 1, "words": ph} for i, ph in enumerate(phrases)]}
    (pdir(name) / "phrases.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    write_transcript(name, data)
    step(name, "Сценарист", "готово", f"убрано: эээ {fillers}, повторов {reps}, дублей {retakes}")


def fmt(t):
    return f"{int(t // 60):02d}:{t % 60:05.2f}"


def write_transcript(name, data):
    lines = ["# Расшифровка. Исправляй текст после ] — время не трогай.",
             "# Удалить фразу из ролика: удали строку целиком.",
             f"ХУК: {data['hook']}", ""]
    for ph in data["phrases"]:
        ws = ph["words"]
        text = ph.get("text") or " ".join(w["w"] for w in ws)
        lines.append(f"[{ph['id']:03d} {fmt(ws[0]['s'])}-{fmt(ws[-1]['e'])}] {text}")
    (pdir(name) / "transcript.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_transcript(name):
    """Возвращает хук и список фраз с исправленным текстом и таймингами слов."""
    data = json.loads((pdir(name) / "phrases.json").read_text(encoding="utf-8"))
    by_id = {p["id"]: p for p in data["phrases"]}
    hook, out = data["hook"], []
    for line in (pdir(name) / "transcript.txt").read_text(encoding="utf-8").splitlines():
        if line.startswith("ХУК:"):
            hook = line[4:].strip()
            continue
        m = re.match(r"\[(\d+)[^\]]*\]\s*(.*)", line)
        if not m or int(m.group(1)) not in by_id:
            continue
        text = strip_emoji(m.group(2)).strip()
        if not text:
            continue
        orig = by_id[int(m.group(1))]["words"]
        new = text.split()
        if [w["w"] for w in orig] == new:
            ws = [dict(w) for w in orig]
        elif len(new) == len(orig):          # исправили слова — тайминги те же
            ws = [dict(w, w=t) for w, t in zip(orig, new)]
        else:                                # число слов поменялось — делим время по длине слов
            s0, e0 = orig[0]["s"], orig[-1]["e"]
            tot = sum(len(t) + 1 for t in new)
            ws, t = [], s0
            for tok in new:
                d = (e0 - s0) * (len(tok) + 1) / tot
                ws.append({"w": tok, "s": round(t, 3), "e": round(t + d, 3)})
                t += d
        out.append({"id": int(m.group(1)), "words": ws, "src": (orig[0]["s"], orig[-1]["e"]), "orig": orig})
    return strip_emoji(hook), out


EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F900-\U0001F9FF️‍⬀-⯿]+")


def strip_emoji(s):
    return re.sub(r"\s{2,}", " ", EMOJI_RE.sub("", s))


# ---------------------------------------------------------------- монтажный план
def keep_intervals(phrases, duration):
    pad, gap = CONFIG["pad"], CONFIG["max_pause"]
    spans = []
    for ph in phrases:
        for w in ph["orig"]:
            spans.append([max(0, w["s"] - pad), min(duration, w["e"] + pad)])
    spans.sort()
    merged = []
    for a, b in spans:
        if merged and a - merged[-1][1] <= gap:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(round(a, 3), round(b, 3)) for a, b in merged if b - a > 0.05]


def mapper(intervals):
    offs, o = [], 0.0
    for a, b in intervals:
        offs.append(o); o += b - a

    def f(t):
        for (a, b), off in zip(intervals, offs):
            if t <= b:
                return off + max(0.0, t - a)
        return o
    return f, o


# ---------------------------------------------------------------- 3. субтитры
def ass_time(t):
    t = max(0, t)
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def ass_escape(s):
    return s.replace("\\", "").replace("{", "(").replace("}", ")")


def wrap(text, n):
    lines, cur = [], ""
    for w in text.split():
        if cur and len(cur) + 1 + len(w) > n:
            lines.append(cur); cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        lines.append(cur)
    return "\\N".join(lines)


def make_ass(name, hook, phrases, f, total):
    c = CONFIG
    sub, hk = c["subs"], c["hook"]
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Sub,{sub['font']},{sub['size']},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,{sub['outline']},{sub['shadow']},2,80,80,{sub['margin_bottom']},1
Style: Hook,{hk['font']},{hk['size']},&H00FFFFFF,&H00FFFFFF,&H00000000,&H99000000,-1,0,0,0,100,100,0,0,1,{hk['outline']},{hk['shadow']},8,70,70,{hk['margin_top']},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ev = []
    if hook:
        txt = ass_escape(hook.upper() if hk.get("uppercase") else hook)
        ev.append(f"Dialogue: 1,{ass_time(0)},{ass_time(min(hk['seconds'], total))},Hook,,0,0,0,,"
                  f"{{\\fad(120,200)}}{wrap(txt, hk['wrap_chars'])}")
    allw = []
    for ph in phrases:
        for w in ph["words"]:
            allw.append((f(w["s"]), f(w["e"]), w["w"]))
    n = max(1, c["subs"]["words_per_caption"])
    groups = [allw[i:i + n] for i in range(0, len(allw), n)]
    for i, g in enumerate(groups):
        s = g[0][0]
        e = g[-1][1] + 0.25
        if i + 1 < len(groups):
            e = min(e, groups[i + 1][0][0]) if groups[i + 1][0][0] - g[-1][1] < 0.5 else g[-1][1] + 0.15
        e = max(e, s + 0.12)
        txt = ass_escape(" ".join(x[2] for x in g))
        if sub.get("uppercase"):
            txt = txt.upper()
        if not sub.get("keep_punctuation", False):
            txt = re.sub(r"[.,!?;:…]+$", "", txt)
        ev.append(f"Dialogue: 0,{ass_time(s)},{ass_time(e)},Sub,,0,0,0,,{{\\fscx92\\fscy92\\t(0,80,\\fscx100\\fscy100)}}{txt}")
    (pdir(name) / "subs.ass").write_text(head + "\n".join(ev) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- 4-5. звук и рендер
def probe_duration(src):
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", src])
    return float(r.stdout.strip())


def has_audio(src):
    r = run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", src])
    return bool(r.stdout.strip())


def cut_graph(intervals, video=True):
    parts, labels = [], []
    for i, (a, b) in enumerate(intervals):
        if video:
            parts.append(f"[0:v]trim={a}:{b},setpts=PTS-STARTPTS[v{i}]")
        parts.append(f"[0:a]atrim={a}:{b},asetpts=PTS-STARTPTS,afade=t=in:d=0.01,afade=t=out:st={max(0, b - a - 0.012):.3f}:d=0.012[a{i}]")
        labels.append((f"[v{i}]" if video else "") + f"[a{i}]")
    n = len(intervals)
    parts.append("".join(labels) + f"concat=n={n}:v={1 if video else 0}:a=1" + ("[vc][ac]" if video else "[ac]"))
    return ";".join(parts)


def measure_loudness(src, intervals):
    t = CONFIG["loudness"]
    g = cut_graph(intervals, video=False) + f";[ac]loudnorm=I={t['I']}:TP={t['TP']}:LRA={t['LRA']}:print_format=json[ao]"
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", src, "-filter_complex", g, "-map", "[ao]", "-f", "null", "-"],
                       capture_output=True, text=True)
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", r.stderr, re.S)
    if not m:
        raise RuntimeError("loudnorm: " + r.stderr[-1500:])
    return json.loads(m.group(0))


def integrated_lufs(f):
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(f), "-af", "ebur128=peak=true", "-f", "null", "-"],
                       capture_output=True, text=True)
    return float(re.findall(r"I:\s+(-?[\d.]+) LUFS", r.stderr)[-1])


def fix_loudness(f, d):
    """Контроль: если вышло не ровно -14 LUFS (±0.5) — докручиваем громкость с лимитером пиков."""
    target, tp = CONFIG["loudness"]["I"], CONFIG["loudness"]["TP"]
    for _ in range(3):
        diff = target - integrated_lufs(f)
        if abs(diff) <= 0.5:
            break
        g = f.with_name(f.stem + ".fix.mp4")
        run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(f), "-c:v", "copy",
             "-af", f"volume={diff + 0.2:.2f}dB,alimiter=limit={10 ** (tp / 20):.3f}:level=false:attack=1:release=50",
             "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", str(g)], cwd=d)
        g.replace(f)
    return f


def render(name, preview=False):
    st = load_state(name)
    src = st["source"]
    d = pdir(name)
    hook, phrases = read_transcript(name)
    if not phrases:
        raise RuntimeError("в расшифровке не осталось фраз")
    if not has_audio(src):
        raise RuntimeError("в видео нет звука")
    dur = probe_duration(src)
    intervals = keep_intervals(phrases, dur)
    f, total = mapper(intervals)
    (d / "cuts.json").write_text(json.dumps(intervals), encoding="utf-8")

    step(name, "Субтитры", "работает")
    make_ass(name, hook, phrases, f, total)
    step(name, "Субтитры", "готово", f"{sum(len(p['words']) for p in phrases)} слов, хук: «{hook}»")

    t = CONFIG["loudness"]
    step(name, "Звук", "работает", "замер громкости")
    m = measure_loudness(src, intervals)
    ln = (f"loudnorm=I={t['I']}:TP={t['TP']}:LRA={t['LRA']}:measured_I={m['input_i']}:measured_TP={m['input_tp']}"
          f":measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true")
    step(name, "Звук", "готово", f"было {m['input_i']} LUFS → цель {t['I']} LUFS")

    W, H = (540, 960) if preview else (1080, 1920)
    fps = CONFIG["fps"]
    g = cut_graph(intervals) + (
        f";[vc]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,fps={fps},"
        f"ass=subs.ass:fontsdir={STUDIO / 'fonts'},format=yuv420p[vo]"
        f";[ac]{ln},aresample=48000[ao]")
    if preview:
        out = d / "preview.mp4"
        venc = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "28"]
    else:
        DONE.mkdir(exist_ok=True)
        out = DONE / f"{name}_reels.mp4"
        venc = ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-profile:v", "high"]
    label = "превью" if preview else "финал 1080x1920"
    step(name, "Рендер", "работает", label)
    tmp = out.with_name(out.stem + ".part.mp4")
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(Path(src).resolve()),
         "-filter_complex", g, "-map", "[vo]", "-map", "[ao]", *venc, "-r", str(fps),
         "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", str(tmp.resolve())], cwd=d)
    tmp = fix_loudness(tmp, d)
    tmp.replace(out)
    msg = f"{label}: {total:.1f} сек (было {dur:.1f})"
    if not preview:
        msg += f" → Готово/{out.name}"
        st = load_state(name); st["final"] = str(out); save_state(name, st)
    msg += f", звук {integrated_lufs(out):.1f} LUFS"
    step(name, "Рендер", "готово" if not preview else "превью готово", msg)
    return out


# ---------------------------------------------------------------- точки входа
def new_project(src):
    src = Path(src).expanduser()
    if not src.exists():
        cand = SOURCES / src.name
        if cand.exists():
            src = cand
        else:
            raise SystemExit(f"Файл не найден: {src}")
    name = project_name(src)
    d = pdir(name)
    d.mkdir(parents=True, exist_ok=True)
    st = load_state(name)
    st.update({"name": name, "source": str(src.resolve()), "created": time.time()})
    st["steps"] = {s: {"status": "ждёт", "msg": ""} for s in STEPS}
    save_state(name, st)
    return name


def prepare(src):
    name = new_project(src)
    try:
        transcribe(name)
        script(name)
    except Exception as e:
        st = load_state(name)
        cur = next((s for s in STEPS if st["steps"][s]["status"] == "работает"), "Расшифровка")
        step(name, cur, "ошибка", str(e)[-300:])
        raise
    return name


def safe(fn, name, *a):
    try:
        return fn(name, *a)
    except Exception as e:
        st = load_state(name)
        cur = next((s for s in STEPS if st["steps"][s]["status"] == "работает"), "Рендер")
        step(name, cur, "ошибка", str(e)[-300:])
        raise


def main():
    if len(sys.argv) < 3:
        print(__doc__); sys.exit(1)
    cmd, arg = sys.argv[1], sys.argv[2]
    if cmd == "prepare":
        name = prepare(arg)
        print(f"\nПроект: {name}\n")
        print((pdir(name) / "transcript.txt").read_text(encoding="utf-8"))
    elif cmd == "show":
        print((pdir(arg) / "transcript.txt").read_text(encoding="utf-8"))
    elif cmd == "preview":
        print(safe(render, arg, True))
    elif cmd == "render":
        print(safe(render, arg, False))
    else:
        print(__doc__); sys.exit(1)


if __name__ == "__main__":
    main()
