"""設定画面（Sキー）。「General」「QR & Announcements」「Course」の3タブ。画面の文字は英語。

Firebase は隠し機能なので、firebase.yaml があるときだけルート・鍵の項目を出す。"""
from __future__ import annotations

from .config import DEFAULT_TRACK_SETTING, validate_root

# コース設定の項目: (キー, 表示名, 型)
TRACK_FIELDS = [
    ("minLap", "minLap (minimum lap, sec)", float),
    ("betterLap", "betterLap (multiplier)", float),
    ("lapMeterMax", "lapMeterMax", float),
    ("lapMeterPass1", "lapMeterPass1", float),
    ("lapMeterPass2", "lapMeterPass2", float),
    ("sessionLaps1", "sessionLaps1", int),
    ("sessionTime1", "sessionTime1 (sec)", float),
    ("sessionTime2", "sessionTime2 (sec)", float),
]
# !trackSetting の項目だが、アナウンスの設定なので「QR & Announcements」タブに出す
TOTAL_ANNOUNCE = "totalLapAnnounce"

# アナウンスの言語ごとに持つ項目
SPEECH_TEXT_KEYS = ("text", "start_text", "lap_text", "total_text", "clear_text", "best_text")
SPEECH_LANG_KEYS = SPEECH_TEXT_KEYS + ("voice",)

# 5.8GHz の一般的なバンド/ch 名（リストに無い名前も直接入力できる）
BAND_CHOICES = [f"{b}{n}" for b in "RFEAB" for n in range(1, 9)] + [f"L{n}" for n in range(1, 9)]


def ask_band(ch: int, current: str, device_name: str = ""):
    """1つの受信器のバンドを選ぶ小さな画面。OKなら新しいバンド名、キャンセルなら None。"""
    import tkinter as tk
    from tkinter import ttk

    result = {"value": None}
    win = tk.Tk()
    win.title(f"CH{ch + 1} band")
    win.attributes("-topmost", True)
    frm = ttk.Frame(win, padding=12)
    frm.pack(fill="both", expand=True)
    ttk.Label(frm, text=f"CH{ch + 1}  {device_name}").grid(row=0, column=0, columnspan=2, sticky="w")
    ttk.Label(frm, text="Band").grid(row=1, column=0, sticky="w", pady=8)
    var = tk.StringVar(value=current)
    box = ttk.Combobox(frm, textvariable=var, values=BAND_CHOICES, width=10)
    box.grid(row=1, column=1, sticky="w", pady=8)
    box.focus_set()
    box.select_range(0, "end")

    def ok(_=None):
        v = var.get().strip().upper()
        if v:
            result["value"] = v
        win.destroy()

    btns = ttk.Frame(frm)
    btns.grid(row=2, column=0, columnspan=2, sticky="e")
    ttk.Button(btns, text="Cancel", command=win.destroy).pack(side="right", padx=4)
    ttk.Button(btns, text="OK", command=ok).pack(side="right", padx=4)
    win.bind("<Return>", ok)
    win.bind("<Escape>", lambda _: win.destroy())
    win.mainloop()
    return result["value"]


def _num(s: str, typ):
    v = float(s)
    if typ is int or (typ is float and v.is_integer()):
        return int(v)
    return v


def open_settings(config, track_setting: dict, n_channels: int = 4, preview_announce=None,
                  voices: list[str] | None = None):
    """設定画面を開く。保存されたら {'general': {...}, 'qr_speech': {...}, 'track': {変更した項目}} を返す。
    キャンセルなら None。preview_announce(band, name, key, speech設定, lap, lap_count, total, best) は「試す」ボタンで呼ぶ。"""
    import tkinter as tk
    from tkinter import messagebox, ttk

    result = {"value": None}
    root_win = tk.Tk()
    from .version import VERSION
    root_win.title(f"d-practice-cam {VERSION} Settings")
    root_win.attributes("-topmost", True)
    nb = ttk.Notebook(root_win)
    nb.pack(fill="both", expand=True, padx=8, pady=8)

    # ---------- 一般 ----------
    g = ttk.Frame(nb, padding=10)
    nb.add(g, text="General")
    fb = config.firebase
    ar = config.aruco
    lp = config.data.get("laprf") or {}
    # Firebase は隠し機能。firebase.yaml があるときだけルート・鍵の項目を出す
    # （データベースURLは firebase.yaml に直接書く。設定画面では扱わない）
    has_fb = config.has_firebase_file()
    general_fields = [("root", "Root name (Firebase)", fb.get("root", "practice"))] if has_fb else []
    for i in range(n_channels):
        general_fields.append((f"band{i}", f"CH{i + 1} band", config.band_for(i)))
    general_fields += [
        ("theme", "Color theme (T key)", (config.data.get("display") or {}).get("theme", "dark")),
        ("device_names", "Receiver device names (comma separated)", ", ".join(config.receiver.get("device_names", []))),
        ("marker_type", "Marker type", ar.get("marker_type", "fgm")),
        ("min_size_pct", "Minimum marker size (%)", ar.get("min_size_pct", 5)),
        ("min_markers", "Markers required at once (ArUco)", ar.get("min_markers", 1)),
        ("fgm_line_step", "FGM scan line interval (px)", ar.get("fgm_line_step", 4)),
        ("fgm_min_lines", "FGM lines required for a marker", ar.get("fgm_min_lines", 3)),
        ("fgm_tol", "FGM stripe tolerance (modules; smaller = fewer false detections)", ar.get("fgm_tol", 0.25)),
        ("appear_ms", "Wait after marker appears (ms; 0 = none)", ar.get("appear_ms", 30)),
        ("flicker_ms", "Wait after marker is lost (ms)", ar.get("flicker_ms", 200)),
        ("pilot_timeout_sec", "Seconds until the frequency is released (clear; 0 = never)",
         config.qr.get("pilot_timeout_sec", 60)),
        ("no_video_clear_ms", "Clear when video is lost for (ms; 0 = never)",
         (config.data.get("no_video") or {}).get("clear_ms", 3000)),
        ("laprf_enabled", "Send passes with the LapRF protocol (for LiveTime)", lp.get("enabled", False)),
    ]
    if has_fb:
        general_fields.append(("credentials", "Firebase key file path", fb.get("credentials", "")))
    gvars = {}
    for r, (key, label, value) in enumerate(general_fields):
        ttk.Label(g, text=label).grid(row=r, column=0, sticky="w", pady=2)
        v = tk.StringVar(value=str(value))
        if key == "laprf_enabled":
            v = tk.BooleanVar(value=bool(value))
            ttk.Checkbutton(g, variable=v).grid(row=r, column=1, sticky="w", pady=2)
        elif key in ("marker_type", "theme"):
            choices = (("FpvGateMarker (FGM)", "fgm"), ("ArUco", "aruco")) if key == "marker_type" \
                else (("Dark", "dark"), ("Light (for sunlight)", "light"))
            mf = ttk.Frame(g)
            mf.grid(row=r, column=1, sticky="w", pady=2)
            for text, val in choices:
                ttk.Radiobutton(mf, text=text, variable=v, value=val).pack(side="left", padx=(0, 12))
        else:
            ttk.Entry(g, textvariable=v, width=48).grid(row=r, column=1, sticky="we", pady=2)
        gvars[key] = v
    if has_fb:
        ttk.Label(g, text="* Key file changes take effect after a restart", foreground="#666").grid(
            row=len(general_fields), column=0, columnspan=2, sticky="w", pady=(8, 0))

    # ---------- QR・アナウンス ----------
    from .sound import DEFAULT_TEXT, DEFAULT_TEXT_EN
    q = ttk.Frame(nb, padding=10)
    nb.add(q, text="QR & Announcements")
    qr, sp = config.qr, config.data.get("speech", {})
    # 文と声は言語ごとに持ち、画面には選んでいる言語の分を出す
    sp_en = sp.get("en") or {}
    lang_values = {
        "ja": {k: str(sp.get(k, DEFAULT_TEXT.get(k, "")) or "") for k in SPEECH_LANG_KEYS},
        "en": {k: str(sp_en.get(k, DEFAULT_TEXT_EN.get(k, "")) or "") for k in SPEECH_LANG_KEYS},
    }
    shown_lang = {"value": "en" if sp.get("language") == "en" else "ja"}
    qvars = {
        "qr_enabled": tk.BooleanVar(value=bool(qr.get("enabled", True))),
        "interval_ms": tk.StringVar(value=str(qr.get("interval_ms", 500))),
        "speech_enabled": tk.BooleanVar(value=bool(sp.get("enabled", True))),
        "language": tk.StringVar(value=shown_lang["value"]),
        "sample_lap": tk.StringVar(value="12.34"),
        "sample": tk.StringVar(value="CHO!"),
    }
    for k in SPEECH_LANG_KEYS:
        qvars[k] = tk.StringVar(value=lang_values[shown_lang["value"]][k])

    def keep_shown_lang():
        """画面の文と声を、いま出している言語の値として控える。"""
        lang_values[shown_lang["value"]] = {k: qvars[k].get().strip() for k in SPEECH_LANG_KEYS}

    def on_language():
        keep_shown_lang()
        shown_lang["value"] = qvars["language"].get()
        for k in SPEECH_LANG_KEYS:
            qvars[k].set(lang_values[shown_lang["value"]][k])

    def speech_values() -> dict:
        """画面の値を config.yaml の speech の形にする。上の階層が日本語、en の下が英語。"""
        keep_shown_lang()
        return {**lang_values["ja"], "language": qvars["language"].get(), "en": dict(lang_values["en"])}

    r = 0
    ttk.Label(q, text="Read pilot names from QR codes").grid(row=r, column=0, sticky="w", pady=2)
    ttk.Checkbutton(q, variable=qvars["qr_enabled"]).grid(row=r, column=1, sticky="w", pady=2)
    r += 1
    ttk.Label(q, text="QR scan interval (ms)").grid(row=r, column=0, sticky="w", pady=2)
    ttk.Entry(q, textvariable=qvars["interval_ms"], width=10).grid(row=r, column=1, sticky="w", pady=2)
    r += 1
    ttk.Separator(q).grid(row=r, column=0, columnspan=2, sticky="we", pady=8)
    r += 1
    ttk.Label(q, text="Announce pilot names").grid(row=r, column=0, sticky="w", pady=2)
    ttk.Checkbutton(q, variable=qvars["speech_enabled"]).grid(row=r, column=1, sticky="w", pady=2)
    r += 1
    total_cur = bool(track_setting.get(TOTAL_ANNOUNCE, DEFAULT_TRACK_SETTING[TOTAL_ANNOUNCE]))
    total_var = tk.BooleanVar(value=total_cur)
    ttk.Label(q, text="Announce lap count and total (totalLapAnnounce)").grid(row=r, column=0, sticky="w", pady=2)
    ttk.Checkbutton(q, variable=total_var).grid(row=r, column=1, sticky="w", pady=2)
    r += 1
    ttk.Label(q, text="Announcement language").grid(row=r, column=0, sticky="w", pady=2)
    lf = ttk.Frame(q)
    lf.grid(row=r, column=1, sticky="w", pady=2)
    for text, val in (("Japanese", "ja"), ("English", "en")):
        ttk.Radiobutton(lf, text=text, variable=qvars["language"], value=val, command=on_language) \
            .pack(side="left", padx=(0, 12))
    r += 1
    ttk.Label(q, text="Speech synthesis reads the text below (English always; Japanese when a WAV is missing)",
              foreground="#666").grid(row=r, column=0, columnspan=2, sticky="w")
    r += 1
    ttk.Label(q, text="QR read ({name} pilot, {band} band)").grid(row=r, column=0, sticky="w", pady=2)
    ttk.Entry(q, textvariable=qvars["text"], width=36).grid(row=r, column=1, sticky="we", pady=2)
    r += 1
    ttk.Label(q, text="Start pass (after the lap sound)").grid(row=r, column=0, sticky="w", pady=2)
    ttk.Entry(q, textvariable=qvars["start_text"], width=36).grid(row=r, column=1, sticky="we", pady=2)
    r += 1
    ttk.Label(q, text="Lap (after the lap sound; {time} lap time)").grid(row=r, column=0, sticky="w", pady=2)
    ttk.Entry(q, textvariable=qvars["lap_text"], width=36).grid(row=r, column=1, sticky="we", pady=2)
    r += 1
    ttk.Label(q, text="Lap count and total ({lap} laps, {total} sec)").grid(row=r, column=0, sticky="w", pady=2)
    ttk.Entry(q, textvariable=qvars["total_text"], width=36).grid(row=r, column=1, sticky="we", pady=2)
    r += 1
    ttk.Label(q, text="New best lap (at the end of the lap)").grid(row=r, column=0, sticky="w", pady=2)
    ttk.Entry(q, textvariable=qvars["best_text"], width=36).grid(row=r, column=1, sticky="we", pady=2)
    r += 1
    ttk.Label(q, text="Clear (frequency released)").grid(row=r, column=0, sticky="w", pady=2)
    ttk.Entry(q, textvariable=qvars["clear_text"], width=36).grid(row=r, column=1, sticky="we", pady=2)
    r += 1
    ttk.Label(q, text="Speech voice (empty = a voice for the language)").grid(row=r, column=0, sticky="w", pady=2)
    ttk.Combobox(q, textvariable=qvars["voice"], values=[""] + list(voices or []), width=34) \
        .grid(row=r, column=1, sticky="we", pady=2)
    r += 1

    def preview(key, best=False):
        cfg = speech_values()
        cfg.update(pilot_dir=sp.get("pilot_dir", ""))   # 画面に無いものは config.yaml の値
        try:
            lap = float(qvars["sample_lap"].get())
        except ValueError:
            lap = 12.34
        if preview_announce:    # ラップは「3周、トータル35秒」まで試す（best なら「ベストラップ更新」も）
            preview_announce("F1", qvars["sample"].get().strip() or "CHO!", key, cfg, lap, 3, 35.0, best)

    ttk.Label(q, text="Test name and time (sec)").grid(row=r, column=0, sticky="w", pady=2)
    pf = ttk.Frame(q)
    pf.grid(row=r, column=1, sticky="w", pady=2)
    ttk.Entry(pf, textvariable=qvars["sample"], width=16).pack(side="left")
    ttk.Entry(pf, textvariable=qvars["sample_lap"], width=8).pack(side="left", padx=(6, 0))
    r += 1
    bf = ttk.Frame(q)
    bf.grid(row=r, column=1, sticky="w", pady=2)
    for label, key, best in (("Test QR", "text", False), ("Test start", "start_text", False),
                             ("Test lap", "lap_text", False), ("Test best", "lap_text", True),
                             ("Test clear", "clear_text", False)):
        ttk.Button(bf, text=label, command=lambda k=key, b=best: preview(k, b),
                   state="normal" if preview_announce else "disabled").pack(side="left", padx=(0, 6))
    r += 1
    ttk.Label(q, text="* After a QR read (drone restarted), the next pass on that channel is the start. "
                      "A lap sound stops any announcement",
              foreground="#666").grid(row=r, column=0, columnspan=2, sticky="w", pady=(8, 0))

    # ---------- コース設定 ----------
    t = ttk.Frame(nb, padding=10)
    nb.add(t, text="Course")
    tvars = {}
    for r, (key, label, typ) in enumerate(TRACK_FIELDS):
        ttk.Label(t, text=label).grid(row=r, column=0, sticky="w", pady=2)
        cur = track_setting.get(key, DEFAULT_TRACK_SETTING[key])
        if typ is bool:
            v = tk.BooleanVar(value=bool(cur))
            ttk.Checkbutton(t, variable=v).grid(row=r, column=1, sticky="w", pady=2)
        else:
            v = tk.StringVar(value=str(cur))
            ttk.Entry(t, textvariable=v, width=16).grid(row=r, column=1, sticky="w", pady=2)
        tvars[key] = (v, typ, cur)
    ttk.Label(t, text="Saving writes to config.yaml and today's !trackSetting (Firebase)"
              if has_fb else "Saving writes to config.yaml", foreground="#666").grid(row=len(TRACK_FIELDS), column=0, columnspan=2, sticky="w", pady=(8, 0))

    # ---------- 保存 / キャンセル ----------
    def on_save():
        try:
            root = gvars["root"].get().strip() if has_fb else fb.get("root", "practice")
            err = validate_root(root) if has_fb else None
            if err:
                raise ValueError(err)
            general = {
                "root": root,
                "bands": [gvars[f"band{i}"].get().strip() or f"CH{i + 1}" for i in range(n_channels)],
                "device_names": [s.strip() for s in gvars["device_names"].get().split(",") if s.strip()],
                "marker_type": gvars["marker_type"].get().strip().lower(),
                "min_size_pct": _num(gvars["min_size_pct"].get(), float),
                "min_markers": int(_num(gvars["min_markers"].get(), int)),
                "fgm_line_step": int(_num(gvars["fgm_line_step"].get(), int)),
                "fgm_min_lines": int(_num(gvars["fgm_min_lines"].get(), int)),
                "fgm_tol": _num(gvars["fgm_tol"].get(), float),
                "appear_ms": _num(gvars["appear_ms"].get(), float),
                "flicker_ms": _num(gvars["flicker_ms"].get(), float),
                "credentials": gvars["credentials"].get().strip() if has_fb else fb.get("credentials", ""),
                "no_video_clear_ms": _num(gvars["no_video_clear_ms"].get(), float),
                "laprf_enabled": bool(gvars["laprf_enabled"].get()),
                "theme": "light" if gvars["theme"].get() == "light" else "dark",
            }
            if not general["device_names"]:
                raise ValueError("Enter at least one receiver device name")
            if general["marker_type"] not in ("aruco", "fgm"):
                raise ValueError("Marker type must be aruco or fgm")
            if general["fgm_line_step"] < 1 or general["fgm_min_lines"] < 1:
                raise ValueError("FGM line interval and line count must be 1 or more")
            if general["fgm_tol"] < 0:
                raise ValueError("FGM stripe tolerance must be 0 or more")
            if general["appear_ms"] < 0:
                raise ValueError("Wait after marker appears must be 0 or more")
            if general["no_video_clear_ms"] < 0:
                raise ValueError("Clear when video is lost must be 0 or more")
            qr_speech = {
                "qr": {
                    "enabled": bool(qvars["qr_enabled"].get()),
                    "interval_ms": int(_num(qvars["interval_ms"].get(), int)),
                    "pilot_timeout_sec": _num(gvars["pilot_timeout_sec"].get(), float),    # 「General」タブにある
                },
                "speech": {"enabled": bool(qvars["speech_enabled"].get()), **speech_values()},
            }
            if qr_speech["qr"]["interval_ms"] < 50:
                raise ValueError("QR scan interval must be 50 ms or more")
            if qr_speech["qr"]["pilot_timeout_sec"] < 0:
                raise ValueError("Seconds until the frequency is released must be 0 or more")
            for values in lang_values.values():
                for key in SPEECH_TEXT_KEYS:
                    if not values[key]:
                        raise ValueError("An announcement text is empty")
                    try:
                        values[key].format(name="x", band="x", time="x", lap="x", total="x")
                    except (KeyError, IndexError, ValueError):
                        raise ValueError("Announcement texts can only use {name} {band} {time} {lap} {total}")
            changes = {}
            for key, (v, typ, cur) in tvars.items():
                new = bool(v.get()) if typ is bool else _num(v.get(), typ)
                if new != cur:
                    changes[key] = new
            if bool(total_var.get()) != total_cur:
                changes[TOTAL_ANNOUNCE] = bool(total_var.get())
        except ValueError as e:
            messagebox.showerror("Input error", f"Please check the values.\n{e}", parent=root_win)
            return
        result["value"] = {"general": general, "qr_speech": qr_speech, "track": changes}
        root_win.destroy()

    btns = ttk.Frame(root_win, padding=(8, 0, 8, 8))
    btns.pack(fill="x")
    ttk.Button(btns, text="Cancel", command=root_win.destroy).pack(side="right", padx=4)
    ttk.Button(btns, text="Save", command=on_save).pack(side="right", padx=4)
    root_win.protocol("WM_DELETE_WINDOW", root_win.destroy)
    root_win.mainloop()
    return result["value"]


def apply_general(config, general: dict) -> dict:
    """設定画面の「General」を config に反映する。変わった区分を返す。"""
    changed = {"root": False, "receivers": False, "aruco": False, "restart": False, "laprf": False}
    fb, ar, rx = config.firebase, config.aruco, config.receiver
    lp = config.data.setdefault("laprf", {})
    if lp.get("enabled") != general["laprf_enabled"]:
        lp["enabled"] = general["laprf_enabled"]
        changed["laprf"] = True
    if "theme" in general:          # 配色は描くときに読むので、開き直しなどは要らない
        config.data.setdefault("display", {})["theme"] = general["theme"]
    nv = config.data.setdefault("no_video", {})
    if nv.get("clear_ms") != general["no_video_clear_ms"]:
        nv["clear_ms"] = general["no_video_clear_ms"]
        changed["aruco"] = True     # 受信器を開き直すと反映する
    if fb.get("root") != general["root"]:
        fb["root"] = general["root"]
        changed["root"] = True
    if fb.get("credentials") != general["credentials"]:
        fb["credentials"] = general["credentials"]
        changed["restart"] = True
    bands = general["bands"]
    old_bands = [config.band_for(i) for i in range(len(bands))]
    if bands != old_bands or rx.get("device_names") != general["device_names"]:
        config.data["channels"] = [{"band": b} for b in bands]
        rx["device_names"] = general["device_names"]
        changed["receivers"] = True
    for k in ("marker_type", "min_size_pct", "min_markers", "fgm_line_step", "fgm_min_lines",
              "fgm_tol", "appear_ms", "flicker_ms"):
        if ar.get(k) != general[k]:
            ar[k] = general[k]
            changed["aruco"] = True
    return changed


def apply_qr_speech(config, qr_speech: dict) -> bool:
    """設定画面の「QR & Announcements」を config に反映する。QR の設定が変わったら True（受信器を開き直す）。"""
    qr_changed = config.qr != {**config.qr, **qr_speech["qr"]}
    config.qr.update(qr_speech["qr"])
    config.data.setdefault("speech", {}).update(qr_speech["speech"])
    return qr_changed
