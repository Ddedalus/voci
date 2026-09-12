"""FastAPI-shaped app: a decorator that registers routes (an effect on
`app`), and a handler whose parameter annotation is a pydantic model (the
test only ever executes the handler body; validation happens with no
first-party Python frames)."""
from .models import User


class App:
    def __init__(self):
        self.routes = {}

    def get(self, path):
        def decorator(fn):
            self.routes[path] = fn
            return fn
        return decorator

    def include_router(self, router):
        self.routes.update(router)


app = App()


@app.get("/users")
def list_users():
    return []


def handle_user(user: User):
    return user.id


def undecorated_helper():
    """Adding this should re-run nothing (case: brand-new undecorated fn)."""
    return 42
