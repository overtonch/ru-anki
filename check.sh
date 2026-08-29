#!/bin/sh
# Pre-deploy gate: fast, offline, no LLM. Run before restarting the server or
# pushing. Exits non-zero on the first failure.
#
#   ./check.sh
#
set -e
cd "$(dirname "$0")"
PY=./.venv/bin/python
PYTEST=./.venv/bin/pytest

echo "· python syntax"
$PY - <<'EOF'
import py_compile, pathlib, sys
bad = 0
for p in list(pathlib.Path("app").glob("*.py")) + [pathlib.Path("db.py"), pathlib.Path("subs.py")]:
    try:
        py_compile.compile(str(p), doraise=True)
    except py_compile.PyCompileError as e:
        print(e); bad = 1
sys.exit(bad)
EOF

echo "· javascript syntax"
if command -v node >/dev/null 2>&1; then
  node --check app/static/sw.js
  $PY - <<'EOF'
import re, subprocess, sys, tempfile, os
html = open("app/static/index.html", encoding="utf-8").read()
scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
if not scripts:
    sys.exit(0)
big = max(scripts, key=len)
f = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False)
f.write(big); f.close()
r = subprocess.run(["node", "--check", f.name])
os.unlink(f.name)
sys.exit(r.returncode)
EOF
else
  echo "  (node not found — skipping JS check)"
fi

echo "· tests"
$PYTEST

echo
echo "✓ all checks passed"
