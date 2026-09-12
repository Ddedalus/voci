import sys

sys.path.insert(0, "/tmp/fa")
import adapter  # noqa: E402

adapter.install()

from app.main import app  # noqa: E402

table = adapter.extract_routes(app)
for e in table:
    print(f"{e.kind:16} {str(e.methods):24} {e.path_format:40} -> {e.endpoint_qualname} @ {e.endpoint_file}:{e.endpoint_firstlineno} (name={e.name})")
