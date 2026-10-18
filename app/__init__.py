import os

from flask import Flask, jsonify, send_from_directory

from . import api, db as dbmod, worker
from .common import ApiError


def create_app(config=None):
    app = Flask(__name__, static_folder="static", static_url_path="")
    app.config.update(
        DATABASE=os.environ.get("DATABASE", "newtonite.db"),
        HASH_METHOD="pbkdf2:sha256:200000",
        WORKER_ENABLED=True,
        MAX_CONTENT_LENGTH=256 * 1024,
        JSON_SORT_KEYS=False,
    )
    if config:
        app.config.update(config)
    dbmod.init_db(app.config["DATABASE"])
    app.teardown_appcontext(dbmod.close_db)
    app.register_blueprint(api.bp)

    @app.errorhandler(ApiError)
    def _api_error(e):
        return jsonify(error={"code": e.code, "message": e.message, "details": e.details}), e.status

    @app.errorhandler(404)
    def _404(_e):
        return jsonify(error={"code": "not_found", "message": "Not found", "details": {}}), 404

    @app.errorhandler(405)
    def _405(_e):
        return jsonify(error={"code": "method_not_allowed", "message": "Method not allowed", "details": {}}), 405

    @app.errorhandler(413)
    def _413(_e):
        return jsonify(error={"code": "too_large", "message": "Request body too large", "details": {}}), 413

    @app.errorhandler(Exception)
    def _500(e):
        app.logger.exception("unhandled error")
        return jsonify(error={"code": "internal_error", "message": "Something went wrong on our side", "details": {}}), 500

    @app.get("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    if app.config["WORKER_ENABLED"]:
        app.worker_stop, app.worker_thread = worker.start_thread(app.config["DATABASE"])
    return app
