"""ReelDock - 인스타 릴스/스토리 원본 화질 다운로더 + 미디어 도구 (Windows EXE / Android APK)"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import flet as ft
import yt_dlp

try:
    from gallery_dl import config as gdl_config, job as gdl_job
except Exception:  # 사진/캐러셀/하이라이트용 보조 엔진
    gdl_config = gdl_job = None

URL_RE = re.compile(r"https?://(?:www\.)?instagram\.com/[^\s]+", re.I)
SEED = "#5B6CFF"
MEDIA = {".mp4", ".mkv", ".webm", ".mov", ".m4a", ".mp3", ".wav", ".flac", ".jpg", ".jpeg", ".png", ".webp", ".gif"}
IMG = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
QUALITY = {"원본 최고 화질": None, "1080p 이하": 1080, "720p 이하": 720, "480p 이하": 480, "오디오만 (m4a)": "audio"}
TEMPLATES = {
    "계정/날짜_ID": "%(uploader)s/%(upload_date)s_%(id)s.%(ext)s",
    "날짜_계정_ID (폴더 없음)": "%(upload_date)s_%(uploader)s_%(id)s.%(ext)s",
}
DEFAULTS = {
    "out_dir": "", "sessionid": "", "browser": "없음", "concurrency": 2, "thumb": False,
    "skip_dup": True, "template": "계정/날짜_ID", "quality": "원본 최고 화질",
    "remember": True, "dark": True, "pin_hash": "",
}
AUDIO_CODEC = {
    "mp3": ["-c:a", "libmp3lame", "-q:a", "2"], "m4a": ["-c:a", "aac", "-b:a", "256k"],
    "wav": [], "flac": [],
}
CRF = {"조금 줄이기": "24", "많이 줄이기": "28", "최대로 줄이기": "33"}
I = ft.Icons

# 도구 정의: (id, 이름, 설명, 아이콘, [(키, 라벨, 기본값, 선택지)], 확장자(None=원본 유지), 접미사, ffmpeg 인자 생성)
TOOLS = {
    "동영상": [
        ("trim", "동영상 자르기", "원하는 부분만 남기기 (무손실)", I.CONTENT_CUT,
         [("s", "시작 (초 또는 분:초)", "0", None), ("e", "끝 (초 또는 분:초)", "10", None)], "mp4", "_cut",
         lambda i, o, p: ["-ss", p["s"], "-to", p["e"], "-i", i, "-c", "copy", o]),
        ("split", "30초 단위로 나누기", "상태/스토리용 분할 (무손실)", I.CALL_SPLIT, [], "mp4", "_part%03d",
         lambda i, o, p: ["-i", i, "-c", "copy", "-map", "0", "-f", "segment", "-segment_time", "30",
                          "-reset_timestamps", "1", o]),
        ("compress", "동영상 압축", "공유용으로 용량 줄이기", I.COMPRESS,
         [("lv", "압축 강도", "많이 줄이기", list(CRF))], "mp4", "_small",
         lambda i, o, p: ["-i", i, "-c:v", "libx264", "-crf", CRF[p["lv"]], "-preset", "veryfast",
                          "-c:a", "aac", "-b:a", "128k", o]),
        ("gif", "동영상을 GIF로", "한 순간을 GIF로 만들기",  I.GIF_BOX,
         [("s", "시작 (초)", "0", None), ("d", "길이 (초)", "3", None)], "gif", "_clip",
         lambda i, o, p: ["-ss", p["s"], "-t", p["d"], "-i", i, "-vf",
                          "fps=15,scale=480:-1:flags=lanczos", o]),
        ("mp4", "MP4로 변환", "MKV/WebM을 어디서나 재생", I.SWAP_HORIZ, [], "mp4", "_conv",
         lambda i, o, p: ["-i", i, "-c:v", "libx264", "-crf", "18", "-c:a", "aac", o]),
        ("speed", "속도 변경", "음정은 유지하며 느리게/빠르게",  I.FAST_FORWARD,
         [("f", "배속", "1.5", ["0.5", "0.75", "1.25", "1.5", "2"])], "mp4", "_speed",
         lambda i, o, p: ["-i", i, "-filter:v", f"setpts=PTS/{p['f']}", "-filter:a", f"atempo={p['f']}", o]),
    ],
    "오디오": [
        ("aext", "오디오 추출", "어떤 영상이든 MP3/M4A로 변환", I.MUSIC_NOTE,
         [("fmt", "형식", "mp3", ["mp3", "m4a"])], "fmt", "_audio",
         lambda i, o, p: ["-i", i, "-vn"] + AUDIO_CODEC[p["fmt"]] + [o]),
        ("atrim", "오디오 자르기", "오디오를 잘라 벨소리 만들기", I.CONTENT_CUT,
         [("s", "시작 (초 또는 분:초)", "0", None), ("e", "끝 (초 또는 분:초)", "30", None)], None, "_cut",
         lambda i, o, p: ["-ss", p["s"], "-to", p["e"], "-i", i, "-c", "copy", o]),
        ("aconv", "오디오 변환", "MP3, M4A, WAV, FLAC", I.SWAP_HORIZ,
         [("fmt", "형식", "mp3", list(AUDIO_CODEC))], "fmt", "_conv",
         lambda i, o, p: ["-i", i, "-vn"] + AUDIO_CODEC[p["fmt"]] + [o]),
        ("norm", "볼륨 정규화", "작은 소리를 키워 균일하게", I.VOLUME_UP, [], None, "_norm",
         lambda i, o, p: ["-i", i, "-af", "loudnorm", o]),
    ],
}


def find_ffmpeg():
    p = shutil.which("ffmpeg")
    if p:
        return p
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def human(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.0f}{u}" if u == "B" else f"{n:.1f}{u}"
        n /= 1024


def main(page: ft.Page):
    page.title = "ReelDock"
    page.padding = 0
    page.theme = ft.Theme(color_scheme_seed=SEED, use_material3=True)
    page.dark_theme = ft.Theme(color_scheme_seed=SEED, use_material3=True)
    is_android = page.platform == ft.PagePlatform.ANDROID

    data_dir = Path(os.environ.get("FLET_APP_STORAGE_DATA") or (Path.home() / ".reeldock"))
    data_dir.mkdir(parents=True, exist_ok=True)
    vault_dir = data_dir / "vault"
    vault_dir.mkdir(exist_ok=True)
    cfg_path, hist_path = data_dir / "settings.json", data_dir / "history.json"
    archive_path, cookie_path = data_dir / "archive.txt", data_dir / "cookies.txt"

    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(cfg_path.read_text("utf-8")))
    except Exception:
        pass
    if not cfg["out_dir"]:
        cfg["out_dir"] = ("/storage/emulated/0/Download/ReelDock" if is_android
                          else str(Path.home() / "Downloads" / "ReelDock"))
    try:
        history = json.loads(hist_path.read_text("utf-8"))
    except Exception:
        history = []

    ffmpeg = None if is_android else find_ffmpeg()
    sem = {"v": threading.Semaphore(int(cfg["concurrency"]))}
    page.theme_mode = ft.ThemeMode.DARK if cfg["dark"] else ft.ThemeMode.LIGHT

    def save_cfg():
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), "utf-8")

    def toast(msg):
        page.open(ft.SnackBar(ft.Text(msg), behavior=ft.SnackBarBehavior.FLOATING))

    def open_path(p):
        try:
            if sys.platform.startswith("win"):
                os.startfile(p)  # noqa
            elif sys.platform == "darwin":
                subprocess.Popen(["open", p])
            elif is_android:
                page.launch_url(Path(p).as_uri())
            else:
                subprocess.Popen(["xdg-open", p])
        except Exception:
            toast("열 수 없습니다. 파일 앱에서 확인하세요")

    def ensure_outdir():
        try:
            Path(cfg["out_dir"]).mkdir(parents=True, exist_ok=True)
            return cfg["out_dir"]
        except Exception:
            fb = data_dir / "downloads"
            fb.mkdir(exist_ok=True)
            toast("저장 폴더 권한 없음 → 앱 내부 폴더에 저장 (모든 파일 접근을 허용하세요)")
            return str(fb)

    # ================= 다운로드 엔진 =================
    def build_opts(hook):
        q = QUALITY.get(cfg["quality"])
        if q == "audio":
            fmt = "ba/b"
        elif isinstance(q, int):
            fmt = f"bv*[height<={q}]+ba/b[height<={q}]/b" if ffmpeg else f"b[height<={q}]/b"
        else:
            fmt = "bv*+ba/b" if ffmpeg else "b"
        o = {
            "outtmpl": os.path.join(ensure_outdir(), TEMPLATES.get(cfg["template"], TEMPLATES["계정/날짜_ID"])),
            "format": fmt, "format_sort": ["res", "fps", "br"], "merge_output_format": "mp4",
            "progress_hooks": [hook], "quiet": True, "no_warnings": True, "retries": 5,
            "fragment_retries": 5, "writethumbnail": bool(cfg["thumb"]), "windowsfilenames": True,
        }
        if ffmpeg:
            o["ffmpeg_location"] = ffmpeg
        if cfg["skip_dup"]:
            o["download_archive"] = str(archive_path)
        sid = cfg["sessionid"].strip()
        if sid:
            cookie_path.write_text("# Netscape HTTP Cookie File\n.instagram.com\tTRUE\t/\tTRUE\t2147483647\tsessionid\t"
                                   + sid + "\n", "utf-8")
            o["cookiefile"] = str(cookie_path)
        elif not is_android and cfg["browser"] != "없음":
            o["cookiesfrombrowser"] = (cfg["browser"].lower(),)
        return o

    def friendly_error(e):
        low = str(e).lower()
        if any(k in low for k in ("login", "cookie", "private", "not available", "empty media")):
            return "로그인 필요/비공개 → 설정에서 sessionid 입력"
        if "rate" in low or "429" in low:
            return "요청 제한 → 잠시 후 다시 시도"
        return str(e).split("\n")[-1][:120]

    def paths_from(info):
        out = []
        for e in (info.get("entries") or [info]):
            for r in (e or {}).get("requested_downloads") or []:
                if r.get("filepath"):
                    out.append(r["filepath"])
        return out

    jobs_col = ft.Column(spacing=10)

    def job_card(title_txt, status_txt="", progress=0, color=None):
        title = ft.Text(title_txt, size=14, weight=ft.FontWeight.W_600, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)
        status = ft.Text(status_txt, size=12, color=color or ft.Colors.ON_SURFACE_VARIANT)
        bar = ft.ProgressBar(value=progress, height=6, border_radius=3)
        card = ft.Container(
            padding=14, border_radius=18, bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
            content=ft.Row([ft.Icon(I.MOVIE_CREATION_OUTLINED, size=30, color=SEED),
                            ft.Column([title, status, bar], spacing=4, expand=True)], spacing=12),
        )
        return card, title, status, bar

    def add_history(title, url, paths):
        history.insert(0, {"title": title, "url": url, "paths": paths, "time": time.strftime("%m-%d %H:%M")})
        del history[100:]
        if cfg["remember"]:
            hist_path.write_text(json.dumps(history, ensure_ascii=False), "utf-8")

    gdl_lock = threading.Lock()  # gallery-dl 설정은 전역이라 한 번에 하나씩 (계정 보호에도 유리)

    def gdl_download(url, include):
        if gdl_job is None:
            raise RuntimeError("gallery-dl을 불러오지 못했어요")
        out = ensure_outdir()
        t0 = time.time()
        with gdl_lock:
            gdl_config.clear()
            gdl_config.set((), "base-directory", out)
            ig = ("extractor", "instagram")
            gdl_config.set(ig, "directory", ["{username}"])
            gdl_config.set(ig, "include", include or "posts")
            gdl_config.set(ig, "sleep-request", [1.0, 3.0])
            if cfg["skip_dup"]:
                gdl_config.set(("extractor",), "archive", str(data_dir / "gdl-archive.sqlite3"))
            sid = cfg["sessionid"].strip()
            if sid:
                gdl_config.set(ig, "cookies", {"sessionid": sid})
            elif not is_android and cfg["browser"] != "없음":
                gdl_config.set(ig, "cookies", [cfg["browser"].lower()])
            status = gdl_job.DownloadJob(url).run()
        files = []
        for p in Path(out).rglob("*"):
            try:
                if p.is_file() and p.stat().st_mtime >= t0 - 1:
                    files.append(str(p))
            except Exception:
                pass
        if not files:
            if status & 32:
                raise RuntimeError("로그인 필요 → 설정에서 sessionid 입력")
            if status & 8:
                raise RuntimeError("게시물을 찾을 수 없어요 (삭제/비공개/로그인 필요)")
            if status & 4:
                raise RuntimeError("요청 제한 또는 네트워크 오류 → 잠시 후 다시 시도")
            if status == 0:
                return files  # 이미 받은 항목
            raise RuntimeError(f"받을 수 있는 파일이 없어요 (코드 {status})")
        return files

    def start_job(url, include=None):
        card, title, status, bar = job_card(url, "대기 중...", None)
        jobs_col.controls.insert(0, card)
        page.update()

        def setp(txt, val=None, color=None):
            status.value, bar.value = txt, val
            if color:
                status.color = color
            page.update()

        def hook(d):
            if d["status"] == "downloading":
                tot = d.get("total_bytes") or d.get("total_bytes_estimate")
                p = (d.get("downloaded_bytes") or 0) / tot if tot else None
                sp = d.get("speed")
                setp(f"{(p or 0) * 100:.0f}%  " + (f"{sp / 1048576:.1f}MB/s" if sp else ""), p)
            elif d["status"] == "finished":
                setp("처리 중...")

        def by_ytdlp():
            with yt_dlp.YoutubeDL(build_opts(hook)) as y:
                info = y.extract_info(url, download=True)
            if info is None:
                return None, []
            return str(info.get("title") or info.get("id") or url)[:80], paths_from(info)

        def by_gdl():
            setp("게시물/사진 받는 중...")
            return None, gdl_download(url, include)

        def worker():
            with sem["v"]:
                setp("연결 중...")
                # 릴스는 yt-dlp(원본 영상+음성 병합) 우선, 그 외(사진·캐러셀·스토리·계정)는 gallery-dl 우선
                is_reel = re.search(r"/(reel|reels|tv)/", url) and not include
                order = [by_ytdlp, by_gdl] if is_reel else [by_gdl, by_ytdlp]
                if include:
                    order = [by_gdl]
                first_err = None
                for fn in order:
                    try:
                        t, ps = fn()
                        title.value = t or (ps[0].replace("\\", "/").split("/")[-1] if ps else url)[:80]
                        if ps:
                            setp(f"완료 ✓ ({len(ps)}개)", 1, ft.Colors.GREEN)
                            add_history(title.value, url, ps)
                        else:
                            setp("이미 받은 항목 (건너뜀)", 1, ft.Colors.AMBER)
                        return
                    except Exception as e:
                        first_err = first_err or e
                setp("실패: " + friendly_error(first_err), 0, ft.Colors.RED_300)

        threading.Thread(target=worker, daemon=True).start()

    if cfg["remember"]:
        for h in history[:5]:
            c, t, s, b = job_card(h["title"], f'{h["time"]} · {len(h.get("paths") or [])}개 · 완료', 1)
            jobs_col.controls.append(c)

    # ================= ffmpeg 도구 =================
    picker = ft.FilePicker()
    page.overlay.append(picker)
    dir_picker = ft.FilePicker()
    page.overlay.append(dir_picker)

    def run_tool(spec, src, params):
        tid, name, _, _, _, ext, suffix, build = spec
        sp = Path(src)
        e = params.get("fmt") if ext == "fmt" else (ext or sp.suffix.lstrip("."))
        outdir = Path(cfg["out_dir"]) / "tools"
        outdir.mkdir(parents=True, exist_ok=True)
        out = str(outdir / f"{sp.stem}{suffix}.{e}")
        dlg = ft.AlertDialog(modal=True, title=ft.Text(name),
                             content=ft.Row([ft.ProgressRing(), ft.Text("처리 중... (기기에서만 실행)")], spacing=16))
        page.open(dlg)

        def work():
            try:
                flags = subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0
                r = subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error"] + build(src, out, params),
                                   capture_output=True, text=True, creationflags=flags)
                page.close(dlg)
                if r.returncode == 0:
                    toast(f"완료: {outdir}")
                    open_path(str(outdir))
                else:
                    toast("실패: " + (r.stderr.strip().split("\n")[-1][:100] or "ffmpeg 오류"))
            except Exception as ex:
                page.close(dlg)
                toast(f"실패: {ex}")

        threading.Thread(target=work, daemon=True).start()

    def ask_params(spec, src):
        fields = {}
        for key, label, default, opts in spec[4]:
            fields[key] = (ft.Dropdown(label=label, value=default, options=[ft.dropdown.Option(o) for o in opts])
                           if opts else ft.TextField(label=label, value=default))

        def go(e):
            page.close(dlg)
            run_tool(spec, src, {k: f.value for k, f in fields.items()})

        dlg = ft.AlertDialog(title=ft.Text(spec[1]), content=ft.Column(
            [ft.Text(Path(src).name, size=12, color=ft.Colors.ON_SURFACE_VARIANT)] + list(fields.values()),
            tight=True, spacing=12),
            actions=[ft.TextButton("취소", on_click=lambda e: page.close(dlg)), ft.FilledButton("시작", on_click=go)])
        page.open(dlg)

    def open_tool(spec):
        if not ffmpeg:
            toast("이 도구는 ffmpeg가 필요해 PC 버전에서만 사용할 수 있어요")
            return

        def picked(e):
            if e.files:
                ask_params(spec, e.files[0].path)

        picker.on_result = picked
        picker.pick_files(dialog_title=spec[1], allow_multiple=False)

    # ================= 갤러리 / 금고 =================
    body = ft.Container(expand=True)

    def move_file(p, dest_dir):
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(p), str(dest_dir / p.name))

    def file_tile(p, menu):
        try:
            st = p.stat()
            sub = f"{human(st.st_size)} · {time.strftime('%m-%d %H:%M', time.localtime(st.st_mtime))}"
        except Exception:
            sub = ""
        lead = (ft.Image(src=str(p), width=52, height=52, fit=ft.ImageFit.COVER, border_radius=10)
                if p.suffix.lower() in IMG else
                ft.Container(ft.Icon(I.AUDIOTRACK if p.suffix.lower() in {".m4a", ".mp3", ".wav", ".flac"}
                                     else I.PLAY_CIRCLE_OUTLINE, color=SEED), width=52, height=52,
                             border_radius=10, bgcolor=ft.Colors.SECONDARY_CONTAINER, alignment=ft.alignment.center))
        return ft.ListTile(leading=lead, title=ft.Text(p.name, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                           subtitle=ft.Text(sub), trailing=ft.PopupMenuButton(items=menu),
                           on_click=lambda e, q=p: open_path(str(q)))

    def title_bar(text, back=False):
        return ft.Row([ft.IconButton(I.ARROW_BACK, on_click=lambda e: show(1)) if back else ft.Container(width=4),
                       ft.Text(text, size=26, weight=ft.FontWeight.BOLD)])

    def gallery_view():
        root, files = Path(cfg["out_dir"]), []
        if root.exists():
            for p in root.rglob("*"):
                try:
                    if p.is_file() and p.suffix.lower() in MEDIA:
                        files.append((p.stat().st_mtime, p))
                except Exception:
                    pass
        files.sort(key=lambda x: x[0], reverse=True)
        lv = ft.ListView(expand=True, spacing=2)

        def delete(p):
            p.unlink(missing_ok=True)
            show(1)

        def to_vault(p):
            if not unlock_vault(lambda: (move_file(p, vault_dir), show(1))):
                return

        for _, p in files[:150]:
            lv.controls.append(file_tile(p, [
                ft.PopupMenuItem(text="열기", icon=I.OPEN_IN_NEW, on_click=lambda e, q=p: open_path(str(q))),
                ft.PopupMenuItem(text="금고로 이동", icon=I.LOCK, on_click=lambda e, q=p: to_vault(q)),
                ft.PopupMenuItem(text="삭제", icon=I.DELETE_OUTLINE, on_click=lambda e, q=p: delete(q)),
            ]))
        if not files:
            lv.controls.append(ft.Container(ft.Text("아직 받은 파일이 없어요", color=ft.Colors.ON_SURFACE_VARIANT),
                                            padding=30, alignment=ft.alignment.center))
        return ft.Column([title_bar("갤러리"), ft.Text(str(root), size=11, color=ft.Colors.ON_SURFACE_VARIANT), lv],
                         expand=True)

    def vault_view():
        lv = ft.ListView(expand=True, spacing=2)
        for p in sorted(vault_dir.iterdir()):
            if p.is_file():
                lv.controls.append(file_tile(p, [
                    ft.PopupMenuItem(text="복원", icon=I.RESTORE, on_click=lambda e, q=p: (
                        move_file(q, Path(cfg["out_dir"])), show_custom(vault_view()))),
                    ft.PopupMenuItem(text="삭제", icon=I.DELETE_OUTLINE, on_click=lambda e, q=p: (
                        q.unlink(missing_ok=True), show_custom(vault_view()))),
                ]))
        if not lv.controls:
            lv.controls.append(ft.Container(ft.Text("금고가 비어 있어요", color=ft.Colors.ON_SURFACE_VARIANT),
                                            padding=30, alignment=ft.alignment.center))
        return ft.Column([ft.Row([ft.IconButton(I.ARROW_BACK, on_click=lambda e: show(2)),
                                  ft.Text("비공개 금고", size=26, weight=ft.FontWeight.BOLD)]),
                          ft.Text("PIN 뒤에 숨겨진 앱 전용 폴더 (암호화는 아님)", size=11,
                                  color=ft.Colors.ON_SURFACE_VARIANT), lv], expand=True)

    def unlock_vault(then):
        first = not cfg["pin_hash"]
        pin = ft.TextField(label="새 PIN (4자리 이상)" if first else "PIN 입력", password=True,
                           keyboard_type=ft.KeyboardType.NUMBER, autofocus=True)

        def ok(e):
            h = hashlib.sha256(("reeldock" + pin.value).encode()).hexdigest()
            if first:
                if len(pin.value) < 4:
                    pin.error_text = "4자리 이상"
                    page.update()
                    return
                cfg["pin_hash"] = h
                save_cfg()
            elif h != cfg["pin_hash"]:
                pin.error_text = "PIN이 틀렸어요"
                page.update()
                return
            page.close(dlg)
            then()

        dlg = ft.AlertDialog(title=ft.Text("비공개 금고"), content=pin,
                             actions=[ft.TextButton("취소", on_click=lambda e: page.close(dlg)),
                                      ft.FilledButton("확인", on_click=ok)])
        page.open(dlg)
        return True

    def open_vault(e=None):
        unlock_vault(lambda: show_custom(vault_view()))

    # ================= 홈 =================
    url_field = ft.TextField(hint_text="게시물·릴스·스토리 링크 붙여넣기 (여러 개 가능)", multiline=True, min_lines=1,
                             max_lines=4, filled=True, bgcolor="#FFFFFF", color="#1B1B1F", border_radius=16,
                             border=ft.InputBorder.NONE, hint_style=ft.TextStyle(color="#7A7A85", size=13),
                             prefix_icon=I.LINK)

    def paste(e):
        try:
            t = page.get_clipboard()
            if t:
                url_field.value = (url_field.value + "\n" + t).strip()
                page.update()
        except Exception:
            toast("클립보드를 읽을 수 없어요. 길게 눌러 붙여넣기 하세요")

    def download(e):
        urls = list(dict.fromkeys(URL_RE.findall(url_field.value or "")))
        if not urls:
            toast("인스타그램 링크를 찾지 못했어요")
            return
        for u in urls:
            start_job(u.rstrip(").,"))
        url_field.value = ""
        page.update()

    ACCT = {"스토리": "stories", "하이라이트": "highlights", "게시물 전체": "posts",
            "릴스 전체": "reels", "전부 (게시물+릴스+스토리+하이라이트)": "posts,reels,stories,highlights"}

    def story_dialog(e=None):
        u = ft.TextField(label="사용자명 (@ 제외)", autofocus=True)
        kind = ft.Dropdown(label="받을 항목", value="스토리", options=[ft.dropdown.Option(k) for k in ACCT])

        def go(ev):
            name = (u.value or "").strip().lstrip("@")
            if name:
                page.close(dlg)
                if not cfg["sessionid"].strip() and cfg["browser"] == "없음":
                    toast("스토리/전체 받기는 로그인이 필요해요 → 설정에서 sessionid 입력")
                start_job(f"https://www.instagram.com/{name}/", ACCT[kind.value])

        dlg = ft.AlertDialog(title=ft.Text("계정에서 받기"), content=ft.Column([u, kind], tight=True, spacing=12),
                             actions=[ft.TextButton("취소", on_click=lambda e: page.close(dlg)),
                                      ft.FilledButton("받기", on_click=go)])
        page.open(dlg)

    def quality_change(e):
        cfg["quality"] = e.control.value
        save_cfg()

    hero = ft.Container(
        padding=18, border_radius=28,
        gradient=ft.LinearGradient(begin=ft.alignment.top_left, end=ft.alignment.bottom_right,
                                   colors=["#5B6CFF", "#A15BFF"]),
        content=ft.Column([
            ft.Text("링크만 붙여넣으세요", size=22, weight=ft.FontWeight.BOLD, color="#FFFFFF"),
            ft.Text("사진·영상 모두 재인코딩 없이 원본 그대로 저장해요", size=12, color="#E6E6FF"),
            url_field,
            ft.Row([
                ft.ElevatedButton("붙여넣기", icon=I.CONTENT_PASTE, on_click=paste, expand=True, height=46,
                                  style=ft.ButtonStyle(bgcolor="#33FFFFFF", color="#FFFFFF",
                                                       shape=ft.RoundedRectangleBorder(radius=23))),
                ft.ElevatedButton("다운로드", icon=I.DOWNLOAD, on_click=download, expand=True, height=46,
                                  style=ft.ButtonStyle(bgcolor="#FFFFFF", color="#4A55E0",
                                                       shape=ft.RoundedRectangleBorder(radius=23))),
            ], spacing=10),
        ], spacing=10),
    )

    def tile(icon, label, on_click):
        return ft.Container(
            on_click=on_click, ink=True, border_radius=18, padding=8, expand=True,
            content=ft.Column([
                ft.Container(ft.Icon(icon, color=SEED, size=26), width=54, height=54, border_radius=27,
                             bgcolor=ft.Colors.SECONDARY_CONTAINER, alignment=ft.alignment.center),
                ft.Text(label, size=12, text_align=ft.TextAlign.CENTER)],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=6))

    t_all = {t[0]: t for g in TOOLS.values() for t in g}
    quick = ft.Container(
        padding=10, border_radius=24, bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
        content=ft.Column([
            ft.Row([tile(I.PHOTO_LIBRARY, "갤러리", lambda e: show(1)),
                    tile(I.AUTO_STORIES, "계정/스토리", story_dialog),
                    tile(I.MUSIC_NOTE, "오디오 추출", lambda e: open_tool(t_all["aext"]))]),
            ft.Row([tile(I.CONTENT_CUT, "동영상 자르기", lambda e: open_tool(t_all["trim"])),
                    tile(I.COMPRESS, "동영상 압축", lambda e: open_tool(t_all["compress"])),
                    tile(I.LOCK, "비공개 금고", open_vault)]),
        ]))

    def toggle_dark(e):
        cfg["dark"] = not cfg["dark"]
        page.theme_mode = ft.ThemeMode.DARK if cfg["dark"] else ft.ThemeMode.LIGHT
        save_cfg()
        page.update()

    home = ft.Column([
        ft.Row([ft.Text("ReelDock", size=28, weight=ft.FontWeight.BOLD, expand=True),
                ft.IconButton(I.DARK_MODE_OUTLINED, on_click=toggle_dark)]),
        hero,
        ft.Dropdown(label="화질", value=cfg["quality"], border_radius=16, on_change=quality_change,
                    options=[ft.dropdown.Option(k) for k in QUALITY]),
        ft.Text("빠른 도구", size=16, weight=ft.FontWeight.W_600), quick,
        ft.Text("최근 다운로드", size=16, weight=ft.FontWeight.W_600), jobs_col,
        ft.Container(ft.Text("모든 작업은 기기에서 실행되며 아무것도 업로드되지 않아요", size=11,
                             color=ft.Colors.ON_SURFACE_VARIANT, text_align=ft.TextAlign.CENTER),
                     padding=12, alignment=ft.alignment.center),
    ], spacing=14, scroll=ft.ScrollMode.AUTO, expand=True)

    # ================= 도구 탭 =================
    def tools_view():
        col = [title_bar("도구")]
        if not ffmpeg:
            col.append(ft.Container(ft.Text("휴대폰에는 ffmpeg가 없어 편집 도구는 PC 버전에서 사용할 수 있어요",
                                            size=12), padding=12, border_radius=16,
                                    bgcolor=ft.Colors.TERTIARY_CONTAINER))
        for group, items in TOOLS.items():
            col.append(ft.Text(group, size=14, color=SEED, weight=ft.FontWeight.W_600))
            for spec in items:
                col.append(ft.Container(
                    ink=True, border_radius=20, padding=14, bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
                    on_click=lambda e, s=spec: open_tool(s),
                    content=ft.Row([ft.Container(ft.Icon(spec[3], color=SEED), width=46, height=46, border_radius=14,
                                                 bgcolor=ft.Colors.SECONDARY_CONTAINER,
                                                 alignment=ft.alignment.center),
                                    ft.Column([ft.Text(spec[1], weight=ft.FontWeight.W_600),
                                               ft.Text(spec[2], size=12, color=ft.Colors.ON_SURFACE_VARIANT)],
                                              spacing=2, expand=True), ft.Icon(I.CHEVRON_RIGHT)], spacing=14)))
        col.append(ft.Text("개인정보", size=14, color=SEED, weight=ft.FontWeight.W_600))
        col.append(ft.Container(
            ink=True, border_radius=20, padding=14, bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST, on_click=open_vault,
            content=ft.Row([ft.Icon(I.LOCK, color=SEED), ft.Column([
                ft.Text("비공개 금고", weight=ft.FontWeight.W_600),
                ft.Text("PIN 뒤에 파일 숨기기", size=12, color=ft.Colors.ON_SURFACE_VARIANT)], spacing=2, expand=True),
                ft.Icon(I.CHEVRON_RIGHT)], spacing=14)))
        return ft.Column(col, spacing=10, scroll=ft.ScrollMode.AUTO, expand=True)

    # ================= 설정 탭 =================
    def bind(key):
        def h(e):
            cfg[key] = e.control.value
            save_cfg()
        return h

    def settings_view():
        out_field = ft.TextField(label="저장 폴더", value=cfg["out_dir"], on_change=bind("out_dir"), expand=True)
        dir_picker.on_result = lambda e: e.path and (setattr(out_field, "value", e.path),
                                                     cfg.update(out_dir=e.path), save_cfg(), page.update())

        def conc(e):
            cfg["concurrency"] = int(e.control.value)
            sem["v"] = threading.Semaphore(cfg["concurrency"])
            save_cfg()

        def clear_hist(e):
            history.clear()
            hist_path.write_text("[]", "utf-8")
            toast("기록을 지웠어요 (파일은 그대로)")

        items = [
            title_bar("설정"),
            ft.Row([out_field, ft.IconButton(I.FOLDER_OPEN, on_click=lambda e: dir_picker.get_directory_path())]),
            ft.TextField(label="인스타 sessionid (스토리/로그인 필요 콘텐츠용)", value=cfg["sessionid"], password=True,
                         can_reveal_password=True, on_change=bind("sessionid"),
                         helper_text="비밀번호와 같으니 공유 금지. 가능하면 부계정을 쓰세요."),
        ]
        if not is_android:
            items.append(ft.Dropdown(label="또는 브라우저 쿠키 사용", value=cfg["browser"], on_change=bind("browser"),
                                     options=[ft.dropdown.Option(b) for b in
                                              ["없음", "Chrome", "Edge", "Firefox", "Brave"]]))
        items += [
            ft.Dropdown(label="파일명 형식", value=cfg["template"], on_change=bind("template"),
                        options=[ft.dropdown.Option(k) for k in TEMPLATES]),
            ft.Text("동시 다운로드 수"),
            ft.Slider(min=1, max=3, divisions=2, value=cfg["concurrency"], label="{value}", on_change_end=conc),
            ft.Switch(label="이미 받은 항목 건너뛰기", value=cfg["skip_dup"], on_change=bind("skip_dup")),
            ft.Switch(label="썸네일도 저장", value=cfg["thumb"], on_change=bind("thumb")),
            ft.Switch(label="최근 다운로드 기억하기", value=cfg["remember"], on_change=bind("remember")),
            ft.OutlinedButton("기록 지우기", icon=I.DELETE_OUTLINE, on_click=clear_hist),
            ft.Text(f"ffmpeg: {'사용 가능' if ffmpeg else '없음 (단일 스트림 최고 화질)'} · "
                    f"yt-dlp {yt_dlp.version.__version__}", size=12, color=ft.Colors.ON_SURFACE_VARIANT),
            ft.Text("본인 콘텐츠 또는 허락받은 콘텐츠에만 사용하세요.", size=12, color=ft.Colors.ON_SURFACE_VARIANT),
        ]
        return ft.Column(items, spacing=12, scroll=ft.ScrollMode.AUTO, expand=True)

    # ================= 네비게이션 =================
    builders = [lambda: home, gallery_view, tools_view, settings_view]

    def show(i):
        nav.selected_index = i
        body.content = ft.Container(builders[i](), padding=ft.padding.only(left=16, right=16, top=10), expand=True)
        page.update()

    def show_custom(ctrl):
        body.content = ft.Container(ctrl, padding=ft.padding.only(left=16, right=16, top=10), expand=True)
        page.update()

    nav = ft.NavigationBar(
        selected_index=0, on_change=lambda e: show(e.control.selected_index),
        destinations=[
            ft.NavigationBarDestination(icon=I.DOWNLOAD_OUTLINED, selected_icon=I.DOWNLOAD, label="다운로드"),
            ft.NavigationBarDestination(icon=I.PHOTO_LIBRARY_OUTLINED, selected_icon=I.PHOTO_LIBRARY, label="갤러리"),
            ft.NavigationBarDestination(icon=I.BUILD_OUTLINED, selected_icon=I.BUILD, label="도구"),
            ft.NavigationBarDestination(icon=I.SETTINGS_OUTLINED, selected_icon=I.SETTINGS, label="설정"),
        ])
    page.navigation_bar = nav
    page.add(ft.SafeArea(body, expand=True))
    show(0)


if __name__ == "__main__":
    ft.app(target=main)
