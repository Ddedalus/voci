"""Router class, re-exported by pkg/__init__.py. Also holds an unrelated
statement used to prove that a re-export hub edit unrelated to the re-exported
name doesn't ripple to consumers."""


def unrelated_helper():
    return 1


class Router:
    prefix: str = "/"

    def add(self, path):
        return path
