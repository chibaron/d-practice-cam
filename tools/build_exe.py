"""exe を作る（PyInstaller、フォルダ形式、コンソール無し）。

    pip install pyinstaller
    python tools/build_exe.py            # 自分の PC 用（config.yaml・firebase.yaml もコピー）
    python tools/build_exe.py --public   # 公開用（PC ごとの設定を入れず、ライセンス文を同梱して、3つの zip を作る）

dist/d-practice-cam/ に d-practice-cam.exe ができる（--public は dist/public/d-practice-cam/）。フォルダごと配る。
exe のバージョンは作った日（YYYYMMDD。app/version.py）で、画面と公開用 zip の名前に出る。
どちらも build/ の下で作ってから dist/ に中身を入れ替える。自分の PC 用では、exe が書いた laps/（毎日の全ラップ）と
sound/pilot/（パイロットの名前.wav）は残す。公開用の sound/pilot/ には、日本語版の zip に PUBLIC_PILOTS の3つだけを入れる。
exe の隣に sound/、debug.bat、README.md、README_jp.md、docs/images/（README の画像）を置く。
sound/voice/（日本語音声の wav）は自分の PC 用と、公開用の日本語版の zip に入れる。
自分の PC 用では config.yaml・firebase.yaml（あれば）、firebase.example.yaml、docs/firebase.md もコピーする。
Firebase の鍵（serviceAccount.json）はどちらでもコピーしない。

--public では、さらに
  - config.yaml・firebase.yaml（データベースURL、受信器の USB パスなど）を入れない
  - THIRD_PARTY_LICENSES/ に同梱したソフトウェアのライセンス文をまとめる
  - 使わない古い FFmpeg の dll（別の OpenCV のもの）を外す
  - exe を2回作る。Firebase なし（firebase-admin を入れず、app/_build_version.py に FIREBASE = False。
    firebase.yaml を置いてもつながない）と、Firebase あり（firebase.example.yaml・docs/firebase.md も入れる）
  - Firebase なしから
      dist/d-practice-cam_en_YYYYMMDD.zip（英語版。日本語音声を入れない）と
      dist/d-practice-cam_ja_YYYYMMDD.zip（日本語版。英語版に sound/voice/ と、sound/pilot/ の PUBLIC_PILOTS の名前.wav を足したもの）、
    Firebase ありから
      dist/d-practice-cam_ja_firebase_YYYYMMDD.zip（日本語版と同じ音声を足したもの）を作る
    （日本語版は、初めて起動したとき sound/voice の wav を見てアナウンスを日本語で始める。app/config.py）
  - dist/public/d-practice-cam/ は英語版の中身
"""
from __future__ import annotations

import argparse
import datetime
import glob
import importlib.metadata as md
import os
import re
import shutil
import sys
import zipfile

import PyInstaller.__main__

import fgm

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAME = "d-practice-cam"
FGM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(fgm.__file__)))   # fgm の入っているフォルダ
DIST = os.path.join(ROOT, "dist")
# 公開用は別のフォルダに作る（自分の PC 用の exe を使っていても、上書きしたりぶつかったりしない）
PUBLIC_DIST = os.path.join(DIST, "public")
# 公開用はまず人が開かない build/ の下で作って zip にし、そのあと dist/public/ にコピーする
# （dist/public/ のファイルをエディタなどで開いていてフォルダを消せなくても、zip は作れる）
PUBLIC_STAGE = os.path.join(ROOT, "build", "public_stage")
PUBLIC_STAGE_FB = os.path.join(ROOT, "build", "public_stage_firebase")     # 公開用の Firebase あり
STAGE = os.path.join(ROOT, "build", "stage")        # 自分の PC 用も同じく build/ の下で作る
LAPS_DIR = "laps"      # exe が全ラップ（laps.jsonl）を書くフォルダ（app.laplog.LAPS_DIR）
KEEP = (LAPS_DIR, os.path.join("sound", "pilot"))   # 自分の PC 用の dist で exe が書いたもの。作り直しても消さない
# 日本語版の zip の sound/pilot/ に入れるパイロットの名前.wav（VOICEVOX で作ったもの。ほかの名前は入れない）
PUBLIC_PILOTS = ("CHO!", "GORI", "KUMACHAN")
# exe のバージョン（作った日 YYYYMMDD）。app/_build_version.py に書いて exe に入れ、公開用 zip の名前にも使う
VERSION = f"{datetime.date.today():%Y%m%d}"
BUILD_VERSION_PY = os.path.join(ROOT, "app", "_build_version.py")

DEBUG_BAT = (
    "@echo off\r\n"
    "rem コンソールを出してログを見ながら起動する\r\n"
    f"\"%~dp0{NAME}.exe\" --debug %*\r\n"
)

# exe に入るパッケージの起点（requirements.txt）。ここから依存をたどってライセンスを集める
TOP_PACKAGES = ["opencv-contrib-python", "numpy", "PyYAML", "firebase-admin", "cv2-enumerate-cameras", "comtypes",
                "fpv-gate-marker"]
FIREBASE_PACKAGE = "firebase-admin"     # Firebase なしの exe には入れない（ライセンス文も入れない）
# 依存はたどらずに入れるもの（PyInstaller の起動部分が exe に入る。PyInstaller の依存は exe に入らない）
EXTRA_PACKAGES = ["pyinstaller"]
_LICENSE_FILE = re.compile(r"(^|/)(LICEN[CS]E|COPYING|NOTICE)[^/]*$", re.I)

LICENSE_README = """d-practice-cam に同梱しているソフトウェアのライセンス
=========================================================

この exe には、次のソフトウェアが含まれています。各フォルダにライセンス文があります。

{table}

補足
----
- Python 本体（Python Software Foundation License）: python/LICENSE.txt
  （OpenSSL、libffi、zlib などの表記も含みます）。Tcl/Tk: python/tcl-tk-license.terms
- OpenCV（Apache License 2.0）に付属の FFmpeg（opencv_videoio_ffmpeg*.dll）は LGPL 2.1 です。
  dll は _internal/ に別ファイルとして入っており、差し替えられます。FFmpeg の表記とソースの入手先は
  opencv-contrib-python/LICENSE-3RD-PARTY.txt を見てください（FFmpeg のソース: https://ffmpeg.org/ ）。
- certifi（Mozilla Public License 2.0）のソース: https://github.com/certifi/python-certifi
- PyInstaller の起動部分は GPL ですが、例外条項により、PyInstaller で作ったアプリは自由なライセンスで配れます
  （pyinstaller/COPYING.txt の「Bootloader Exception」）。
- Microsoft Visual C++ ランタイム（VCRUNTIME140.dll など）は Microsoft の再配布可能なファイルです。
- 効果音（sound/lap.wav・bestlap.wav・errorlap.wav・boot.wav）は、このアプリのために tools/make_sounds.py で作った音です。
"""

# 日本語版の sound/voice/ に入れる説明（クレジットを含む）
VOICE_README = """d-practice-cam Japanese voice / 日本語音声
=========================================

Japanese announcement voice (wav). Used when Announcement language in the settings (S key) is Japanese.

日本語のアナウンスの声（wav）です。設定画面（S キー）の Announcement language が Japanese のときに使います。

VOICEVOX:四国めたん
"""


def collect_distributions(firebase: bool = True) -> list:
    """TOP_PACKAGES と、その依存をたどったパッケージ（インストール済みのもの）。firebase=False なら firebase-admin を除く。"""
    seen, todo, out = set(), [p for p in TOP_PACKAGES if firebase or p != FIREBASE_PACKAGE], []
    while todo:
        name = re.split(r"[ ;<>=!~\[(]", todo.pop())[0].strip()
        key = name.lower().replace("_", "-")
        if not key or key in seen:
            continue
        seen.add(key)
        try:
            d = md.distribution(name)
        except md.PackageNotFoundError:
            continue
        out.append(d)
        for req in d.requires or []:
            if "extra ==" in req:       # 追加機能の依存は入れない
                continue
            todo.append(req)
    for name in EXTRA_PACKAGES:
        try:
            out.append(md.distribution(name))
        except md.PackageNotFoundError:
            pass
    return sorted(out, key=lambda d: d.metadata["Name"].lower())


def license_name(d) -> str:
    lic = d.metadata.get("License-Expression") or d.metadata.get("License") or ""
    lic = lic.splitlines()[0].strip() if lic else ""
    if not lic or len(lic) > 60:
        cls = [c.split(" :: ")[-1] for c in d.metadata.get_all("Classifier") or [] if c.startswith("License ::")]
        lic = ", ".join(cls) or lic[:60] or "(ライセンス文を参照)"
    return lic


def write_licenses(dest: str, firebase: bool = True) -> None:
    os.makedirs(dest, exist_ok=True)
    rows = []
    for d in collect_distributions(firebase):
        name, ver = d.metadata["Name"], d.version
        files = [f for f in d.files or [] if _LICENSE_FILE.search(str(f).replace("\\", "/"))
                 and ".dist-info" in str(f)]
        files = files or [f for f in d.files or [] if _LICENSE_FILE.search(str(f).replace("\\", "/"))]
        sub = os.path.join(dest, name)
        for f in files:
            src = d.locate_file(f)
            if os.path.isfile(src):
                os.makedirs(sub, exist_ok=True)
                shutil.copy2(src, os.path.join(sub, os.path.basename(str(f))))
        rows.append(f"  {name:28s} {ver:12s} {license_name(d)}")
    # Python 本体と Tcl/Tk
    py = os.path.join(dest, "python")
    os.makedirs(py, exist_ok=True)
    base = sys.base_prefix
    shutil.copy2(os.path.join(base, "LICENSE.txt"), py)
    for lic in glob.glob(os.path.join(base, "tcl", "*", "license.terms"))[:1]:
        shutil.copy2(lic, os.path.join(py, "tcl-tk-license.terms"))
    rows.insert(0, f"  {'Python':28s} {sys.version.split()[0]:12s} PSF License")
    with open(os.path.join(dest, "README.txt"), "w", encoding="utf-8-sig", newline="\r\n") as f:
        f.write(LICENSE_README.format(table="\n".join(rows)))


def remove_unused_ffmpeg(internal: str) -> None:
    """別に入っている OpenCV（opencv-python）の FFmpeg dll は使わないので外す。"""
    import cv2
    keep = "opencv_videoio_ffmpeg" + cv2.__version__.replace(".", "")[:3]
    for dll in glob.glob(os.path.join(internal, "**", "opencv_videoio_ffmpeg*.dll"), recursive=True):
        if not os.path.basename(dll).startswith(keep):
            os.remove(dll)
            print("使わない dll を外しました:", os.path.basename(dll))


def voice_wavs() -> list:
    """日本語音声（sound/voice/）の wav の名前。"""
    voice = os.path.join(ROOT, "sound", "voice")
    return sorted(fn for fn in os.listdir(voice) if fn.lower().endswith(".wav")) if os.path.isdir(voice) else []


def make_zip(folder: str, lang: str, suffix: str = "") -> str:
    """公開用の zip。en は folder の中身（日本語音声なし）、ja はそれに sound/voice/ の wav と説明、
    sound/pilot/ の PUBLIC_PILOTS の名前.wav を足したもの。suffix は名前に付ける（"_firebase"）。"""
    path = os.path.join(DIST, f"{NAME}_{lang}{suffix}_{VERSION}.zip")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for top, _, files in os.walk(folder):
            for fn in files:
                full = os.path.join(top, fn)
                z.write(full, os.path.join(NAME, os.path.relpath(full, folder)))
        if lang == "ja":
            for fn in voice_wavs():
                z.write(os.path.join(ROOT, "sound", "voice", fn), os.path.join(NAME, "sound", "voice", fn))
            z.writestr(os.path.join(NAME, "sound", "voice", "README.txt"),
                       VOICE_README.replace("\n", "\r\n").encode("utf-8-sig"))
            for name in PUBLIC_PILOTS:
                src = os.path.join(ROOT, "sound", "pilot", f"{name}.wav")
                if not os.path.isfile(src):
                    sys.exit(f"日本語版に入れるパイロットの wav がありません: {src}")
                z.write(src, os.path.join(NAME, "sound", "pilot", f"{name}.wav"))
    return path


def publish_folder(stage: str, dest: str, keep=()) -> bool:
    """build/ で作ったフォルダを dest にコピーする。使用中で消せなければ、そのことを知らせるだけ。
    エクスプローラーやエディタがフォルダを開いているとフォルダ自体は消せないので、フォルダは残して中身だけ入れ替える。
    keep（dest からの相対パス。ファイルかフォルダ）は消さずに残す（stage に同じものがあれば dest の方を残す）。
    exe が起動していたら、何も消さずにやめる（途中まで消して config.yaml などが無くなるのを防ぐ）。"""
    exe = os.path.join(dest, f"{NAME}.exe")
    if os.path.exists(exe):
        try:
            open(exe, "r+b").close()        # 起動中の exe は書き込みで開けない
        except OSError:
            print(f"※ {exe} が起動中のため更新できませんでした（何も変えていません）。exe を終了してから作り直してください。")
            return False
    try:
        for rel in keep:
            src = os.path.join(dest, rel)
            if os.path.isdir(src):
                shutil.copytree(src, os.path.join(stage, rel), dirs_exist_ok=True)
            elif os.path.isfile(src):
                os.makedirs(os.path.dirname(os.path.join(stage, rel)), exist_ok=True)
                shutil.copy2(src, os.path.join(stage, rel))
        if os.path.exists(dest):
            for name in os.listdir(dest):
                path = os.path.join(dest, name)
                if os.path.isdir(path):
                    shutil.rmtree(path)
                else:
                    os.remove(path)
        shutil.copytree(stage, dest, dirs_exist_ok=True)
        return True
    except OSError as e:
        print(f"※ {dest} が使用中のため更新できませんでした。exe を終了し、開いているファイルやエディタを閉じて"
              f"作り直してください: {e}")
        return False


def exe_modules(exe: str) -> list:
    """exe に入っている Python のモジュール名（Firebase なしの exe に Firebase が入っていないことの確認用）。"""
    import tempfile
    from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader
    arc = CArchiveReader(exe)
    names = []
    for entry in arc.toc:
        if entry.lower().endswith(".pyz"):
            with tempfile.TemporaryDirectory() as d:
                path = os.path.join(d, "a.pyz")
                with open(path, "wb") as f:
                    f.write(arc.extract(entry))
                names += list(ZlibArchiveReader(path).toc)
    return names


def build(dist: str, public: bool, firebase: bool = True) -> str:
    """exe を dist/NAME/ に作り、隣に置くものをコピーする。できたフォルダを返す。
    firebase=False は公開用の Firebase なし（firebase-admin を入れず、FIREBASE = False を書く）。"""
    out = os.path.join(dist, NAME)
    # バージョン（作った日）を exe に入れる。ソースから動かすときに古い日付が出ないよう、作り終えたら消す
    with open(BUILD_VERSION_PY, "w", encoding="utf-8") as f:
        f.write(f'# tools/build_exe.py が exe を作るときだけ書く（Git の管理外）\nVERSION = "{VERSION}"\n'
                f"FIREBASE = {firebase}\n")
    try:
        PyInstaller.__main__.run([
            os.path.join(ROOT, "main.py"),
            "--name", NAME,
            "--onedir",
            "--windowed",                   # コンソールは出さない（--debug で出す）
            "--noconfirm",
            "--clean",
            "--distpath", dist,
            "--workpath", os.path.join(ROOT, "build"),
            "--specpath", os.path.join(ROOT, "build"),
            # 実行時に名前で読み込まれるもの
            "--collect-submodules", "cv2_enumerate_cameras",
            "--collect-binaries", "cv2_enumerate_cameras",
            "--hidden-import", "comtypes.client",
            "--hidden-import", "comtypes.stream",
            "--hidden-import", "app._build_version",
            # FGM（fpv-gate-marker）を pip install -e で入れているときにも見つかるよう、場所を教える
            "--paths", FGM_DIR,
            "--hidden-import", "fgm",
        ] + ([] if firebase else ["--exclude-module", "firebase_admin", "--exclude-module", "app.firebase_sync"]))
    finally:
        os.remove(BUILD_VERSION_PY)
    print("バージョン:", VERSION, "" if firebase else "（Firebase なし）")
    if not firebase:
        found = [m for m in exe_modules(os.path.join(out, f"{NAME}.exe"))
                 if m.split(".")[0] in ("firebase_admin", "google") or m == "app.firebase_sync"]
        if found:
            sys.exit(f"Firebase なしの exe に Firebase のコードが入っています: {found[:5]}")

    # exe の隣に置くもの（鍵はコピーしない）
    # パイロットの名前.wav（sound/pilot。人の名前）は公開用のフォルダには入れず、日本語版の zip に PUBLIC_PILOTS だけ足す。
    # 日本語音声（sound/voice）も日本語版の zip で足す
    # sample_*.wav は効果音を作るときの参考に置いた音（出どころの分からない音）なので入れない
    shutil.copytree(os.path.join(ROOT, "sound"), os.path.join(out, "sound"), dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("pilot", "voice", "sample_*") if public
                    else shutil.ignore_patterns("sample_*"))
    names = ["README.md", "README_jp.md", "LICENSE"]
    # PC ごとの設定（URL・USB のパスなど）は公開用には入れない。Firebase の説明とひな形は Firebase ありだけ
    if not public:
        names += ["config.yaml", "firebase.yaml"]
    if firebase:
        names += ["firebase.example.yaml"]
    for name in names:
        if os.path.exists(os.path.join(ROOT, name)):
            shutil.copy2(os.path.join(ROOT, name), out)
    # README の画像（docs/images）。README から相対パスで見ているので、同じ場所に置く
    shutil.copytree(os.path.join(ROOT, "docs", "images"), os.path.join(out, "docs", "images"), dirs_exist_ok=True)
    if firebase:
        shutil.copy2(os.path.join(ROOT, "docs", "firebase.md"), os.path.join(out, "docs"))
    with open(os.path.join(out, "debug.bat"), "w", encoding="cp932", newline="") as f:
        f.write(DEBUG_BAT)

    if public:
        write_licenses(os.path.join(out, "THIRD_PARTY_LICENSES"), firebase)
        remove_unused_ffmpeg(os.path.join(out, "_internal"))

    if public and os.path.exists(os.path.join(out, LAPS_DIR)):
        sys.exit(f"配ってはいけないフォルダが出力先にあります。削除してください: {os.path.join(out, LAPS_DIR)}")
    for top, _, files in os.walk(out):
        for fn in files:
            low = fn.lower()
            if low.startswith("serviceaccount") or "firebase-adminsdk" in low or \
                    (public and low in ("config.yaml", "firebase.yaml") and top == out) or low.endswith(".log"):
                sys.exit(f"配ってはいけないファイルが出力先にあります。削除してください: {os.path.join(top, fn)}")
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--public", action="store_true",
                   help="公開用（PC ごとの設定を入れず、ライセンス文を入れて、英語版・日本語版・日本語版 Firebase ありの zip を作る）")
    args = p.parse_args()
    if args.public:
        out = build(PUBLIC_STAGE, True, firebase=False)
        print("\n英語版の zip:", make_zip(out, "en"))
        if voice_wavs():
            print("日本語版の zip:", make_zip(out, "ja"))
        else:
            print("※ sound/voice に wav が無いため、日本語版の zip は作りませんでした")
        dest = os.path.join(PUBLIC_DIST, NAME)
        if publish_folder(out, dest):
            print("公開用フォルダ:", dest)
        fb_out = build(PUBLIC_STAGE_FB, True, firebase=True)
        if voice_wavs():
            print("\n日本語版（Firebase あり）の zip:", make_zip(fb_out, "ja", "_firebase"))
        else:
            print("※ sound/voice に wav が無いため、日本語版（Firebase あり）の zip は作りませんでした")
    else:
        out = build(STAGE, False)
        dest = os.path.join(DIST, NAME)
        if publish_folder(out, dest, KEEP):
            print(f"\n完成: {os.path.join(dest, NAME)}.exe")
            print("使う PC では exe の隣に serviceAccount.json を置いてください。")


if __name__ == "__main__":
    main()
