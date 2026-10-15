"""IGSaver - 인스타그램 릴스/스토리 원본 화질 다운로더 (Windows EXE / Android APK)"""
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

URL_RE = re.compile(r"https?://(?:www\.)?instagram\.com/[^\s]+", re.I)

DEFAULTS = {
    "out_dir": "",
    "sessionid": "",
    "browser": "없음",
    "concurrency": 2,
    "audio_only": False,
    "thumb": False,
    "skip_dup": True,
    "template": "계정/날짜_ID",
}
TEMPLATES = {
    "계정/날짜_ID": "%(uploader)s/%(upload_date)s_%(id)s.%(ext)s",
    "날짜_계정_ID (폴더 없음)": "%(upload_date)s_%(uploader)s_%(id)s.%(ext)s",
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


def main(page: ft.Page):
    page.title = "IGSaver"
    page.theme_mode = ft.ThemeMode.DARK
    page.padding = 10
    is_android = page.platform == ft.PagePlatform.ANDROID

    data_dir = Path(os.environ.get("FLET_APP_STORAGE_DATA") or (Path.home() / ".igsaver"))
    data_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = data_dir / "settings.json"
    hist_path = data_dir / "history.json"
    archive_path = data_dir / "archive.txt"
    cookie_path = data_dir / "cookies.txt"

    cfg = dict(DEFAULTS)
    if cfg_path.exists():
        try:
            cfg.update(json.loads(cfg_path.read_text("utf-8")))
        except Exception:
            pass
    if not cfg["out_dir"]:
        cfg["out_dir"] = (
            "/storage/emulated/0/Download/IGSaver" if is_android
            else str(Path.home() / "Downloads" / "IGSaver")
        )
    history = []
    if hist_path.exists():
        try:
            history = json.loads(hist_path.read_text("utf-8"))
        except Exception:
            history = []

    ffmpeg = None if is_android else find_ffmpeg()
    sem_holder = {"sem": threading.Semaphore(int(cfg["concurrency"]))}

    def save_cfg():
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), "utf-8")

    def toast(msg):
        page.open(ft.SnackBar(ft.Text(msg)))

    def write_cookie_file():
        sid = cfg["sessionid"].strip()
        if not sid:
            return None
        cookie_path.write_text(
            "# Netscape HTTP Cookie File\n"
            f".instagram.com\tTRUE\t/\tTRUE\t2147483647\tsessionid\t{sid}\n",
            "utf-8",
        )
        return str(cookie_path)

    def ensure_outdir():
        try:
            Path(cfg["out_dir"]).mkdir(parents=True, exist_ok=True)
            return cfg["out_dir"]
        except Exception:
            fb = data_dir / "downloads"
            fb.mkdir(exist_ok=True)
            toast("저장 폴더 권한 없음 → 앱 내부 폴더에 저장합니다 (모든 파일 접근 권한을 허용하세요)")
            return str(fb)

    def build_opts(hook):
        out = ensure_outdir()
        o = {
            "outtmpl": os.path.join(out, TEMPLATES.get(cfg["template"], TEMPLATES["계정/날짜_ID"])),
            # 원본 화질/fps 유지: 재인코딩 없이 해상도→fps→비트레이트 순으로 최고 선택
            "format": "ba/b" if cfg["audio_only"] else ("bv*+ba/b" if ffmpeg else "b"),
            "format_sort": ["res", "fps", "br"],
            "merge_output_format": "mp4",
            "progress_hooks": [hook],
            "quiet": True,
            "no_warnings": True,
            "retries": 5,
            "fragment_retries": 5,
            "writethumbnail": bool(cfg["thumb"]),
            "restrictfilenames": False,
            "windowsfilenames": True,
        }
        if ffmpeg:
            o["ffmpeg_location"] = ffmpeg
        if cfg["skip_dup"]:
            o["download_archive"] = str(archive_path)
        ck = write_cookie_file()
        if ck:
            o["cookiefile"] = ck
        elif not is_android and cfg["browser"] != "없음":
            o["cookiesfrombrowser"] = (cfg["browser"].lower(),)
        return o

    # ---------------- 다운로드 큐 ----------------
    queue_view = ft.ListView(expand=True, spacing=8, auto_scroll=False)

    def add_history(title, url, paths):
        history.insert(0, {
            "title": title, "url": url, "paths": paths,
            "time": time.strftime("%Y-%m-%d %H:%M"),
        })
        del history[300:]
        hist_path.write_text(json.dumps(history, ensure_ascii=False), "utf-8")
        refresh_history()

    def friendly_error(e):
        s = str(e)
        low = s.lower()
        if any(k in low for k in ("login", "cookie", "private", "not available", "empty media")):
            return "로그인 필요/비공개 → 설정에서 sessionid를 입력하세요"
        if "rate" in low or "429" in low:
            return "요청 제한(rate-limit) → 잠시 후 다시 시도"
        if "already been recorded" in low:
            return "이미 받은 항목 (중복 건너뜀)"
        return s.split("\n")[-1][:140]

    def paths_from(info):
        out = []
        for e in (info.get("entries") or [info]):
            if not e:
                continue
            for r in e.get("requested_downloads") or []:
                if r.get("filepath"):
                    out.append(r["filepath"])
        return out

    def start_job(url):
        title = ft.Text(url, size=13, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)
        status = ft.Text("대기 중...", size=12, color=ft.Colors.GREY_400)
        bar = ft.ProgressBar(value=0, height=6)
        card = ft.Card(content=ft.Container(padding=12, content=ft.Column([title, status, bar], spacing=4)))
        queue_view.controls.insert(0, card)
        page.update()

        def setp(txt, val=None, color=None):
            status.value = txt
            if color:
                status.color = color
            bar.value = val
            page.update()

        def worker():
            with sem_holder["sem"]:
                try:
                    setp("연결 중...", None)

                    def hook(d):
                        if d["status"] == "downloading":
                            tot = d.get("total_bytes") or d.get("total_bytes_estimate")
                            done = d.get("downloaded_bytes") or 0
                            p = (done / tot) if tot else None
                            sp = d.get("speed")
                            sps = f"{sp/1048576:.1f}MB/s" if sp else ""
                            setp(f"{(p or 0)*100:.0f}%  {sps}", p)
                        elif d["status"] == "finished":
                            setp("처리 중(병합)...", None)

                    with yt_dlp.YoutubeDL(build_opts(hook)) as y:
                        info = y.extract_info(url, download=True)
                    if info is None:
                        setp("이미 받은 항목 (중복 건너뜀)", 1, ft.Colors.AMBER)
                        return
                    t = info.get("title") or info.get("id") or url
                    title.value = str(t)[:80]
                    ps = paths_from(info)
                    setp(f"완료 ✓ ({len(ps)}개)", 1, ft.Colors.GREEN)
                    add_history(str(t)[:80], url, ps)
                except Exception as e:
                    setp("실패: " + friendly_error(e), 0, ft.Colors.RED_300)

        threading.Thread(target=worker, daemon=True).start()

    url_field = ft.TextField(
        label="릴스/게시물/스토리 링크 (여러 개 붙여넣기 가능)",
        multiline=True, min_lines=2, max_lines=5,
    )
    story_user = ft.TextField(label="스토리 전체 받기: 사용자명 (@ 없이)", expand=True)

    def paste(e):
        try:
            txt = page.get_clipboard()
            if txt:
                url_field.value = (url_field.value + "\n" + txt).strip()
                page.update()
        except Exception:
            toast("클립보드를 읽을 수 없습니다. 길게 눌러 붙여넣기 하세요")

    def download(e):
        urls = list(dict.fromkeys(URL_RE.findall(url_field.value or "")))
        if not urls:
            toast("인스타그램 링크를 찾지 못했습니다")
            return
        for u in urls:
            start_job(u.rstrip(").,"))
        url_field.value = ""
        page.update()

    def download_story(e):
        u = (story_user.value or "").strip().lstrip("@")
        if not u:
            return
        if not cfg["sessionid"].strip() and cfg["browser"] == "없음":
            toast("스토리는 로그인이 필요합니다 → 설정에서 sessionid 입력")
        start_job(f"https://www.instagram.com/stories/{u}/")
        story_user.value = ""
        page.update()

    tab_dl = ft.Column([
        url_field,
        ft.Row([
            ft.OutlinedButton("붙여넣기", icon=ft.Icons.CONTENT_PASTE, on_click=paste),
            ft.ElevatedButton("다운로드", icon=ft.Icons.DOWNLOAD, on_click=download, expand=True),
        ]),
        ft.Row([story_user, ft.IconButton(ft.Icons.AUTO_STORIES, tooltip="스토리 받기", on_click=download_story)]),
        ft.Divider(),
        queue_view,
    ], expand=True)

    # ---------------- 기록 ----------------
    hist_view = ft.ListView(expand=True, spacing=4)

    def open_path(p):
        try:
            if sys.platform.startswith("win"):
                os.startfile(p)  # noqa
            elif sys.platform == "darwin":
                subprocess.Popen(["open", p])
            else:
                subprocess.Popen(["xdg-open", p])
        except Exception:
            toast("열 수 없습니다")

    def refresh_history():
        hist_view.controls.clear()
        for h in history:
            first = (h.get("paths") or [None])[0]
            trailing = None
            if first and not is_android:
                trailing = ft.IconButton(
                    ft.Icons.FOLDER_OPEN,
                    on_click=lambda e, p=os.path.dirname(first): open_path(p),
                )
            hist_view.controls.append(ft.ListTile(
                title=ft.Text(h["title"], max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                subtitle=ft.Text(f'{h["time"]} · {len(h.get("paths") or [])}개'),
                trailing=trailing,
            ))
        try:
            page.update()
        except Exception:
            pass

    def clear_history(e):
        history.clear()
        hist_path.write_text("[]", "utf-8")
        refresh_history()

    tab_hist = ft.Column([
        ft.Row([ft.TextButton("기록 지우기", icon=ft.Icons.DELETE_OUTLINE, on_click=clear_history)]),
        hist_view,
    ], expand=True)

    # ---------------- 설정 ----------------
    def bind(key, conv=lambda v: v):
        def _h(e):
            cfg[key] = conv(e.control.value)
            save_cfg()
        return _h

    out_field = ft.TextField(label="저장 폴더", value=cfg["out_dir"], on_change=bind("out_dir"), expand=True)
    picker = ft.FilePicker(on_result=lambda e: (
        e.path and (setattr(out_field, "value", e.path), cfg.update(out_dir=e.path), save_cfg(), page.update())
    ))
    page.overlay.append(picker)

    def conc_change(e):
        n = int(e.control.value)
        cfg["concurrency"] = n
        sem_holder["sem"] = threading.Semaphore(n)
        save_cfg()

    settings_controls = [
        ft.Row([out_field, ft.IconButton(ft.Icons.FOLDER, on_click=lambda e: picker.get_directory_path())]),
        ft.TextField(
            label="인스타 sessionid (스토리/비공개 열람용, 선택)", value=cfg["sessionid"],
            password=True, can_reveal_password=True, on_change=bind("sessionid"),
            helper_text="브라우저 로그인 후 쿠키 sessionid 값. 본인 계정에서만 사용하세요.",
        ),
    ]
    if not is_android:
        settings_controls.append(ft.Dropdown(
            label="또는 브라우저 쿠키 자동 사용", value=cfg["browser"],
            options=[ft.dropdown.Option(b) for b in ["없음", "Chrome", "Edge", "Firefox", "Brave"]],
            on_change=bind("browser"),
        ))
    settings_controls += [
        ft.Dropdown(
            label="파일명 형식", value=cfg["template"],
            options=[ft.dropdown.Option(k) for k in TEMPLATES], on_change=bind("template"),
        ),
        ft.Text("동시 다운로드 수"),
        ft.Slider(min=1, max=3, divisions=2, value=cfg["concurrency"], label="{value}", on_change_end=conc_change),
        ft.Switch(label="이미 받은 항목 건너뛰기", value=cfg["skip_dup"], on_change=bind("skip_dup")),
        ft.Switch(label="썸네일도 저장", value=cfg["thumb"], on_change=bind("thumb")),
        ft.Switch(label="오디오만 저장 (원본 m4a)", value=cfg["audio_only"], on_change=bind("audio_only")),
        ft.Text(
            f"ffmpeg: {'사용 가능 (영상+음성 병합, 최고 화질)' if ffmpeg else '없음 (단일 스트림 최고 화질)'}\n"
            f"yt-dlp {yt_dlp.version.__version__}",
            size=12, color=ft.Colors.GREY_500,
        ),
    ]
    tab_set = ft.Column(settings_controls, scroll=ft.ScrollMode.AUTO, expand=True, spacing=12)

    page.add(ft.Tabs(
        expand=True,
        tabs=[
            ft.Tab(text="다운로드", icon=ft.Icons.DOWNLOAD, content=ft.Container(tab_dl, padding=8)),
            ft.Tab(text="기록", icon=ft.Icons.HISTORY, content=ft.Container(tab_hist, padding=8)),
            ft.Tab(text="설정", icon=ft.Icons.SETTINGS, content=ft.Container(tab_set, padding=8)),
        ],
    ))
    refresh_history()


if __name__ == "__main__":
    ft.app(target=main)
