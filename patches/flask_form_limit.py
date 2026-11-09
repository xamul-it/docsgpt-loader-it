"""Loaded at interpreter start by flask_form_limit.pth, both mounted into the
DocsGPT backend's site-packages (see the compose file).

Flask caps non-file multipart form fields at 500 KB (MAX_FORM_MEMORY_SIZE) and
DocsGPT has no setting for it. When an ingest finishes, the worker posts the
source's directory listing as one form field to /api/upload_index: past ~2,000
files it exceeds the cap and the whole ingest fails with a 413 at the last step.
Lift the cap (and the 1,000-part cap) before the app object is created.
"""
try:
    import flask
    from werkzeug.datastructures import ImmutableDict

    flask.Flask.default_config = ImmutableDict(
        {
            **flask.Flask.default_config,
            "MAX_FORM_MEMORY_SIZE": None,
            "MAX_FORM_PARTS": None,
        }
    )
except Exception:  # never block interpreter start-up
    pass
