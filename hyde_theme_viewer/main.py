import sys

from .window import HydeThemeApp


def main() -> int:
    app = HydeThemeApp()
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
