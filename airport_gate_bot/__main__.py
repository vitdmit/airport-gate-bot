from .runtime_fixes import apply as _apply_runtime_fixes

_apply_runtime_fixes()

from .cli import main


if __name__ == "__main__":
    main()
