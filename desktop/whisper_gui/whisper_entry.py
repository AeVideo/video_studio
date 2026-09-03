"""
Точка входа для сборки Whisper CLI как отдельного exe через PyInstaller.
"""
import sys

# На Windows консоль по умолчанию использует cp1252, что ломается на кириллице.
# Принудительно переключаем стандартный вывод на UTF-8.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from whisper.transcribe import cli

if __name__ == "__main__":
    cli()
