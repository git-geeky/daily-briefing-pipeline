from briefing_shared_loader import handle_version_flag, load_shared_module, reexport


if __name__ == "__main__" and handle_version_flag("validate_briefing_core.py"):
    raise SystemExit(0)

_shared = load_shared_module("validate_briefing_core.py", __file__)
reexport(_shared, globals())


if __name__ == "__main__":
    raise SystemExit(main())
