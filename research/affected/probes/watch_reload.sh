#!/usr/bin/env bash
# Shows today's --watch bug: two in-process cli.main calls, with a first-party edit between them.
# The second call still runs the old myapp.f (the test asserting == 1 passes after f returns 2),
# because collect._import_module evicts only test modules from sys.modules.
set -euo pipefail
dir=$(mktemp -d)
mkdir -p "$dir/tests"
cd "$dir"
printf '[project]\nname="p"\nversion="0"\n[tool.voci]\n' > pyproject.toml
echo 'def f(): return 1' > myapp.py
printf 'import myapp\ndef test_f():\n    assert myapp.f() == 1\n' > tests/test_f.py
"${VOCI_PYTHON:-/home/hubert/voci/.venv/bin/python}" -c "
import time
from voci import cli
print('first', cli.main(['tests']))
time.sleep(0.05)
open('myapp.py', 'w').write('def f(): return 2\n')
print('second (should fail, passes)', cli.main(['tests']))
"
