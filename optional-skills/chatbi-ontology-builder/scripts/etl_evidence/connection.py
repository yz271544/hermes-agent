"""Explicit, standalone MySQL catalog configuration; no project paths or defaults."""
import os


def get_connection(config):
    import pymysql
    if not isinstance(config, dict):
        raise ValueError("Provide a metadata catalog JSON configuration")
    required = ("host", "port", "database", "user", "password_env")
    if any(not config.get(key) for key in required):
        raise ValueError("Catalog config requires host/port/database/user/password_env")
    if "password" in config:
        raise ValueError("Use password_env, not an inline password")
    password = os.environ.get(config["password_env"])
    if password is None:
        raise ValueError("Configured password environment variable is absent")
    options = {}
    if config.get("tls"):
        tls = config["tls"]
        if not tls.get("ca"):
            raise ValueError("TLS requires a CA path")
        options.update(ssl_ca=tls["ca"], ssl_verify_cert=True, ssl_verify_identity=True)
    return pymysql.connect(host=config["host"], port=int(config["port"]), database=config["database"],
                           user=config["user"], password=password, charset="utf8mb4",
                           cursorclass=pymysql.cursors.DictCursor, connect_timeout=10,
                           read_timeout=int(config.get("read_timeout", 120)), **options)
