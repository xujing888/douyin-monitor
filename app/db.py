import pymysql

from . import config


def conn():
    return pymysql.connect(
        host=config.DB["host"],
        port=int(config.DB["port"]),
        user=config.DB["user"],
        password=config.DB["password"],
        database=config.DB["database"],
        charset="utf8mb4",
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
    )
