#!/usr/bin/env python3
"""
tools/audio_capture/recorder_gui.py  (бывший recorder.py)

GUI для РУЧНОГО захвата системного звука (то, что играет в браузере/системе)
— например, предпросмотр голоса в HeyGen без оплаты полного рендера
аватар-видео (предпросмотр у них бесплатный и без лимита, в отличие от
рендера). Вся логика захвата и обрезки тишины — в capture_core.py, здесь
только интерфейс для человека: кнопки «начать/остановить», таймер, выбор
источника звука.

Для автоматизации через Playwright/Camoufox (когда до этого дойдёт) GUI не
нужен — используйте capture_core.RecordingSession напрямую, см. докстринг
класса там.

Зависимости (Lubuntu / PipeWire с pipewire-pulse или классический PulseAudio):
    sudo apt install pulseaudio-utils ffmpeg python3-tk
    pip install pydub    (уже в requirements-cpu.txt)

Запуск:
    python3 tools/audio_capture/recorder_gui.py
"""
import os
import sys
import threading
import time
from datetime import datetime

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    from pydub import AudioSegment
except ImportError:
    print("Не найден pydub. Установите: pip install pydub")
    sys.exit(1)

from tools.audio_capture.capture_core import (
    ALL_SYSTEM_AUDIO, RecordingSession, list_active_streams, trim_silence,
)


class RecorderApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Захват системного звука")
        self.root.geometry("480x360")
        self.root.resizable(False, False)

        self.session: RecordingSession | None = None
        self.recording = False
        self.raw_path = None
        self.start_time = None

        self.init_error = None
        try:
            self.monitor_index, self.monitor_source = RecordingSession().monitor_index, RecordingSession().monitor_source
        except RuntimeError as e:
            self.monitor_index, self.monitor_source = None, None
            self.init_error = str(e)

        self.stream_map = {}

        self._build_ui()
        self._refresh_sources()

        if self.init_error:
            self.root.after(200, lambda: messagebox.showerror("Ошибка инициализации", self.init_error))

    def _build_ui(self):
        pad = {"padx": 10, "pady": 6}

        info_text = (
            f"Monitor дефолтного устройства:\n{self.monitor_source or 'не найден'}"
            f" (индекс {self.monitor_index if self.monitor_index is not None else '?'})"
        )
        ttk.Label(self.root, text=info_text, wraplength=440, justify="left").pack(**pad, anchor="w")

        src_frame = ttk.Frame(self.root)
        src_frame.pack(**pad, fill="x")

        ttk.Label(src_frame, text="Источник звука:").grid(row=0, column=0, sticky="w")
        self.source_var = tk.StringVar(value=ALL_SYSTEM_AUDIO)
        self.source_combo = ttk.Combobox(src_frame, textvariable=self.source_var, state="readonly", width=44)
        self.source_combo.grid(row=1, column=0, sticky="w")

        refresh_btn = ttk.Button(src_frame, text="⟳ Обновить", command=self._refresh_sources)
        refresh_btn.grid(row=1, column=1, padx=6)

        self.status_var = tk.StringVar(value="Готово")
        ttk.Label(self.root, textvariable=self.status_var, font=("Sans", 11, "bold")).pack(**pad)

        self.timer_var = tk.StringVar(value="00:00")
        ttk.Label(self.root, textvariable=self.timer_var, font=("Sans", 22)).pack(**pad)

        btn_frame = ttk.Frame(self.root)
        btn_frame.pack(**pad)

        self.start_btn = ttk.Button(btn_frame, text="● Начать запись", command=self.start_recording)
        self.start_btn.grid(row=0, column=0, padx=5)

        self.stop_btn = ttk.Button(btn_frame, text="■ Остановить и сохранить", command=self.stop_recording, state="disabled")
        self.stop_btn.grid(row=0, column=1, padx=5)

        thresh_frame = ttk.Frame(self.root)
        thresh_frame.pack(**pad)
        ttk.Label(thresh_frame, text="Порог тишины (дБ):").grid(row=0, column=0)
        self.thresh_var = tk.DoubleVar(value=-40.0)
        ttk.Spinbox(thresh_frame, from_=-80, to=-10, increment=1, textvariable=self.thresh_var, width=6).grid(row=0, column=1, padx=8)

        self.debug_var = tk.StringVar(value="")
        ttk.Label(self.root, textvariable=self.debug_var, foreground="#a00000", wraplength=440, justify="left").pack(**pad, anchor="w")

    def _refresh_sources(self):
        if self.recording:
            return
        streams = list_active_streams()
        self.stream_map = {label: idx for idx, label in streams}

        values = [ALL_SYSTEM_AUDIO] + [label for _, label in streams]
        current = self.source_var.get()
        self.source_combo["values"] = values
        if current not in values:
            self.source_var.set(ALL_SYSTEM_AUDIO)

        self.root.after(3000, self._refresh_sources)

    def start_recording(self):
        if not self.monitor_source:
            messagebox.showerror("Ошибка", "Источник записи не найден. Проверьте PulseAudio/PipeWire.")
            return

        self.debug_var.set("")
        selected_label = self.source_var.get()

        try:
            self.session = RecordingSession(source_label=selected_label)
            self.session.start()
        except RuntimeError as e:
            messagebox.showerror("Ошибка", str(e))
            return

        self.recording = True
        self.start_time = time.time()
        self.status_var.set(f"Идёт запись: {selected_label}")
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.source_combo.config(state="disabled")
        self._tick()

        self.root.after(1000, self._verify_source)

    def _verify_source(self):
        if not self.recording or not self.session:
            return
        ok, actual = self.session.verify_source()
        if actual is None:
            self.debug_var.set("⚠ Не удалось проверить фактический источник записи (pactl не вернул данные).")
        elif not ok:
            self.debug_var.set(
                f"⚠ ВНИМАНИЕ: запись реально идёт с источника #{actual}, "
                f"а ожидался monitor #{self.session.monitor_index}. "
                f"Похоже, пишется микрофон, а не системный звук!"
            )
        else:
            self.debug_var.set(f"✓ Подтверждено: запись идёт с источника #{actual} (monitor).")

    def _tick(self):
        if self.recording:
            elapsed = int(time.time() - self.start_time)
            self.timer_var.set(f"{elapsed // 60:02d}:{elapsed % 60:02d}")
            self.root.after(500, self._tick)

    def stop_recording(self):
        if not self.recording or not self.session:
            return
        self.recording = False
        self.status_var.set("Обработка и обрезка тишины...")
        self.stop_btn.config(state="disabled")
        self.source_combo.config(state="readonly")

        self.raw_path = self.session.stop()
        threading.Thread(target=self._process_and_save, daemon=True).start()

    def _process_and_save(self):
        try:
            sound = AudioSegment.from_wav(self.raw_path)
            trimmed = trim_silence(sound, silence_thresh_db=self.thresh_var.get())

            if len(trimmed) == 0:
                self.root.after(0, lambda: messagebox.showwarning(
                    "Пусто",
                    "После обрезки тишины файл получился пустым.\n"
                    "Возможно, звук не был захвачен, или порог тишины слишком строгий."
                ))
                self.root.after(0, self._reset_ui)
                return

            self.root.after(0, lambda: self._ask_save_path(trimmed))
        except Exception as e:
            self.root.after(0, lambda: messagebox.showerror("Ошибка обработки", str(e)))
            self.root.after(0, self._reset_ui)

    def _ask_save_path(self, trimmed):
        default_name = f"recording_{datetime.now().strftime('%Y%m%d_%H%M%S')}.wav"
        path = filedialog.asksaveasfilename(
            defaultextension=".wav",
            initialfile=default_name,
            filetypes=[("WAV audio", "*.wav"), ("MP3 audio", "*.mp3"), ("All files", "*.*")],
        )
        if path:
            fmt = "mp3" if path.lower().endswith(".mp3") else "wav"
            trimmed.export(path, format=fmt)
            messagebox.showinfo("Готово", f"Сохранено: {path}\nДлительность: {len(trimmed) / 1000:.1f} сек.")
        self._reset_ui()

    def _reset_ui(self):
        self.status_var.set("Готово")
        self.timer_var.set("00:00")
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        for p in (self.raw_path, getattr(self.session, "_log_path", None)):
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("clam")
    except Exception:
        pass
    RecorderApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
