import re

# ---------- gui/workers.py ----------
path = "gui/workers.py"
with open(path, "r", encoding="utf-8") as f:
    content = f.read()

if "_NOWIN" not in content:
    content = content.replace(
        "sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))",
        "sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\n\n"
        "_NOWIN = subprocess.CREATE_NO_WINDOW if sys.platform == \"win32\" else 0",
        1,
    )

content, n1 = re.subn(
    r"subprocess\.Popen\(\s*\n(\s*)cmd, stdout=subprocess\.PIPE, stderr=subprocess\.STDOUT,\s*\n(\s*)text=True, bufsize=1, universal_newlines=True\s*\n(\s*)\)",
    lambda m: (
        f"subprocess.Popen(\n{m.group(1)}cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,\n"
        f"{m.group(2)}text=True, bufsize=1, universal_newlines=True,\n"
        f"{m.group(2)}creationflags=_NOWIN\n{m.group(3)})"
    ),
    content,
)

content, n2 = re.subn(
    r"out = subprocess\.run\(cmd, stdout=subprocess\.PIPE, stderr=subprocess\.PIPE, text=True\)",
    "out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=_NOWIN)",
    content,
)

with open(path, "w", encoding="utf-8") as f:
    f.write(content)

print(f"workers.py: Popen заменено {n1} раз, run заменено {n2} раз")

# ---------- make_subs.py ----------
path2 = "make_subs.py"
with open(path2, "r", encoding="utf-8") as f:
    content2 = f.read()

if "import sys" not in content2:
    content2 = content2.replace("import subprocess", "import subprocess\nimport sys", 1)

if "_NOWIN" not in content2:
    content2 = re.sub(
        r"(import subprocess\n(?:import sys\n)?)",
        r"\1\n_NOWIN = subprocess.CREATE_NO_WINDOW if sys.platform == \"win32\" else 0\n",
        content2,
        count=1,
    )

content2, n3 = re.subn(
    r"result = subprocess\.run\(cmd, stdout=subprocess\.PIPE, stderr=subprocess\.PIPE, text=True\)",
    "result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=_NOWIN)",
    content2,
)

with open(path2, "w", encoding="utf-8") as f:
    f.write(content2)

print(f"make_subs.py: run заменено {n3} раз")

if n1 != 2 or n2 != 1 or n3 != 1:
    print("!!! ВНИМАНИЕ: не все вхождения найдены — проверь вручную grep-ом")
